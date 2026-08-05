"""Typed chat → memory. Tell mnemos something in the chat box and it lands in
the same memory the ears feed.

Until now only *spoken* words (and ingested documents) became facts — a typed
"I met Sarah Chen, she runs platform at Foundry" vanished after the reply. Now
every substantive typed turn is:

  1. stored as a Modality.TEXT event (source=chat.user) — provenance, timeline
  2. queued for the SAME extractor the audio pipeline uses (durable jobs
     table, so a crash mid-extract just re-runs) — tasks / commitments /
     claims / people / entities / relations
  3. persisted through the write-time hygiene gate (dedup / supersede /
     confidence floor), then chained into graph.rebuild — so the constellation
     picks the new node up on its next live poll

Typed statements carry the ACCEPTED confidence tier: the human said it
directly, no capture noise, no extraction guesswork about who spoke.

Skips: empty/short messages (approvals, "yes", "go"), slash-command prefixes.
Questions are NOT skipped — "can you note that I moved the demo to Friday?"
is a question that carries a fact; the extractor's precision-first prompt
returns nothing for pure questions anyway. Off switch: QUILL_CHAT_INGEST=0.

Generic code: what gets remembered comes from what THIS user types.
"""
from __future__ import annotations

import os
import time

# Substantive-statement floor: below this it's an approval / verdict / nudge
# ("yes", "go", "cancel"), which teaches nothing and would waste an LLM call.
MIN_CHARS = int(os.getenv("QUILL_CHAT_INGEST_MIN_CHARS", "15"))


def enabled() -> bool:
    return os.getenv("QUILL_CHAT_INGEST", "1") not in ("0", "false", "False")


def ingestable(message: str) -> str | None:
    """The cleaned text to remember, or None when this turn isn't memory
    material. Pure — the cheap pre-filter in front of the event write."""
    text = (message or "").strip()
    if text.startswith("/"):  # dry-run prefix ("/plan open x") — drop the verb
        parts = text.split(None, 1)
        text = parts[1].strip() if len(parts) > 1 else ""
    if len(text) < MIN_CHARS:
        return None
    return text


def ingest(message: str) -> int | None:
    """Store one typed chat turn as a TEXT event and queue extraction.
    Returns the event id, or None when skipped. Best-effort: any failure
    is logged, never raised into the chat path."""
    if not enabled():
        return None
    text = ingestable(message)
    if text is None:
        return None
    try:
        from app.events import Event, Modality
        from app.services import confidence as _conf
        from app.services.attachments import _index_event
        from app.storage import get_store

        now = time.time()
        ev = Event(
            time=now, modality=Modality.TEXT, raw=text,
            summary=f"[chat] {text[:120]}", source="chat.user",
            meta={"section": "chat"},
        )
        _conf.attach(ev, _conf.ACCEPTED, capture=1.0)
        anchor = get_store().insert(ev)
        _index_event(anchor, ev)

        from app.services.worker import worker
        worker.enqueue("chat_ingest", payload={"event_id": anchor,
                                               "text": text})
        return anchor
    except Exception as exc:
        print(f"[chat_ingest] skipped ({exc}).")
        return None


def run_job(payload: dict) -> None:
    """Job handler: mine one stored chat turn for facts (same extractor, same
    hygiene gate as speech), then chain a graph rebuild so the constellation's
    live poll sees the new nodes."""
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
    # Typed chat is the enrolled user speaking — label so owner='me' maps to self.
    speaker = None
    try:
        from app.services.identity import user_identity
        speaker = (user_identity(store).get("name") or "").strip() or None
    except Exception:
        pass
    facts = extractor._extract_text(text, speaker=speaker)
    n = _persist_facts(store, facts, anchor, text, now)
    try:
        if anchor is not None:
            store.mark_extracted([anchor], now)
    except Exception:
        pass
    if n:
        print(f"[chat_ingest] {n} fact(s) from typed chat.")
        worker.enqueue("graph", unique=True)
