"""Connector background sync (connector capture & task fulfillment spec,
Feature 1).

One daemon thread polls every connector that is `availability == "ready"`,
`connected()`, consented for background sync, and implements
`fetch_items(cursor, now)`; each on its own `sync_interval_s` (default 300 s,
per-connector override via QUILL_CONNECTOR_SYNC_S_<ID>). Mirrors
icloud_calendar.start_background(); wired in main.py next to the iCloud hook.
Off switch: QUILL_CONNECTOR_SYNC=0.

Incremental and idempotent: the connector persists a cursor (history id,
updated-since timestamp) in connector_sync.cursor_json, and the scheduler
keeps a bounded ledger of external ids there too, so a sync never re-lands
an item — the soak criterion is that items_landed stays flat against a
stub connector that keeps returning the same items.

Uniform landing: every item becomes an Event with source="<connector>.<kind>"
(google.mail, google.calendar, slack.dm…), meta.epistemic="observed",
meta.never_authorizes=True, meta.connector_id, meta.external_id,
meta.thread_id, and a privacy_class stamp (Store.insert). DOCUMENT for
message bodies and attachments, SYSTEM for metadata-only items. The insert
hook (task_completion) then scores it against open slots, open commitments,
and the salience gate.

Read-only by construction: nothing here issues a write to any provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any

from app.events import Event, Modality

DEFAULT_INTERVAL_S = float(os.environ.get("QUILL_CONNECTOR_SYNC_DEFAULT_S", "300"))
LEDGER_MAX = int(os.environ.get("QUILL_CONNECTOR_LEDGER_MAX", "5000"))
MAX_ITEMS_PER_SYNC = int(os.environ.get("QUILL_CONNECTOR_MAX_ITEMS", "500"))

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_running: set[str] = set()


def enabled() -> bool:
    return os.environ.get("QUILL_CONNECTOR_SYNC", "1") not in ("0", "false", "False")


def interval_for(connector) -> float:
    cid = (getattr(connector, "id", "") or "").upper().replace("-", "_")
    raw = os.environ.get(f"QUILL_CONNECTOR_SYNC_S_{cid}")
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            pass
    val = getattr(connector, "sync_interval_s", None)
    try:
        return max(30.0, float(val)) if val else DEFAULT_INTERVAL_S
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S


def syncable(connector) -> bool:
    """Ready, connected, consented, and able to hand over items."""
    try:
        if getattr(connector, "availability", "") != "ready":
            return False
        if not callable(getattr(connector, "fetch_items", None)):
            return False
        if not connector.connected():
            return False
    except Exception:
        return False
    try:
        from app.services import capture_consent
        return capture_consent.allows_connector(connector.id)
    except Exception:
        return False


def consent_sentence(connector) -> str:
    """The plain sentence recorded at connect time (spec F1 trust §8)."""
    what = getattr(connector, "sync_blurb", None) or (
        f"new items from {getattr(connector, 'label', connector.id)}")
    every = int(interval_for(connector))
    return (f"Sparrow will check {what} in the background about every "
            f"{every // 60 or 1} minute{'s' if every >= 120 else ''}, read-only, "
            "and keep what it finds as observed memory on this machine.")


# --- landing ---------------------------------------------------------------------
def _fingerprint(connector_id: str, item: dict) -> str:
    key = str(item.get("external_id") or "")
    if not key:
        blob = json.dumps({k: item.get(k) for k in ("kind", "ts", "title", "text")},
                          sort_keys=True, default=str)
        key = hashlib.sha1(blob.encode("utf-8")).hexdigest()
    content = hashlib.sha1(json.dumps(
        {k: item.get(k) for k in ("title", "text", "body", "ts", "end")},
        sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    return f"{connector_id}:{item.get('kind') or 'item'}:{key}:{content}"


def land_item(connector, item: dict, *, store=None) -> int | None:
    """Persist one connector item as an Event. Returns the event id."""
    if store is None:
        from app.storage import get_store
        store = get_store()
    from app.services import confidence as _conf
    cid = str(getattr(connector, "id", "") or "connector").lower()
    kind = str(item.get("kind") or "item").lower()
    body = item.get("body")
    text = str(item.get("text") or "").strip()
    title = str(item.get("title") or "").strip()
    raw = (body if isinstance(body, str) and body.strip() else text) or title
    if not raw:
        return None
    modality = Modality.DOCUMENT if (isinstance(body, str) and body.strip()) \
        or item.get("attachment") else Modality.SYSTEM
    ts = item.get("ts")
    try:
        ts = float(ts) if ts is not None else time.time()
    except (TypeError, ValueError):
        ts = time.time()
    people = [str(p) for p in (item.get("people") or []) if str(p).strip()]
    entities = [str(e) for e in (item.get("entities") or []) if str(e).strip()]
    meta: dict[str, Any] = {
        "section": "connectors", "origin": "connector",
        "connector_id": cid, "external_id": item.get("external_id"),
        "thread_id": item.get("thread_id"), "title": title,
        "never_authorizes": True, "external_source": True,
    }
    for key in ("start", "end", "attendees", "organizer", "from", "to", "cc",
                "direction", "from_self", "url", "labels", "summary"):
        if item.get(key) not in (None, "", [], {}):
            meta[key] = item[key]
    if item.get("meta"):
        meta.update({k: v for k, v in dict(item["meta"]).items()
                     if k not in meta})
    ev = Event(time=ts, modality=modality, raw=raw,
               summary=f"[{cid}.{kind}] {title or raw[:120]}",
               source=f"{cid}.{kind}", confidence=0.9,
               people=people, entities=entities, meta=meta)
    _conf.attach(ev, _conf.OBSERVED, capture=0.95)
    eid = int(store.insert(ev))
    try:
        from app.services.slots import index_if_bound
        index_if_bound(store, eid, ev)
    except Exception:
        pass
    return eid


# --- one connector, one pass ---------------------------------------------------------
def sync_connector(connector, *, store=None, now: float | None = None,
                   force: bool = False) -> dict[str, Any]:
    """Fetch new items since the cursor, land the unseen ones, advance."""
    if store is None:
        from app.storage import get_store
        store = get_store()
    now = float(now if now is not None else time.time())
    cid = str(getattr(connector, "id", "") or "").lower()
    with _lock:
        if cid in _running:
            return {"ok": True, "connector_id": cid, "running": True}
        _running.add(cid)
    try:
        state = store.connector_sync_get(cid) or {}
        cursor = dict(state.get("cursor") or {})
        if not force and state.get("next_sync") and float(state["next_sync"]) > now:
            return {"ok": True, "connector_id": cid, "skipped": "not due",
                    "next_sync": state["next_sync"]}
        interval = interval_for(connector)
        try:
            items, new_cursor = connector.fetch_items(cursor=cursor, now=now)
        except Exception as exc:
            store.connector_sync_set(cid, last_sync=now, next_sync=now + interval,
                                     last_error=str(exc)[:300])
            return {"ok": False, "connector_id": cid, "error": str(exc)}
        seen: list[str] = list(cursor.get("seen") or [])
        seen_set = set(seen)
        landed = 0
        for item in list(items or [])[:MAX_ITEMS_PER_SYNC]:
            fp = _fingerprint(cid, item)
            if fp in seen_set:
                continue
            try:
                eid = land_item(connector, item, store=store)
            except Exception as exc:
                print(f"[connectors] {cid} item landing skipped ({exc}).")
                continue
            if eid:
                landed += 1
            seen.append(fp)
            seen_set.add(fp)
        if len(seen) > LEDGER_MAX:
            seen = seen[-LEDGER_MAX:]
        merged = dict(new_cursor or cursor)
        merged["seen"] = seen
        store.connector_sync_set(cid, cursor=merged, last_sync=now,
                                 next_sync=now + interval,
                                 items_landed_add=landed, clear_error=True)
        return {"ok": True, "connector_id": cid, "landed": landed,
                "fetched": len(items or []), "next_sync": now + interval}
    finally:
        with _lock:
            _running.discard(cid)


def sync_all(*, store=None, now: float | None = None,
             force: bool = False) -> dict[str, Any]:
    """Manual tick (POST /connectors/sync-all)."""
    from app.services.connectors import registry
    out = []
    for c in registry.all():
        if not syncable(c):
            continue
        out.append(sync_connector(c, store=store, now=now, force=force))
    return {"ok": True, "synced": out, "count": len(out)}


def status(connector_id: str, *, store=None) -> dict[str, Any]:
    """last_sync / next_sync / items_landed / last_error for GET /connectors/{id}."""
    if store is None:
        from app.storage import get_store
        store = get_store()
    st = store.connector_sync_get(connector_id) or {}
    from app.services.connectors import registry
    c = registry.get(connector_id)
    try:
        from app.services import capture_consent
        consented = capture_consent.allows_connector(connector_id)
    except Exception:
        consented = False
    return {
        "background_sync": bool(c is not None and enabled() and consented
                                and callable(getattr(c, "fetch_items", None))),
        "consented": consented,
        "sync_interval_s": interval_for(c) if c is not None else None,
        "last_sync": st.get("last_sync"),
        "next_sync": st.get("next_sync"),
        "items_landed": int(st.get("items_landed") or 0),
        "last_error": st.get("last_error"),
        "consent_sentence": consent_sentence(c) if c is not None else None,
    }


# --- the daemon --------------------------------------------------------------------------
def _loop() -> None:
    from app.services.connectors import registry
    if _stop.wait(5.0):
        return
    while not _stop.is_set():
        wait = 30.0
        try:
            now = time.time()
            for c in registry.all():
                if not syncable(c):
                    continue
                res = sync_connector(c, now=now)
                nxt = res.get("next_sync")
                if nxt:
                    wait = min(wait, max(5.0, float(nxt) - time.time()))
        except Exception as exc:
            print(f"[connectors] sync pass skipped ({exc}).")
        try:
            from app.services import salience
            salience.flush_deferred()
        except Exception:
            pass
        if _stop.wait(max(5.0, min(wait, 300.0))):
            return


def start_background() -> bool:
    """One daemon thread per process; best-effort, quiet when idle."""
    global _thread
    if not enabled():
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="connector-sync",
                                   daemon=True)
        _thread.start()
    return True


def stop() -> None:
    _stop.set()
