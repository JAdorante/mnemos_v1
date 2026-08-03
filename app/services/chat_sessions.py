"""Durable chat-session archive — past conversations when the user starts a new one.

Live chat bubbles live in `AgentWorker.events` (in-memory). Starting a new
conversation snapshots that log under `$QUILL_DATA_DIR/chat_sessions/` so the
thread is not lost, then the live log is cleared for a fresh start.

Index:  data/chat_sessions/index.jsonl   (one meta line per archive, newest last)
Bodies: data/chat_sessions/<id>.json     (events + title)
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings

_lock = threading.Lock()

# Persist these event fields; drop bulky compiled docs (text is enough to replay).
_KEEP = ("id", "kind", "text", "distill_id", "sources", "packet")


def sessions_dir() -> Path:
    return Path(settings.storage.data_dir) / "chat_sessions"


def _index_path() -> Path:
    return sessions_dir() / "index.jsonl"


def _slug_title(text: str, limit: int = 72) -> str:
    one = re.sub(r"\s+", " ", (text or "").strip())
    if not one:
        return "Untitled chat"
    if len(one) <= limit:
        return one
    return one[: limit - 1].rstrip() + "…"


def _slim_event(ev: dict) -> dict:
    out = {k: ev[k] for k in _KEEP if k in ev and ev[k] is not None}
    return out


def worth_archiving(events: list[dict]) -> bool:
    """Skip empty / noise-only logs (boot system lines, no real turns)."""
    return any(e.get("kind") in ("user", "result", "ask") for e in events)


def archive_events(events: list[dict]) -> dict | None:
    """Write a chat archive. Returns meta dict, or None if nothing to save."""
    if not worth_archiving(events):
        return None
    slim = [_slim_event(e) for e in events]
    first_user = next((e for e in slim if e.get("kind") == "user"), None)
    title = _slug_title(str((first_user or {}).get("text") or ""))
    now = datetime.now(timezone.utc)
    sid = f"{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    meta = {
        "id": sid,
        "saved_at": now.isoformat(),
        "title": title,
        "n_events": len(slim),
        "n_turns": sum(1 for e in slim if e.get("kind") == "user"),
    }
    body = {**meta, "events": slim}
    root = sessions_dir()
    with _lock:
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{sid}.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        with _index_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    return meta


def list_sessions(limit: int = 50) -> list[dict]:
    """Newest first. Meta only (no event bodies)."""
    path = _index_path()
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(rows) >= max(1, limit):
            break
    return rows


def load_session(session_id: str) -> dict[str, Any] | None:
    """Load a full archived session by id, or None if missing/invalid."""
    sid = (session_id or "").strip()
    if not sid or "/" in sid or "\\" in sid or ".." in sid:
        return None
    path = sessions_dir() / f"{sid}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data
