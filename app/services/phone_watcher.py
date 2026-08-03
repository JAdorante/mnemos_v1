"""Proactive Phone Link watcher — notification -> offer -> act trigger.

When Windows surfaces an iPhone notification via Phone Link, this offers (in
chat) to reply or open the thread. The user replies yes/no; on yes, the goal is
dispatched with surface=phone_link.

Disable with QUILL_PHONE_WATCH=0 (or QUILL_AGENT=0).
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

from app.events import Modality

_recent: dict[str, float] = {}
_lock = threading.Lock()
_COOLDOWN_S = 120
_REPLY_HINT = re.compile(r"\b(reply|respond|text back|message)\b", re.I)


def _enabled() -> bool:
    return (os.environ.get("QUILL_PHONE_WATCH", "1") not in ("0", "false", "False")
            and os.environ.get("QUILL_AGENT") not in ("0", "false", "False")
            and os.environ.get("QUILL_PHONE_LINK", "1") not in ("0", "false", "False"))


def _hash(body: str) -> str:
    return hashlib.sha1((body or "").strip().lower().encode()).hexdigest()



def _on_event(ev) -> None:
    try:
        if getattr(ev, "modality", None) != Modality.NOTIFICATION:
            return
        meta = ev.meta or {}
        src = (getattr(ev, "source", None) or meta.get("source") or "").lower()
        if src and not src.startswith("notifications"):
            app = (meta.get("app") or "").lower()
            if "phone" not in app and "link" not in app:
                return
        body = (ev.raw or ev.summary or "").strip()
        if not body or not _enabled():
            return

        h = _hash(body)
        now = time.time()
        with _lock:
            last = _recent.get(h)
            if last is not None and now - last < _COOLDOWN_S:
                return
            _recent[h] = now

        goal = f"Reply to this iPhone notification: {body}"
        if not _REPLY_HINT.search(body):
            goal = f"Open Phone Link and show messages related to: {body}"

        from app.services.agent_bridge import worker

        offered = worker.propose_phone(goal, body)
        if offered:
            print(f"[phone] offered notification in chat — reply yes/no.")
    except Exception as exc:
        print(f"[phone] watcher error: {exc}")


def attach() -> None:
    from app.events import bus

    bus.subscribe(_on_event)
    print("[phone] watching Phone Link notifications (offers to act via chat).")
