"""Typed chat → memory. Tell mnemos something in the chat box and it lands in
the same memory the ears feed.

Every substantive typed turn is remembered *after the turn resolves* (so a
failed send never becomes a claim), with the assistant answer stored as
context:

  1. stored as a Modality.TEXT event — provenance, timeline
  2. statements (source=chat.user) are queued for the SAME extractor the
     audio pipeline uses — tasks / commitments / claims / people / entities /
     relations
  3. questions are tagged as queries (source=chat.query) and are NOT mined
     for facts, so "is Dana leaving?" cannot become a claim
  4. persisted through the write-time hygiene gate, then chained into
     graph.rebuild

Typed statements carry the ACCEPTED confidence tier: the human said it
directly, no capture noise, no extraction guesswork about who spoke.

Skips: empty/short messages (approvals, "yes", "go"), slash-command prefixes,
per-message off-the-record. Off switch: QUILL_CHAT_INGEST=0.

Generic code: what gets remembered comes from what THIS user types.
"""
from __future__ import annotations

import os
import re
import time

# Substantive-statement floor: below this it's an approval / verdict / nudge
# ("yes", "go", "cancel"), which teaches nothing and would waste an LLM call.
MIN_CHARS = int(os.getenv("QUILL_CHAT_INGEST_MIN_CHARS", "15"))

_QUESTION_START = re.compile(
    r"^(?:"
    r"who|what|when|where|why|how|which|whose|"
    r"is|are|was|were|am|do|does|did|"
    r"can|could|would|will|should|may|might|"
    r"tell me|do you know|have you|did you|are you|is there|are there"
    r")\b",
    re.I,
)


def enabled() -> bool:
    return os.getenv("QUILL_CHAT_INGEST", "1") not in ("0", "false", "False")


def looks_like_question(text: str) -> bool:
    """Heuristic: trailing '?' or a leading interrogative / request-to-answer."""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return bool(_QUESTION_START.match(t))


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


def ingest(
    message: str,
    *,
    answer: str | None = None,
    off_the_record: bool = False,
) -> int | None:
    """Store one typed chat turn as a TEXT event after the turn resolves.

    ``answer`` is optional assistant text kept as meta context (not extracted).
    Questions are tagged as queries and skip fact extraction. Off-the-record
    turns are skipped entirely. Returns the event id, or None when skipped.
    Best-effort: any failure is logged, never raised into the chat path.
    """
    if off_the_record or not enabled():
        return None
    text = ingestable(message)
    if text is None:
        return None
    try:
        from app.events import Event, Modality
        from app.services import confidence as _conf
        from app.services.attachments import _index_event
        from app.storage import get_store

        is_query = looks_like_question(text)
        kind = "query" if is_query else "statement"
        source = "chat.query" if is_query else "chat.user"
        meta: dict = {"section": "chat", "kind": kind}
        ans = (answer or "").strip()
        if ans:
            meta["answer"] = ans[:4000]

        now = time.time()
        ev = Event(
            time=now, modality=Modality.TEXT, raw=text,
            summary=f"[chat] {text[:120]}", source=source,
            meta=meta,
        )
        _conf.attach(ev, _conf.ACCEPTED, capture=1.0)
        anchor = get_store().insert(ev)
        _index_event(anchor, ev)

        if not is_query:
            from app.services.worker import worker
            worker.enqueue("chat_ingest", payload={"event_id": anchor,
                                                   "text": text})
        return anchor
    except Exception as exc:
        print(f"[chat_ingest] skipped ({exc}).")
        return None


_SELF = ("me", "myself", "i")


def _drop_speaker_self(facts: dict) -> dict:
    """'me' in an assistant-written note is the assistant, which owns
    nothing: clear it to unspecified, and drop relations about it."""
    if not isinstance(facts, dict):
        return facts
    out = dict(facts)
    for key, rows in facts.items():
        if not isinstance(rows, list):
            continue
        kept = []
        for row in rows:
            if not isinstance(row, dict):
                kept.append(row)
                continue
            row = dict(row)
            if any(str(row.get(k) or "").strip().lower() in _SELF
                   for k in ("subject", "object")):
                continue
            for k, v in row.items():
                if k not in ("text", "source_span") and isinstance(v, str) \
                        and v.strip().lower() in _SELF:
                    row[k] = ""
            kept.append(row)
        out[key] = kept
    return out


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
    # Prefer the stored event's source so People v2 source_policy classifies
    # this as direct_message (create_person_candidates=True). Defaulting to
    # "document" wrongly treated chat like a doc and rejected new people.
    event_source = "chat.user"
    raw_ev = None
    if anchor is not None:
        try:
            raw_ev = store.get_event(int(anchor))
            if raw_ev and (raw_ev.get("source") or "").strip():
                event_source = str(raw_ev["source"]).strip()
        except Exception:
            pass
    # Queries must never be mined — defensive if an old job is still queued.
    if event_source == "chat.query":
        return
    try:
        meta = (raw_ev or {}).get("meta") or {}
        if isinstance(meta, dict) and meta.get("kind") == "query":
            return
    except Exception:
        pass
    if event_source.startswith("assistant."):
        # A cloud assistant wrote this note, not the user: label it as the
        # assistant so its first person ("I'll draft it") is not read as the
        # user's own commitment. Only owners the note names get assigned.
        from app.services.remote_mcp import ASSISTANT_LABELS
        who = ASSISTANT_LABELS.get(event_source.split(".", 1)[1], "Assistant")
        speaker = f"{who} (AI assistant, relaying a note)"
    facts = extractor._extract_text(text, speaker=speaker)
    if event_source.startswith("assistant."):
        facts = _drop_speaker_self(facts)
    n = _persist_facts(store, facts, anchor, text, now,
                       event_source=event_source)
    if event_source.startswith("assistant.") and anchor is not None:
        # The reviewer confirmed the project, so what the note yields belongs
        # to it even when no sentence names it.
        try:
            from app.services import projects
            meta = (raw_ev or {}).get("meta") or {}
            if isinstance(meta, dict) and meta.get("project"):
                projects.stamp_event_facts(store, int(anchor), meta["project"])
        except Exception as exc:
            print(f"[chat_ingest] project link skipped ({exc}).")
    try:
        if anchor is not None:
            store.mark_extracted([anchor], now)
    except Exception:
        pass
    if n:
        print(f"[chat_ingest] {n} fact(s) from typed chat.")
        worker.enqueue("graph", unique=True)
