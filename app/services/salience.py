"""Salience gate for ambient capture (connector capture & task fulfillment
spec, Feature 1.6).

After a connector item (or any observed-tier event that names itself with
meta.connector_id) is persisted, it is scored — deterministically first —
against what the user is already waiting on:

  * open slots (Feature 3) — the fill score the slot watcher computed;
  * open commitments and waiting_on_them loops — the counterparty, or the
    task's own words, appear in the item;
  * people the user is in active threads with — a known person who has
    exchanged mail with the user in the last week;
  * explicit watch phrases — data/watch_phrases.json (user-defined).

Above threshold → an attention_ledger impression and one chat-stream notice
through agent_bridge ("A quote from Dana at Acme just arrived — you were
waiting on this"). Below → silent persist. At most N notices per hour per
connector (default 5), deduped by thread or external id; never during
meeting mode (deferred, then flushed).

LLM scoring is optional (QUILL_SALIENCE_LLM=1) and only breaks ties in the
band just under threshold; it runs on the local ladder, and model_router
enforces privacy_class before any cloud call as it does everywhere else.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from app.events import Event

THRESHOLD = float(os.environ.get("QUILL_SALIENCE_THRESHOLD", "0.6"))
NOTICES_PER_HOUR = int(os.environ.get("QUILL_SALIENCE_NOTICES_PER_HOUR", "5"))
ACTIVE_THREAD_DAYS = float(os.environ.get("QUILL_SALIENCE_THREAD_DAYS", "7"))

_lock = threading.Lock()
_notices: dict[str, deque] = {}          # connector_id -> timestamps
_seen_keys: dict[str, float] = {}        # dedupe key -> ts
_deferred: list[dict] = []               # notices held during meeting mode


def enabled() -> bool:
    return os.environ.get("QUILL_SALIENCE", "1") not in ("0", "false", "False")


def llm_enabled() -> bool:
    return os.environ.get("QUILL_SALIENCE_LLM", "0") in ("1", "true", "True")


def _now(now: float | None) -> float:
    return float(now if now is not None else time.time())


# --- inputs -------------------------------------------------------------------
def _meta(event: Event | dict) -> dict:
    meta = event.get("meta") if isinstance(event, dict) else event.meta
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _field(event: Event | dict, key: str, default=None):
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def _people(event: Event | dict) -> list[str]:
    raw = _field(event, "people") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    return [str(x) for x in raw or []]


def is_connector_event(event: Event | dict) -> bool:
    meta = _meta(event)
    if meta.get("connector_id"):
        return True
    src = str(_field(event, "source") or "")
    return src.startswith("agent.fetch") or src.startswith("peer.update")


def watch_phrases_path() -> Path:
    from app.config import settings
    return Path(settings.storage.data_dir) / "watch_phrases.json"


def watch_phrases() -> list[str]:
    try:
        p = watch_phrases_path()
        if not p.is_file():
            return []
        raw = json.loads(p.read_text(encoding="utf-8"))
        items = raw.get("phrases") if isinstance(raw, dict) else raw
        return [str(x).strip() for x in (items or []) if str(x).strip()]
    except Exception:
        return []


def set_watch_phrases(phrases: list[str]) -> list[str]:
    clean = []
    for p in phrases or []:
        s = str(p or "").strip()[:120]
        if s and s.lower() not in {c.lower() for c in clean}:
            clean.append(s)
    from app.atomic_json import write_json
    write_json(watch_phrases_path(), {"phrases": clean})
    return clean


def active_thread_people(store, *, now: float | None = None) -> set[str]:
    """Known people who appear on connector mail items in the last week."""
    now = _now(now)
    out: set[str] = set()
    try:
        rows = store.recent_events(source_substr=".mail", limit=200,
                                   since=now - ACTIVE_THREAD_DAYS * 86400)
    except Exception:
        return out
    for r in rows:
        for p in r.get("people") or []:
            if isinstance(p, str) and p.strip():
                out.add(p.strip().lower())
    return out


# --- scoring ---------------------------------------------------------------------
def score(store, event_id: int, event: Event | dict, *,
          fills: list[dict] | None = None,
          completions: list[dict] | None = None,
          now: float | None = None) -> dict[str, Any]:
    """Deterministic salience: {score, reasons, hits}. Never raises."""
    from app.services import slots as _slots
    from app.services import task_completion as _tc

    now = _now(now)
    text = _tc._ev_text(event)
    low = text.lower()
    people = [p.lower() for p in _people(event)]
    best = 0.0
    reasons: list[str] = []
    hits: dict[str, Any] = {}

    # 1. Open slots — the watcher already scored them.
    if fills:
        top = max(float(f.get("score") or 0) for f in fills)
        best = max(best, 1.0)
        need = (fills[0].get("need") or "").strip()
        reasons.append(f"fills the open slot for {need!r}" if need
                       else "fills an open slot")
        hits["slot_fact_ids"] = [int(f["fact_id"]) for f in fills]
        hits["slot_score"] = round(top, 3)
    else:
        try:
            for row in _slots.open_slots(store):
                slot = row.get("slot") or {}
                if not _slots.eligible(event, slot=slot, event_id=event_id):
                    continue
                sc, _parts = _slots.score_event(slot, event)
                if sc >= 0.5:
                    best = max(best, 0.4 + 0.6 * sc)
                    reasons.append(f"near the open slot for {slot.get('need')!r}")
                    hits.setdefault("slot_near", []).append(int(row["fact_id"]))
        except Exception as exc:
            print(f"[salience] slot scan skipped ({exc}).")

    # 2. Open commitments / waiting_on_them loops.
    if completions:
        for c in completions:
            if c.get("verdict") in ("completes", "progresses"):
                best = max(best, 0.9 if c.get("verdict") == "completes" else 0.7)
                reasons.append(f"{c['verdict']} task #{c.get('fact_id')}")
                hits.setdefault("task_fact_ids", []).append(int(c["fact_id"]))
    try:
        for task in _tc.open_tasks(store):
            cp = _tc.counterparty_of(task, store)
            if cp and _tc._name_in(cp, text, people):
                waiting = bool(task.get("counterparty_expects")) or (
                    (task.get("from_person") or "").strip().lower() == cp.lower())
                best = max(best, 0.85 if waiting else 0.7)
                reasons.append(f"from {cp} — you have an open item with them")
                hits.setdefault("task_fact_ids", []).append(int(task["fact_id"]))
                continue
            ov_ = _tc._overlap(task.get("text") or "", text)
            if ov_ >= 0.6:
                best = max(best, 0.65)
                reasons.append(f"mentions your open task: {task.get('text')!r}"[:100])
                hits.setdefault("task_fact_ids", []).append(int(task["fact_id"]))
    except Exception as exc:
        print(f"[salience] task scan skipped ({exc}).")

    # 3. People in active threads.
    try:
        active = active_thread_people(store, now=now)
        for p in people:
            if p in active and p:
                best = max(best, 0.6)
                reasons.append(f"{p.title()} is in an active thread with you")
                hits.setdefault("active_people", []).append(p)
                break
    except Exception:
        pass

    # 4. Explicit watch phrases.
    for phrase in watch_phrases():
        if re.search(r"\b" + re.escape(phrase.lower()) + r"\b", low):
            best = max(best, 0.9)
            reasons.append(f"matches your watch phrase {phrase!r}")
            hits.setdefault("watch", []).append(phrase)

    # Optional LLM tie-break just under threshold.
    if llm_enabled() and THRESHOLD - 0.2 <= best < THRESHOLD:
        verdict = _llm_salient(text)
        if verdict is True:
            best = THRESHOLD
            reasons.append("local model judged it important")
        elif verdict is False:
            best = min(best, THRESHOLD - 0.01)

    return {"score": round(min(1.0, best), 3), "reasons": reasons[:4],
            "hits": hits}


_LLM_SCHEMA = {"type": "object",
               "properties": {"important": {"type": "boolean"}},
               "required": ["important"]}


def _llm_salient(text: str) -> bool | None:
    try:
        from app.services.model_router import router
        res = router.complete_json(
            "salience",
            system=("Is this newly captured item something the user would want "
                    "to be told about right now (a reply they are waiting on, "
                    "a document arriving, a decision)? Answer strictly."),
            messages=[{"role": "user", "content": text[:1500]}],
            schema=_LLM_SCHEMA, max_tokens=16)
        val = (res or {}).get("important")
        return bool(val) if isinstance(val, bool) else None
    except Exception:
        return None


# --- notices --------------------------------------------------------------------------
def _dedupe_key(event: Event | dict) -> str:
    meta = _meta(event)
    cid = str(meta.get("connector_id") or _field(event, "source") or "")
    key = meta.get("thread_id") or meta.get("thread_key") or meta.get("external_id")
    return f"{cid}:{key}" if key else ""


def _rate_ok(connector_id: str, now: float) -> bool:
    with _lock:
        q = _notices.setdefault(connector_id, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= max(1, NOTICES_PER_HOUR):
            return False
        q.append(now)
        return True


def _in_meeting() -> bool:
    try:
        from app.services import meeting_mode
        return bool(meeting_mode.status().get("active"))
    except Exception:
        return False


def headline(event: Event | dict, reasons: list[str]) -> str:
    meta = _meta(event)
    title = str(meta.get("title") or _field(event, "summary") or "").strip()
    title = re.sub(r"^\[[^\]]+\]\s*", "", title)[:120]
    who = ""
    people = _people(event)
    if people:
        who = f" from {people[0]}"
    why = reasons[0] if reasons else "it looks relevant"
    return f"Just arrived{who}: “{title}” — {why}."


def evaluate(store, event_id: int, event: Event | dict, *,
             fills: list[dict] | None = None,
             completions: list[dict] | None = None,
             now: float | None = None) -> dict[str, Any] | None:
    """Score a connector event and, above threshold, record + notify.
    Returns the score payload (with `notified`) or None when the event is
    not connector capture."""
    if not enabled() or not is_connector_event(event):
        return None
    now = _now(now)
    result = score(store, event_id, event, fills=fills,
                   completions=completions, now=now)
    result["event_id"] = int(event_id)
    result["notified"] = False
    result["deferred"] = False
    if result["score"] < THRESHOLD:
        return result
    meta = _meta(event)
    cid = str(meta.get("connector_id") or (_field(event, "source") or "").split(".")[0])
    key = _dedupe_key(event)
    with _lock:
        if key and key in _seen_keys and now - _seen_keys[key] < 6 * 3600:
            result["deduped"] = True
            return result
        if key:
            _seen_keys[key] = now
    # Attention ledger impression, whether or not the notice fires now.
    try:
        from app.services.attention_ledger import attention_ledger
        fid = None
        ids = result["hits"].get("slot_fact_ids") or result["hits"].get("task_fact_ids")
        if ids:
            fid = int(ids[0])
        attention_ledger.record_offer(
            fact_id=fid, text=headline(event, result["reasons"]),
            kind="connector.salient_item", score=result["score"], store=store)
    except Exception as exc:
        print(f"[salience] ledger skipped ({exc}).")
    # Slot fills already produced their own Deliver / Not it offer; the
    # salience notice is the general "you were waiting on this" line.
    if fills:
        result["notified"] = True
        result["via"] = "slot_offer"
        return result
    if not _rate_ok(cid, now):
        result["rate_limited"] = True
        return result
    notice = {"text": headline(event, result["reasons"]),
              "stream": {"type": "connector.salient_item",
                         "event_id": int(event_id), "connector_id": cid,
                         "score": result["score"],
                         "task_id": (result["hits"].get("task_fact_ids") or [None])[0],
                         "actions": [{"label": "Open", "reply": f"show event {int(event_id)}"}]}}
    if _in_meeting():
        with _lock:
            _deferred.append(notice)
        result["deferred"] = True
        return result
    _emit(notice)
    result["notified"] = True
    return result


def flush_deferred() -> int:
    """Send notices held during meeting mode. Returns how many."""
    if _in_meeting():
        return 0
    with _lock:
        pending = list(_deferred)
        _deferred.clear()
    for n in pending:
        _emit(n)
    return len(pending)


def deferred_count() -> int:
    with _lock:
        return len(_deferred)


def _emit(notice: dict) -> None:
    try:
        from app.services.agent_bridge import worker
        worker._emit("result", notice["text"], stream=notice.get("stream"))
    except Exception as exc:
        print(f"[salience] notice skipped ({exc}).")


def reset_for_tests() -> None:
    with _lock:
        _notices.clear()
        _seen_keys.clear()
        _deferred.clear()
