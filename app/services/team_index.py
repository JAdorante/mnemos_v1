"""Team search, this seat's half: the pointers it keeps, and where they go.

Every item a teammate could usefully be routed to (mail, meetings, messages,
documents, accepted notes) gets a pointer, built by `build` from the stored
event: owner, project / org tags, type, people, time and where it lives.
Shape and matching are shared with the org service
(app/services/records/pointers.py). Pointers live in a small local table, and
where they go depends on the deployment rung:

  Connected            published to the org's central index, so teammates
                       find them even while this seat is offline
  Sealed / Perimeter   never leave the seat. A teammate's search arrives as
                       a `pointer_query` peer ask; this seat answers it from
                       the same table with the same rule, and the owner
                       approves each answer (app/services/team_search.py)

QUILL_TEAM_SEARCH picks the setting: off (default), on (publish on
Connected, answer directly otherwise), or federated (never publish). An org
admin can also turn publishing off for the whole org (policy
team_search.publish), and the service refuses pointers off Connected.

What a pointer may carry is the point of the design:
  * No titles and no snippets, except on items in a shared project, and
    then only when the text itself passes the egress privacy check.
  * Nothing at all for an item above the rung's privacy ceiling, or from a
    source that is personal by nature (texts, calls, screen, audio, chat).
  * People are display names, never addresses.

Taking pointers back is as important as publishing them:
  * a deleted or expired item is withdrawn as it goes (store delete hook),
    and a failed withdrawal is retried before anything new is published
  * unsharing a project withdraws this member's pointers there at once
  * leaving Connected, or turning the setting off, withdraws everything
  * a daily reconcile sends the full list of live items, and the service
    drops anything else this seat ever published
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.services.records import pointers as P

MODES = ("off", "central", "federated")
_BATCH = 200
_RESCAN_IDS = 2000         # recent events re-read each pass (tags arrive late)
_RECONCILE_S = 86400.0
_FULL_S = 86400.0          # a full rescan catches deletes the hook missed
_MAX_PER_TICK = 2000       # a first publish of a big store spreads out
_SNIPPET = 240
_lock = threading.RLock()
_attached_store = None

# Sources that are personal by nature never get a pointer, whatever their
# privacy stamp says.
_NEVER = ("imessage.", "sms.", "phone", "peer.", "fleet.", "org.", "chat",
          "onboarding", "self", "screen", "desktop", "camera", "audio",
          "ambient", "browser", "agent", "relay", "signal")
_TAIL_KIND = {
    "mail": "email", "email": "email", "message": "message", "chat": "message",
    "channel": "message", "dm": "message", "thread": "message",
    "calendar": "meeting", "event": "meeting", "meeting": "meeting",
    "transcript": "meeting", "document": "document", "doc": "document",
    "page": "document", "file": "document", "sheet": "document",
    "drive": "document", "note": "note",
}
_TAG_KINDS = frozenset({"project", "org", "company", "organization"})
_NAME_ADDR = re.compile(r"^\s*\"?([^<\"@]+?)\"?\s*<[^>]*>\s*$")


# ------------------------------------------------------------------ mode --
def setting() -> str:
    v = os.environ.get("QUILL_TEAM_SEARCH", "off").strip().lower()
    if v in ("1", "on", "true", "yes", "auto", "central"):
        return "on"
    return "federated" if v == "federated" else "off"


def mode() -> str:
    """off | central | federated, for this seat right now."""
    if setting() == "off":
        return "off"
    from app.services.records import org_client
    if not org_client.joined():
        return "off"
    from app.services import egress
    if setting() == "federated" or egress.rung() != "connected":
        return "federated"
    m = org_client.membership()
    if str(m.get("team_index") or "open").startswith("closed"):
        return "federated"
    ts = (m.get("policy") or {}).get("team_search") or {}
    if isinstance(ts, dict) and ts.get("publish") is False:
        return "federated"
    return "central"


# ----------------------------------------------------------- local table --
def _db_path() -> Path:
    from app.config import settings
    return Path(settings.storage.data_dir) / "team_index.db"


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(str(_db_path()), timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS pointers (
            item_ref TEXT PRIMARY KEY, event_id INTEGER NOT NULL,
            hash TEXT NOT NULL, body TEXT NOT NULL, scope_id TEXT,
            published INTEGER NOT NULL DEFAULT 0, built_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS pointers_event ON pointers(event_id);
        CREATE TABLE IF NOT EXISTS pending (
            op TEXT NOT NULL, arg TEXT NOT NULL, queued_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY (op, arg));
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
    """)
    return c


@contextmanager
def _open():
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _get(c, key: str, default: Any = None) -> Any:
    r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return default


def _set(c, key: str, value: Any) -> None:
    c.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)",
              (key, json.dumps(value)))


def holds_published() -> bool:
    """True while the org index may still hold pointers from this seat."""
    if not _db_path().exists():
        return False
    with _lock, _open() as c:
        return bool(_get(c, "index_holds", False))


def unshared_scopes() -> set[str]:
    if not _db_path().exists():
        return set()
    with _lock, _open() as c:
        return set(_get(c, "unshared", []) or [])


# --------------------------------------------------------------- building --
def kind_for(source: str | None, meta: dict | None = None) -> str | None:
    """The pointer type for an event's source, or None for anything that
    should never be pointed at."""
    s = (source or "").strip().lower()
    if not s or s.startswith(_NEVER):
        return None
    if s.startswith("assistant."):
        return "note"
    if s == "documents.scan":
        return "document"
    meta = meta or {}
    if meta.get("origin") != "connector":
        return None
    tail = s.rsplit(".", 1)[-1]
    return _TAIL_KIND.get(tail, "item")


def _person_name(v: Any) -> str:
    s = str(v or "").strip()
    m = _NAME_ADDR.match(s)
    if m:
        return m.group(1).strip()
    return "" if "@" in s else s


def _people(row: dict, meta: dict) -> list[str]:
    out: list[str] = []
    vals: list[Any] = list(row.get("people") or [])
    for k in ("from", "to", "cc", "attendees", "organizer", "speakers"):
        v = meta.get(k)
        vals.extend(v if isinstance(v, (list, tuple)) else [v])
    split: list[Any] = []
    for v in vals:
        if isinstance(v, str) and ("@" in v or "<" in v):
            split.extend(re.split(r"[,;]\s*(?=[^>]*(?:<|$))", v))
        else:
            split.append(v)
    for v in split:
        name = _person_name(v)
        if name and name.casefold() not in {o.casefold() for o in out}:
            out.append(name)
    return out[:P.MAX_PEOPLE]


def _entity_kinds(store) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for e in store.all_entities():
            kind = str(e.get("kind") or "").lower()
            if kind in _TAG_KINDS:
                for n in [e.get("name"), *(e.get("aliases") or [])]:
                    if n:
                        out[str(n).casefold()] = str(e.get("name"))
    except Exception as exc:
        print(f"[team_index] entity kinds skipped ({exc}).")
    return out


def _fact_tags(store, event_id: int, kinds: dict[str, str]) -> list[str]:
    try:
        with store._lock:
            ids = [int(r["id"]) for r in store._conn.execute(
                "SELECT id FROM facts WHERE source_event_id = ?",
                (int(event_id),)).fetchall()]
        names = [n for ns in store.fact_entities(ids).values() for n in ns]
    except Exception:
        return []
    return [kinds[n.casefold()] for n in names if n and n.casefold() in kinds]


def _fact_count(store, event_id: int) -> int:
    try:
        with store._lock:
            r = store._conn.execute(
                "SELECT COUNT(*) AS n FROM facts WHERE source_event_id = ?",
                (int(event_id),)).fetchone()
        return int(r["n"] or 0)
    except Exception:
        return 0


def _tags(store, row: dict, meta: dict, kinds: dict[str, str]) -> list[str]:
    from app.services import projects
    names: list[str] = []
    if meta.get("project"):
        names.append(str(meta["project"]))
    names += [kinds[str(e).casefold()] for e in (row.get("entities") or [])
              if str(e).casefold() in kinds]
    names += _fact_tags(store, int(row["id"]), kinds)
    out: list[str] = []
    for n in names:
        n = projects.clean_name(n)
        if n and not projects.is_misc(n) and \
                projects.norm(n) not in {projects.norm(o) for o in out}:
            out.append(n)
    return out[:P.MAX_TAGS]


def _shared_scope(tags: list[str], reg: list[dict], unshared: set[str]) -> str | None:
    from app.services import projects
    for t in tags:
        hit = projects.resolve(t, reg)
        if hit and hit.get("shared") and hit.get("scope_id") \
                and hit["scope_id"] not in unshared:
            return str(hit["scope_id"])
    return None


def _title(row: dict, meta: dict) -> str:
    t = str(meta.get("title") or "").strip()
    if not t:
        t = re.sub(r"^\[[^\]]*\]\s*", "", str(row.get("summary") or "")).strip()
    return t


def _decode(row: Any) -> dict:
    d = dict(row)
    for k in ("people", "entities", "meta"):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except ValueError:
                d[k] = {} if k == "meta" else []
    d["meta"] = d.get("meta") if isinstance(d.get("meta"), dict) else {}
    return d


def build(store, row: Any, *, reg: list[dict] | None = None,
          kinds: dict[str, str] | None = None,
          unshared: set[str] | None = None) -> dict | None:
    """The pointer for one stored event, or None when it gets none."""
    from app.services import privacy_class as pc
    row = _decode(row)
    meta = row["meta"]
    kind = kind_for(row.get("source"), meta)
    if kind is None or meta.get("never_share") or meta.get("private"):
        return None
    cls = pc.normalize(row.get("privacy_class") or meta.get("privacy_class")
                       or pc.INTERNAL)
    # Work items only: a personal-class item is nobody's routing hint, even
    # where the rung would let it leave.
    if pc.rank(cls) > pc.rank(pc.INTERNAL) or not pc.egress_allowed(cls):
        return None
    if reg is None:
        from app.services import projects
        reg = projects.registry(store)
    kinds = _entity_kinds(store) if kinds is None else kinds
    unshared = unshared_scopes() if unshared is None else unshared
    tags = _tags(store, row, meta, kinds)
    if not tags:
        # Tags are the index: an item filed under no project or org is not
        # part of the team's filing, so its people are not published either.
        return None
    people = _people(row, meta)
    scope = _shared_scope(tags, reg, unshared)
    p: dict[str, Any] = {
        "item_ref": f"ev:{int(row['id'])}", "kind": kind, "scope_id": scope,
        "tags": tags, "people": people, "at": float(row["time"]),
        "source": str(row.get("source") or "")[:P.MAX_SOURCE],
        "refs": _fact_count(store, int(row["id"]))}
    if scope:
        title = _title(row, meta)
        body = " ".join(str(row.get("raw") or "").split())
        snippet = "" if body == title else body[:_SNIPPET]
        if pc.egress_refusal(f"{title}\n{snippet}", source=p["source"]) is None:
            p["title"], p["snippet"] = title, snippet
    return P.clean(p)


def _hash(p: dict) -> str:
    return hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------------ hooks --
def attach(store=None) -> None:
    """Follow deletes in this store. Called once at startup (and by tests
    with their own store)."""
    global _attached_store
    from app.storage import add_delete_hook, get_store
    _attached_store = store if store is not None else get_store()
    add_delete_hook(_on_store_change)


def _on_store_change(store, change: dict) -> None:
    if _attached_store is None or store is not _attached_store:
        return
    refs = [f"ev:{int(eid)}" for eid, _t in change.get("deleted_events") or []]
    rewritten = [f"ev:{int(eid)}" for eid, _t, _r in
                 change.get("rewritten_events") or []]
    if not (refs or rewritten) or not _db_path().exists():
        return
    with _lock, _open() as c:
        marks = ",".join("?" for _ in refs)
        published = [r["item_ref"] for r in c.execute(
            f"SELECT item_ref FROM pointers WHERE published=1 AND item_ref IN "
            f"({marks})", refs).fetchall()] if refs else []
        if refs:
            c.execute(f"DELETE FROM pointers WHERE item_ref IN ({marks})", refs)
        if rewritten:
            # Rebuilt on the next pass from the new text.
            c.executemany("UPDATE pointers SET hash='' WHERE item_ref=?",
                          [(r,) for r in rewritten])
        for r in published:
            c.execute("INSERT OR IGNORE INTO pending(op, arg, queued_at) "
                      "VALUES ('withdraw', ?, ?)", (r, time.time()))
    if published:
        # Off the deleting thread: an erase never waits on the network.
        threading.Thread(target=flush_pending, name="team-index-withdraw",
                         daemon=True).start()


def flush_pending() -> dict[str, int]:
    """Send queued withdrawals and unshares. Kept until the service says ok."""
    from app.services.records import org_client
    out = {"sent": 0, "failed": 0}
    with _lock, _open() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM pending ORDER BY queued_at").fetchall()]
    if not rows or not org_client.joined():
        return out
    withdraws = [r["arg"] for r in rows if r["op"] == "withdraw"]
    done: list[tuple[str, str]] = []
    err = ""
    try:
        for i in range(0, len(withdraws), 1000):
            org_client.withdraw_pointers(withdraws[i:i + 1000])
            done += [("withdraw", a) for a in withdraws[i:i + 1000]]
        for r in rows:
            if r["op"] == "unshare":
                org_client.unshare_pointers(r["arg"])
                done.append(("unshare", r["arg"]))
    except org_client.OrgRefused as exc:
        # A refusal is final (inactive member, bad ref): retrying cannot help,
        # and the service purges a departed member's pointers itself.
        err = str(exc)
        done += [(r["op"], r["arg"]) for r in rows]
    except Exception as exc:
        err = str(exc)
    with _lock, _open() as c:
        c.executemany("DELETE FROM pending WHERE op=? AND arg=?", done)
        if err:
            c.execute("UPDATE pending SET attempts = attempts + 1, "
                      "last_error = ?", (err[:300],))
    out["sent"] = len(done)
    out["failed"] = len(rows) - len(done)
    if err:
        print(f"[team_index] withdrawal kept for retry ({err}).")
    return out


# ------------------------------------------------------------------- sync --
def _candidate_rows(store, *, since_id: int | None) -> list[dict]:
    # Only the text a snippet can use; a long transcript stays on disk.
    sql = ("SELECT id, time, source, summary, substr(raw, 1, 2000) AS raw, "
           "people, entities, meta, privacy_class FROM events WHERE "
           "(source LIKE 'assistant.%' OR source = 'documents.scan' OR "
           "meta LIKE '%\"origin\": \"connector\"%')")
    args: tuple = ()
    if since_id is not None:
        sql += " AND id > ?"
        args = (int(since_id),)
    with store._lock:
        try:
            rows = store._conn.execute(sql + " ORDER BY id", args).fetchall()
        except sqlite3.OperationalError:
            rows = store._conn.execute(
                sql.replace(", privacy_class", "") + " ORDER BY id",
                args).fetchall()
    return [dict(r) for r in rows if kind_for(dict(r).get("source"),
                                               _meta_of(dict(r))) is not None]


def _meta_of(row: dict) -> dict:
    m = row.get("meta")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = {}
    return m if isinstance(m, dict) else {}


def _withdraw_everything(c) -> dict[str, Any]:
    """The index must not hold this seat's pointers (setting off, or the
    rung left Connected). An empty reconcile drops every one of them."""
    from app.services.records import org_client
    try:
        out = org_client.reconcile_pointers([])
    except Exception as exc:
        return {"withdraw_all": "retry", "error": str(exc)}
    c.execute("UPDATE pointers SET published=0")
    c.execute("DELETE FROM pending WHERE op='withdraw'")
    _set(c, "index_holds", False)
    return {"withdraw_all": out.get("withdrawn", 0)}


def sync(store=None, *, now: float | None = None,
         full: bool = False) -> dict[str, Any]:
    """One pass: rebuild what changed, publish it (Connected), withdraw what
    no longer qualifies, reconcile once a day."""
    from app.services.records import org_client
    now = float(now if now is not None else time.time())
    m = mode()
    out: dict[str, Any] = {"mode": m}
    if store is None:
        from app.storage import get_store
        store = get_store()
    with _lock:
        c = _connect()
        try:
            if m != "central" and _get(c, "index_holds", False) \
                    and org_client.joined():
                out.update(_withdraw_everything(c))
                c.commit()
            if m == "off":
                return out
            if m == "central":
                out["pending"] = flush_pending()
            cursor = _get(c, "cursor")
            from app.services import projects
            reg = projects.registry(store)
            unshared = set(_get(c, "unshared", []) or [])
            shared_now = sorted(str(r["scope_id"]) for r in reg
                                if r.get("shared") and r.get("scope_id"))
            # Joining or leaving a project changes which items may carry a
            # title, so every pointer is rebuilt, not only recent ones.
            full = (full or cursor is None or _get(c, "full_due", False)
                    or _get(c, "shared_scopes") != shared_now
                    or now - float(_get(c, "last_full", 0) or 0) >= _FULL_S)
            since = None if full else max(0, int(cursor) - _RESCAN_IDS)
            kinds = _entity_kinds(store)
            have = {r["item_ref"]: dict(r) for r in c.execute(
                "SELECT item_ref, event_id, hash, published FROM pointers"
                + ("" if since is None else " WHERE event_id > ?"),
                () if since is None else (since,)).fetchall()}
            seen: set[str] = set()
            changed = gone = 0
            max_id = int(cursor or 0)
            for row in _candidate_rows(store, since_id=since):
                max_id = max(max_id, int(row["id"]))
                try:
                    p = build(store, row, reg=reg, kinds=kinds,
                              unshared=unshared)
                except Exception as exc:
                    print(f"[team_index] pointer for event {row['id']} "
                          f"skipped ({exc}).")
                    continue
                if p is None:
                    continue
                seen.add(p["item_ref"])
                h = _hash(p)
                prev = have.get(p["item_ref"])
                if prev and prev["hash"] == h:
                    continue
                c.execute(
                    "INSERT OR REPLACE INTO pointers(item_ref, event_id, hash, "
                    "body, scope_id, published, built_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (p["item_ref"], int(row["id"]), h, json.dumps(p),
                     p["scope_id"], now))
                changed += 1
            # Scanned but no longer pointed at (privacy stamp rose, source
            # now excluded, event gone): out of the table, out of the index.
            for ref, prev in have.items():
                if ref in seen:
                    continue
                c.execute("DELETE FROM pointers WHERE item_ref=?", (ref,))
                gone += 1
                if prev["published"]:
                    c.execute("INSERT OR IGNORE INTO pending(op, arg, "
                              "queued_at) VALUES ('withdraw', ?, ?)",
                              (ref, now))
            _set(c, "cursor", max_id)
            _set(c, "full_due", False)
            _set(c, "shared_scopes", shared_now)
            _set(c, "last_sync", now)
            if full:
                _set(c, "last_full", now)
            c.commit()
            out.update(scanned=len(seen), changed=changed, removed=gone,
                       full=full)
            if m == "central":
                out["published"] = _publish_unpublished(c)
                c.commit()
                out["pending"] = flush_pending()
                if now - float(_get(c, "last_reconcile", 0) or 0) >= _RECONCILE_S:
                    out["reconcile"] = _reconcile(c, now)
                c.commit()
        finally:
            c.close()
    return out


def _publish_unpublished(c) -> int:
    from app.services import egress
    from app.services.records import org_client
    rows = [dict(r) for r in c.execute(
        "SELECT item_ref, body FROM pointers WHERE published=0 "
        "ORDER BY event_id DESC LIMIT ?", (_MAX_PER_TICK,)).fetchall()]
    sent = 0
    for i in range(0, len(rows), _BATCH):
        batch = rows[i:i + _BATCH]
        verdict = egress.decide(
            path="index", destination="org index", policy="allow",
            data_class="work", privacy_class="internal", text="",
            meta={"pointers": len(batch)})
        if not verdict.allowed:
            break
        try:
            org_client.publish_pointers([json.loads(r["body"]) for r in batch])
        except org_client.OrgRefused as exc:
            print(f"[team_index] publish refused ({exc}).")
            break
        except Exception as exc:
            print(f"[team_index] publish deferred ({exc}).")
            break
        c.executemany("UPDATE pointers SET published=1 WHERE item_ref=?",
                      [(r["item_ref"],) for r in batch])
        _set(c, "index_holds", True)
        sent += len(batch)
    return sent


def _reconcile(c, now: float) -> dict[str, Any]:
    from app.services.records import org_client
    keep = [r["item_ref"] for r in c.execute(
        "SELECT item_ref FROM pointers WHERE published=1").fetchall()]
    try:
        out = org_client.reconcile_pointers(keep)
    except Exception as exc:
        return {"error": str(exc)}
    _set(c, "last_reconcile", now)
    return {"withdrawn": out.get("withdrawn", 0), "kept": out.get("kept", 0)}


# -------------------------------------------------------- project sharing --
def set_project_shared(project: str, shared: bool) -> dict[str, Any]:
    """Share (the default for a shared project) or stop sharing this
    member's items in one project. Stopping takes their pointers there out
    of the index now; they come back as routing-only pointers next pass."""
    from app.services import projects
    hit = projects.resolve(project)
    if not hit or not hit.get("shared") or not hit.get("scope_id"):
        return {"ok": False, "error": "not a shared project"}
    sid = str(hit["scope_id"])
    with _lock, _open() as c:
        unshared = set(_get(c, "unshared", []) or [])
        if shared:
            unshared.discard(sid)
        else:
            unshared.add(sid)
            c.execute("UPDATE pointers SET hash='' WHERE scope_id=?", (sid,))
            if mode() == "central" or _get(c, "index_holds", False):
                c.execute("INSERT OR IGNORE INTO pending(op, arg, queued_at) "
                          "VALUES ('unshare', ?, ?)", (sid, time.time()))
        _set(c, "unshared", sorted(unshared))
        _set(c, "full_due", True)
    sent = flush_pending() if not shared else {"sent": 0, "failed": 0}
    return {"ok": True, "project": hit["name"], "scope_id": sid,
            "shared": shared, "withdrawal": sent}


# ---------------------------------------------------------- local search --
def local_search(query: str, *, reader: str, readable: set[str],
                 limit: int = 5, ask_limit: int = 1) -> dict[str, list]:
    """A teammate's search answered from this seat's own pointers, with the
    rule the central index uses: open only for shared projects both of us
    read, at most "ask me" for anything else."""
    from app.services.records import org_client
    me = org_client.member_id() or "me"
    mine = {s.get("id") for s in org_client.cached_scopes()
            if "read" in (s.get("permissions") or [])}
    unshared = unshared_scopes()
    with _lock, _open() as c:
        rows = [json.loads(r["body"]) for r in c.execute(
            "SELECT body FROM pointers").fetchall()]
    for p in rows:
        p["owner"] = me
        if p.get("scope_id") in unshared:
            p.update(P.without_text(p))
    return P.search(rows, query, reader=reader, readable=readable,
                    owner_can_read=lambda _o, sid: sid in mine,
                    limit=limit, ask_limit=ask_limit)


def status() -> dict[str, Any]:
    from app.services import egress
    with _lock, _open() as c:
        n = c.execute("SELECT COUNT(*) FROM pointers").fetchone()[0]
        pub = c.execute(
            "SELECT COUNT(*) FROM pointers WHERE published=1").fetchone()[0]
        shared = c.execute("SELECT COUNT(*) FROM pointers WHERE scope_id "
                           "IS NOT NULL").fetchone()[0]
        pend = c.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
        return {"setting": setting(), "mode": mode(), "rung": egress.rung(),
                "pointers": n, "in_shared_projects": shared, "published": pub,
                "pending": pend, "last_sync": _get(c, "last_sync"),
                "last_reconcile": _get(c, "last_reconcile"),
                "unshared": _get(c, "unshared", [])}
