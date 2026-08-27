"""Web/agent research answers → memory (testing-first writeback).

Saved chats are history, not recall. Without this, a research answer about
someone lives only in the archived thread — a later "using only your memory"
question cannot find it. This module mirrors chat_ingest / peer answer ingest:

  1. store the answer as a Modality.TEXT event (source=chat.research)
  2. extract facts through the same hygiene gate
  3. default SYNC so the next memory question in a test loop sees the facts

Gated to hands results that actually left the memory-only path (browser /
desktop / phone). Pure `answered_no_browser` replies are skipped — those are
already grounded in existing memory.

Off switch: QUILL_RESEARCH_INGEST=0.
Async (production-like): QUILL_RESEARCH_INGEST_SYNC=0.
"""
from __future__ import annotations

import os
import time
from typing import Any

SOURCE = "chat.research"

# Research digests are long; short stubs are refusals / status lines.
MIN_CHARS = int(os.getenv("QUILL_RESEARCH_INGEST_MIN_CHARS", "40"))

# Hands outcomes that should not teach memory (no real research payload).
_SKIP_STATUS = frozenset({
    "answered_no_browser",
    "plan_only",
    "error",
    "blocked",
    "cancelled",
    "desktop_unavailable",
    "stopped_user",
})

_SKIP_PREFIXES = (
    "(no answer",
    "refused:",
    "okay, i won't",
    "desktop and phone tasks",
)


def enabled() -> bool:
    """Default ON — testing loops need writeback without a Helpful click."""
    return os.getenv("QUILL_RESEARCH_INGEST", "1") not in ("0", "false", "False")


def sync_mode() -> bool:
    """Default ON so a follow-up memory question in the same test sees facts."""
    return os.getenv("QUILL_RESEARCH_INGEST_SYNC", "1") not in ("0", "false", "False")


def ingestable(text: str, *, status: str | None = None,
               route: dict | None = None) -> str | None:
    """Cleaned answer text to remember, or None when this result isn't
    research material worth writing back."""
    if not enabled():
        return None
    st = (status or "").strip().lower()
    if st in _SKIP_STATUS:
        return None
    r = route or {}
    # Memory-only direct answers: skip even if status is odd/missing.
    if r and r.get("requires_browser") is False and (
            (r.get("surface") or "").lower() in ("", "none", "direct_answer")):
        return None
    body = (text or "").strip()
    if len(body) < MIN_CHARS:
        return None
    low = body.lower()
    if any(low.startswith(p) for p in _SKIP_PREFIXES):
        return None
    return body


def ingest(text: str, *, status: str | None = None,
           route: dict | None = None,
           question: str | None = None) -> int | None:
    """Store one research answer and extract (sync) or queue extraction.
    Returns the event id, or None when skipped. Best-effort — never raises
    into the chat path."""
    body = ingestable(text, status=status, route=route)
    if body is None:
        return None
    try:
        from app.events import Event, Modality
        from app.services import confidence as _conf
        from app.services.attachments import _index_event
        from app.storage import get_store

        now = time.time()
        meta: dict[str, Any] = {"section": "chat", "origin": "research"}
        if status:
            meta["agent_status"] = status
        if route:
            meta["route"] = {
                k: route.get(k) for k in ("intent", "surface", "requires_browser")
                if route.get(k) is not None
            }
        if (question or "").strip():
            meta["question"] = question.strip()[:300]

        ev = Event(
            time=now, modality=Modality.TEXT, raw=body,
            summary=f"[research] {body[:120]}", source=SOURCE,
            meta=meta,
        )
        # Web-derived: extracted, not accepted — user didn't vouch for it.
        _conf.attach(ev, _conf.EXTRACTED, model=0.7)
        anchor = get_store().insert(ev)
        _index_event(anchor, ev)

        payload = {"event_id": anchor, "text": body, "source": SOURCE}
        if sync_mode():
            run_job(payload)
        else:
            from app.services.worker import worker
            worker.enqueue("research_ingest", payload=payload)
        return anchor
    except Exception as exc:
        print(f"[research_ingest] skipped ({exc}).")
        return None


def run_job(payload: dict | None) -> None:
    """Mine one research answer for facts (same extractor + hygiene as chat)."""
    text = (payload or {}).get("text") or ""
    anchor = (payload or {}).get("event_id")
    if not text:
        return
    from app.services.documents import _persist_facts
    from app.services.extractor import extractor
    from app.services.worker import worker
    from app.storage import get_store

    store = get_store()
    now = time.time()
    event_source = (payload or {}).get("source") or SOURCE
    if anchor is not None:
        try:
            ev = store.get_event(int(anchor))
            if ev and (ev.get("source") or "").strip():
                event_source = str(ev["source"]).strip()
        except Exception:
            pass
    facts = extractor._extract_text(text)
    n = _persist_facts(store, facts, anchor, text, now,
                       event_source=event_source)
    try:
        if anchor is not None:
            store.mark_extracted([anchor], now)
    except Exception:
        pass
    if n:
        print(f"[research_ingest] {n} fact(s) from research answer.")
        worker.enqueue("graph", unique=True)
