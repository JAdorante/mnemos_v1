"""Chat log — one SQLite file, WAL, separate from the memory database.

The memory database already owns ``sessions``, ``events``, and ``jobs``.
This file is ``$QUILL_DATA_DIR/chat_log.db`` and uses a ``chat_`` prefix:

    sessions -> chat_sessions
    events   -> chat_events
    jobs     -> chat_jobs
    offers   -> chat_offers

The live thread is the row ``chat_meta.live_session_id`` points at.
``POST /chat`` inserts the user event before it returns (``client_msg_id``
dedupes retries). Worker emits insert only when their generation still
equals ``chat_sessions.current_gen``. ``new()`` bumps that generation,
marks running jobs cancelled, and opens a new session. Progress lines live
on the job row, not in the transcript.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id                  TEXT    PRIMARY KEY,
    created_at          REAL    NOT NULL,
    current_gen         INTEGER NOT NULL DEFAULT 1,
    study_mode          TEXT,
    study_mode_expires  REAL,
    connectors_json     TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS chat_events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT    NOT NULL REFERENCES chat_sessions(id),
    turn_id        TEXT,
    gen            INTEGER NOT NULL,
    kind           TEXT    NOT NULL,
    text           TEXT,
    payload_json   TEXT,
    created_at     REAL    NOT NULL,
    client_msg_id  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS chat_events_client_msg
    ON chat_events(session_id, client_msg_id)
    WHERE client_msg_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS chat_events_session_seq
    ON chat_events(session_id, seq);

CREATE TABLE IF NOT EXISTS chat_jobs (
    id                TEXT    PRIMARY KEY,
    session_id        TEXT    NOT NULL REFERENCES chat_sessions(id),
    turn_id           TEXT,
    gen               INTEGER NOT NULL,
    lane              TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    progress_json     TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS chat_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_offers (
    id            TEXT    PRIMARY KEY,
    session_id    TEXT    NOT NULL REFERENCES chat_sessions(id),
    turn_id       TEXT,
    payload_hash  TEXT    NOT NULL,
    expires_at    REAL    NOT NULL,
    state         TEXT    NOT NULL,
    payload_json  TEXT    NOT NULL DEFAULT '{}',
    created_at    REAL    NOT NULL DEFAULT 0,
    verdict       TEXT,
    resolved_at   REAL
);

CREATE INDEX IF NOT EXISTS chat_offers_open
    ON chat_offers(session_id, state, expires_at);
"""

# Insert only when the caller's generation is still the session's generation.
# changes() == 0 means the result was stale and must be dropped.
INSERT_IF_CURRENT_SQL = """
INSERT INTO chat_events (
    session_id, turn_id, gen, kind, text, payload_json, created_at, client_msg_id
)
SELECT ?, ?, s.current_gen, ?, ?, ?, ?, ?
FROM chat_sessions s
WHERE s.id = ? AND s.current_gen = ?
"""


def apply(conn: sqlite3.Connection) -> None:
    """Create the chat-log tables and enable WAL. Does not touch memory tables."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_jobs)")}
    if "progress_json" not in cols:
        conn.execute(
            "ALTER TABLE chat_jobs ADD COLUMN progress_json TEXT NOT NULL DEFAULT '[]'")
    offer_cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_offers)")}
    if "payload_json" not in offer_cols:
        conn.execute(
            "ALTER TABLE chat_offers ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")
    if "created_at" not in offer_cols:
        conn.execute(
            "ALTER TABLE chat_offers ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
    if "verdict" not in offer_cols:
        conn.execute("ALTER TABLE chat_offers ADD COLUMN verdict TEXT")
    if "resolved_at" not in offer_cols:
        conn.execute("ALTER TABLE chat_offers ADD COLUMN resolved_at REAL")
    conn.commit()


def insert_if_current(
    conn: sqlite3.Connection, *,
    session_id: str,
    turn_id: str | None,
    gen: int,
    kind: str,
    text: str | None,
    payload_json: str | None,
    created_at: float,
    client_msg_id: str | None,
) -> int | None:
    """Write one event if ``gen`` is still current. Returns seq, or None if stale."""
    cur = conn.execute(
        INSERT_IF_CURRENT_SQL,
        (
            session_id, turn_id, kind, text, payload_json, created_at,
            client_msg_id, session_id, int(gen),
        ),
    )
    conn.commit()
    if cur.rowcount != 1:
        return None
    return int(cur.lastrowid)


_PROGRESS_CAP = 16
_PAYLOAD_KEYS = ("distill_id", "sources", "packet", "compiled", "stream", "lane")
_OFFER_HASH_KEYS = (
    "kind", "title", "message", "items", "fact_id", "choices",
    "peer_id", "peer_name", "reasoner", "meeting_session_id",
    "confidence", "deliverable_only", "options", "default_retention",
)


def _new_id() -> str:
    return uuid.uuid4().hex


def offer_payload_hash(offer: dict) -> str:
    """Stable hash of the fields a Yes/No click is authorizing."""
    body = {k: offer.get(k) for k in _OFFER_HASH_KEYS
            if offer.get(k) is not None}
    try:
        from app.services.records.canonical import canonical_hash
        return canonical_hash(body)
    except Exception:
        import hashlib
        raw = json.dumps(body, sort_keys=True, ensure_ascii=False,
                         default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _slug_title(text: str, limit: int = 72) -> str:
    import re
    one = re.sub(r"\s+", " ", (text or "").strip())
    if not one:
        return "Untitled chat"
    if len(one) <= limit:
        return one
    return one[: limit - 1].rstrip() + "…"


class ChatLog:
    """Process-wide chat sessions. One connection, check_same_thread=False."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        apply(self._conn)
        self._hydrated = False
        with self._lock:
            self._ensure_live()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def live_id(self) -> str:
        with self._lock:
            return self._live_id()

    def live(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._session(self._live_id()) or {})

    def accept_turn(self, *, text: str, session_id: str | None,
                    client_msg_id: str | None, lane: str = "chat") -> dict[str, Any]:
        """Insert the user event and a running job. Idempotent on client_msg_id.

        A session_id that is not the live thread returns ``switched`` and
        writes nothing — the caller reloads onto the live session.
        """
        with self._lock:
            live = self._live_id()
            if session_id and session_id != live:
                return {"switched": True, "session_id": live}
            sid = live
            sess = self._session(sid)
            if sess is None:
                sid = self._ensure_live()
                sess = self._session(sid)
            assert sess is not None
            gen = int(sess["current_gen"])
            if client_msg_id:
                prior = self._conn.execute(
                    "SELECT seq, turn_id, gen FROM chat_events "
                    "WHERE session_id=? AND client_msg_id=?",
                    (sid, client_msg_id),
                ).fetchone()
                if prior is not None:
                    return {
                        "duplicate": True,
                        "seq": int(prior["seq"]),
                        "session_id": sid,
                        "turn_id": prior["turn_id"],
                        "gen": int(prior["gen"]),
                        "job_id": None,
                    }
            turn_id = _new_id()
            job_id = _new_id()
            now = time.time()
            try:
                seq = insert_if_current(
                    self._conn, session_id=sid, turn_id=turn_id, gen=gen,
                    kind="user", text=text, payload_json=None, created_at=now,
                    client_msg_id=client_msg_id or None,
                )
            except sqlite3.IntegrityError:
                prior = self._conn.execute(
                    "SELECT seq, turn_id, gen FROM chat_events "
                    "WHERE session_id=? AND client_msg_id=?",
                    (sid, client_msg_id),
                ).fetchone()
                if prior is None:
                    raise
                return {
                    "duplicate": True,
                    "seq": int(prior["seq"]),
                    "session_id": sid,
                    "turn_id": prior["turn_id"],
                    "gen": int(prior["gen"]),
                    "job_id": None,
                }
            if seq is None:
                return {"dropped": True, "session_id": sid}
            self._conn.execute(
                "INSERT INTO chat_jobs "
                "(id, session_id, turn_id, gen, lane, status, cancel_requested, progress_json) "
                "VALUES (?, ?, ?, ?, ?, 'running', 0, '[]')",
                (job_id, sid, turn_id, gen, lane),
            )
            event = {
                "id": seq, "kind": "user", "text": text,
                "turn_id": turn_id, "session_id": sid,
            }
            return {
                "duplicate": False,
                "seq": seq,
                "session_id": sid,
                "turn_id": turn_id,
                "gen": gen,
                "job_id": job_id,
                "event": event,
            }

    def emit_event(self, *, session_id: str, gen: int, kind: str, text: str,
                   turn_id: str | None = None, payload: dict | None = None,
                   ) -> dict[str, Any]:
        """Conditional insert. ``dropped`` when gen is stale. ``duplicate`` when
        this turn already has its user row (the request path wrote it)."""
        with self._lock:
            if kind == "user" and turn_id:
                prior = self._conn.execute(
                    "SELECT seq FROM chat_events "
                    "WHERE session_id=? AND turn_id=? AND kind='user' "
                    "ORDER BY seq LIMIT 1",
                    (session_id, turn_id),
                ).fetchone()
                if prior is not None:
                    return {"duplicate": True, "seq": int(prior["seq"])}
            raw = None
            if payload:
                kept = {k: payload[k] for k in _PAYLOAD_KEYS
                        if payload.get(k) is not None}
                if kept:
                    raw = json.dumps(kept, ensure_ascii=False)
            seq = insert_if_current(
                self._conn, session_id=session_id, turn_id=turn_id, gen=gen,
                kind=kind, text=text, payload_json=raw, created_at=time.time(),
                client_msg_id=None,
            )
            if seq is None:
                return {"dropped": True}
            return {"seq": seq}

    def note_progress(self, job_id: str | None, line: str) -> None:
        if not job_id or not (line or "").strip():
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT progress_json FROM chat_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            try:
                lines = json.loads(row["progress_json"] or "[]")
            except json.JSONDecodeError:
                lines = []
            if not isinstance(lines, list):
                lines = []
            lines.append(line)
            self._conn.execute(
                "UPDATE chat_jobs SET progress_json=? WHERE id=?",
                (json.dumps(lines[-_PROGRESS_CAP:], ensure_ascii=False), job_id),
            )

    def progress_state(self, session_id: str) -> tuple[str | None, list[str]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, progress_json FROM chat_jobs "
                "WHERE session_id=? AND status='running' "
                "ORDER BY rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                return None, []
            try:
                lines = json.loads(row["progress_json"] or "[]")
            except json.JSONDecodeError:
                lines = []
            if not isinstance(lines, list):
                lines = []
            return str(row["id"]), [str(x) for x in lines]

    def set_lane(self, job_id: str | None, lane: str) -> None:
        if not job_id or not lane:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE chat_jobs SET lane=? WHERE id=?",
                (lane, job_id),
            )

    def running_jobs(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, lane, progress_json FROM chat_jobs "
                "WHERE session_id=? AND status='running' ORDER BY rowid",
                (session_id,),
            ).fetchall()
        out = []
        for row in rows:
            try:
                lines = json.loads(row["progress_json"] or "[]")
            except json.JSONDecodeError:
                lines = []
            if not isinstance(lines, list):
                lines = []
            out.append({
                "id": str(row["id"]),
                "lane": row["lane"] or "chat",
                "progress": [str(x) for x in lines],
            })
        return out

    def finish_job(self, job_id: str | None, status: str = "done") -> None:
        if not job_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE chat_jobs SET status=? "
                "WHERE id=? AND cancel_requested=0 AND status='running'",
                (status, job_id),
            )

    def cancel_requested(self, job_id: str | None) -> bool:
        if not job_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM chat_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            return bool(row and row["cancel_requested"])

    def mint_offer(self, offer: dict, *, ttl_s: float) -> dict[str, Any]:
        """Insert an open offer on the live session. Returns id + hash + expiry."""
        oid = _new_id()
        now = time.time()
        expires = now + max(1.0, float(ttl_s))
        phash = offer_payload_hash(offer)
        body = dict(offer)
        body["offer_id"] = oid
        body["payload_hash"] = phash
        body["expires_at"] = expires
        body.setdefault("created_at", now)
        with self._lock:
            sid = self._live_id()
            self._conn.execute(
                "INSERT INTO chat_offers "
                "(id, session_id, turn_id, payload_hash, expires_at, state, "
                " payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                (oid, sid, offer.get("turn_id"), phash, expires,
                 json.dumps(body, ensure_ascii=False, default=str), now),
            )
        return {
            "offer_id": oid,
            "payload_hash": phash,
            "expires_at": expires,
            "created_at": now,
            "payload": body,
        }

    def resolve_offer(self, offer_id: str, payload_hash: str,
                      verdict: str) -> dict[str, Any]:
        """Compare-and-set: open and unexpired only. One successful write."""
        oid = (offer_id or "").strip()
        phash = (payload_hash or "").strip()
        if not oid or not phash:
            return {"ok": False, "error": "offer_id and payload_hash required"}
        state = "accepted" if verdict in ("accepted", "yes", "true", "1") else (
            "declined" if verdict in ("declined", "no", "false", "0") else (
                "cancelled" if verdict == "cancelled" else str(verdict or "resolved")))
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE chat_offers SET state=?, verdict=?, resolved_at=? "
                "WHERE id=? AND payload_hash=? AND state='open' AND expires_at>?",
                (state, state, now, oid, phash, now),
            )
            if cur.rowcount != 1:
                row = self._conn.execute(
                    "SELECT state, payload_hash, expires_at FROM chat_offers "
                    "WHERE id=?",
                    (oid,),
                ).fetchone()
                if row is None:
                    return {"ok": False, "error": "not_found"}
                if str(row["payload_hash"] or "") != phash:
                    return {"ok": False, "error": "payload_hash_mismatch"}
                if str(row["state"] or "") != "open":
                    return {"ok": False, "error": "already_resolved",
                            "state": row["state"]}
                if float(row["expires_at"] or 0) <= now:
                    return {"ok": False, "error": "expired"}
                return {"ok": False, "error": "refused"}
            row = self._conn.execute(
                "SELECT payload_json FROM chat_offers WHERE id=?",
                (oid,),
            ).fetchone()
        try:
            payload = json.loads(row["payload_json"] or "{}") if row else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {"ok": True, "offer": payload, "verdict": state,
                "offer_id": oid, "payload_hash": phash}

    def expire_due(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """CAS-expire every open offer past its deadline. Returns the payloads."""
        now = float(now if now is not None else time.time())
        out: list[dict[str, Any]] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload_json FROM chat_offers "
                "WHERE state='open' AND expires_at<=?",
                (now,),
            ).fetchall()
            for row in rows:
                cur = self._conn.execute(
                    "UPDATE chat_offers SET state='expired', resolved_at=? "
                    "WHERE id=? AND state='open'",
                    (now, row["id"]),
                )
                if cur.rowcount != 1:
                    continue
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                if isinstance(payload, dict):
                    payload.setdefault("offer_id", row["id"])
                    out.append(payload)
        return out

    def open_offers(self, session_id: str | None = None,
                    *, now: float | None = None) -> list[dict[str, Any]]:
        now = float(now if now is not None else time.time())
        with self._lock:
            sid = session_id or self._live_id()
            rows = self._conn.execute(
                "SELECT id, payload_hash, expires_at, payload_json, created_at "
                "FROM chat_offers "
                "WHERE session_id=? AND state='open' AND expires_at>? "
                "ORDER BY created_at, id",
                (sid, now),
            ).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload["offer_id"] = row["id"]
            payload["payload_hash"] = row["payload_hash"]
            payload["expires_at"] = float(row["expires_at"])
            payload["created_at"] = float(row["created_at"] or 0)
            out.append(payload)
        return out

    def cancel_open_offers(self, session_id: str | None = None) -> int:
        with self._lock:
            sid = session_id or self._live_id()
            cur = self._conn.execute(
                "UPDATE chat_offers SET state='cancelled', resolved_at=? "
                "WHERE session_id=? AND state='open'",
                (time.time(), sid),
            )
            return int(cur.rowcount or 0)

    def retire(self) -> dict[str, Any]:
        """Bump the live generation, cancel its jobs, open a new live session.

        Returns archive meta when the old session had user turns, else {}.
        """
        with self._lock:
            old_id = self._live_id()
            rows = self._conn.execute(
                "SELECT kind, text FROM chat_events WHERE session_id=? ORDER BY seq",
                (old_id,),
            ).fetchall()
            n_turns = sum(1 for r in rows if r["kind"] == "user")
            first = next((r["text"] for r in rows if r["kind"] == "user"), "")
            self._conn.execute(
                "UPDATE chat_sessions SET current_gen = current_gen + 1 WHERE id=?",
                (old_id,),
            )
            self._conn.execute(
                "UPDATE chat_jobs SET cancel_requested=1 "
                "WHERE session_id=? AND status='running'",
                (old_id,),
            )
            self._conn.execute(
                "UPDATE chat_offers SET state='cancelled', resolved_at=? "
                "WHERE session_id=? AND state='open'",
                (time.time(), old_id),
            )
            meta: dict[str, Any] = {}
            if n_turns:
                meta = {
                    "id": old_id,
                    "title": _slug_title(str(first or "")),
                    "n_turns": n_turns,
                    "n_events": len(rows),
                    "saved_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            new_id = self._create_session()
            self._set_live(new_id)
            return meta

    def save_study(self, mode_id: str | None, until: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE chat_sessions SET study_mode=?, study_mode_expires=? "
                "WHERE id=?",
                (mode_id or "general", until, self._live_id()),
            )

    def save_connectors(self, overrides: dict | None) -> None:
        raw = json.dumps(overrides or {"enabled": [], "disabled": []},
                         ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "UPDATE chat_sessions SET connectors_json=? WHERE id=?",
                (raw, self._live_id()),
            )

    def events_since(self, session_id: str, since: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, text, payload_json FROM chat_events "
                "WHERE session_id=? AND seq>=? ORDER BY seq",
                (session_id, int(since)),
            ).fetchall()
        return [_event_view(r) for r in rows]

    def list_sessions(self, limit: int = 50) -> list[dict]:
        """Previous threads (not the live one), newest first."""
        with self._lock:
            live = self._live_id()
            rows = self._conn.execute(
                "SELECT s.id, s.created_at, "
                "(SELECT text FROM chat_events e "
                " WHERE e.session_id=s.id AND e.kind='user' "
                " ORDER BY e.seq LIMIT 1) AS title, "
                "(SELECT COUNT(*) FROM chat_events e "
                " WHERE e.session_id=s.id AND e.kind='user') AS n_turns, "
                "(SELECT COUNT(*) FROM chat_events e "
                " WHERE e.session_id=s.id) AS n_events "
                "FROM chat_sessions s WHERE s.id!=? "
                "ORDER BY s.created_at DESC LIMIT ?",
                (live, int(limit)),
            ).fetchall()
        out = []
        for r in rows:
            if not r["n_turns"]:
                continue
            out.append({
                "id": r["id"],
                "title": _slug_title(str(r["title"] or "")),
                "n_turns": int(r["n_turns"] or 0),
                "n_events": int(r["n_events"] or 0),
                "saved_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(r["created_at"]))),
            })
        return out

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        sid = (session_id or "").strip()
        if not sid or "/" in sid or "\\" in sid or ".." in sid:
            return None
        with self._lock:
            sess = self._session(sid)
            if sess is None:
                return None
            rows = self._conn.execute(
                "SELECT seq, kind, text, payload_json FROM chat_events "
                "WHERE session_id=? ORDER BY seq",
                (sid,),
            ).fetchall()
        events = [_event_view(r) for r in rows]
        first = next((e["text"] for e in events if e["kind"] == "user"), "")
        return {
            "id": sid,
            "title": _slug_title(str(first or "")),
            "n_turns": sum(1 for e in events if e["kind"] == "user"),
            "n_events": len(events),
            "events": events,
        }

    def hydrate_runtime(self) -> None:
        """Copy the live session's study mode and connector toggles into memory.

        Once per process. Later writes go session-row first, then memory.
        """
        with self._lock:
            if self._hydrated:
                return
            self._hydrated = True
            row = self._session(self._live_id())
        if not row:
            return
        try:
            from app.services.connectors import session as conn_session
            raw = json.loads(row["connectors_json"] or "{}")
            if isinstance(raw, dict):
                conn_session.load_overrides(raw)
        except Exception:
            pass
        mode = (row["study_mode"] or "").strip()
        expires = row["study_mode_expires"]
        if not mode or mode == "general":
            return
        if expires is not None and float(expires) < time.time():
            return
        try:
            from app.services import agent_chat_mode as smode
            smode.set_manual(mode)
        except Exception:
            pass

    # --- internals (caller holds _lock) ------------------------------------
    def _live_id(self) -> str:
        row = self._conn.execute(
            "SELECT value FROM chat_meta WHERE key='live_session_id'"
        ).fetchone()
        if row and row["value"]:
            return str(row["value"])
        return self._ensure_live()

    def _ensure_live(self) -> str:
        row = self._conn.execute(
            "SELECT value FROM chat_meta WHERE key='live_session_id'"
        ).fetchone()
        if row and row["value"] and self._session(str(row["value"])):
            return str(row["value"])
        sid = self._create_session()
        self._set_live(sid)
        return sid

    def _create_session(self) -> str:
        sid = _new_id()
        self._conn.execute(
            "INSERT INTO chat_sessions "
            "(id, created_at, current_gen, study_mode, connectors_json) "
            "VALUES (?, ?, 1, 'general', '{}')",
            (sid, time.time()),
        )
        return sid

    def _set_live(self, sid: str) -> None:
        self._conn.execute(
            "INSERT INTO chat_meta (key, value) VALUES ('live_session_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (sid,),
        )

    def _session(self, sid: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM chat_sessions WHERE id=?", (sid,)
        ).fetchone()


def _event_view(row: sqlite3.Row) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "id": int(row["seq"]),
        "kind": row["kind"],
        "text": row["text"] or "",
    }
    if row["payload_json"]:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            for key in _PAYLOAD_KEYS:
                if payload.get(key) is not None:
                    ev[key] = payload[key]
    return ev


_singleton: ChatLog | None = None
_singleton_lock = threading.Lock()


def get_log() -> ChatLog:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            from app.config import settings
            path = Path(settings.storage.data_dir) / "chat_log.db"
            _singleton = ChatLog(path)
            _singleton.hydrate_runtime()
        return _singleton


def close_log() -> None:
    """Drop the singleton so the next get_log() opens the default file."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            try:
                _singleton.close()
            except Exception:
                pass
            _singleton = None


def reset_log(path: Path | None = None) -> ChatLog:
    """Test hook. Closes the singleton and opens ``path`` (or the default)."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            try:
                _singleton.close()
            except Exception:
                pass
            _singleton = None
        if path is None:
            return get_log()
        _singleton = ChatLog(path)
        return _singleton
