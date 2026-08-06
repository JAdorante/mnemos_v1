"""Screen → memory. What the user SEES and DOES becomes graph knowledge.

Desktop capture already turns the screen into VISION events (source
desktop.screen: window title + OCR/VLM text) and rolls them into activities —
but none of it was ever mined for facts. The ears fed the graph; the eyes
didn't. This closes that gap on the audio pipeline's pattern:

  batch unextracted desktop.screen events (settled, oldest first)
    → ONE local-first extraction call (screen-specific precision prompt)
    → documents._persist_facts (the same hygiene gate: confidence floor,
      verbatim-span check against the OCR, dedup/supersede — critical here,
      the same email on screen for five minutes must not mint five claims)
    → graph rebuild chain → people/org/tool/task nodes + edges → constellation

Attribution is the hard part of screen text: most of it is OTHER people's
words (emails received, feeds, docs). The prompt is strict: tasks and
commitments only when they are clearly the USER's own (their sent/drafted
message, their to-do list, their calendar); everything else at most a claim
or an entity/relation. Provenance is kept — every fact anchors to the screen
frame event it came from.

Off switch: QUILL_SCREEN_EXTRACT=0 (capture itself stays on).
Generic code: what gets learned comes from THIS install's own screen.
"""
from __future__ import annotations

import os
import time

MIN_CHARS = int(os.getenv("QUILL_SCREEN_EXTRACT_MIN_CHARS", "60"))
BATCH = int(os.getenv("QUILL_SCREEN_EXTRACT_BATCH", "10"))
_MAX_TEXT = 4000          # cap the combined batch text per LLM call
# Fresh lane: frames younger than this jump any backlog, so what's on screen
# NOW becomes memory in one worker cycle even mid-drain. Chronology is kept
# within each lane (both query oldest-first), so contradiction adjudication
# still sees each lane in order.
FRESH_S = float(os.getenv("QUILL_SCREEN_EXTRACT_FRESH_S", "600"))

SOURCE = "desktop.screen"

_SYSTEM = (
    "You extract structured memory from text captured OFF THE USER'S SCREEN "
    "(window titles + OCR). Screen text is mostly OTHER PEOPLE'S words — "
    "emails received, chats, articles — so attribution rules are strict:\n"
    "- tasks: only items that are clearly the user's own to-dos (their to-do "
    "list, their calendar, a message THEY wrote assigning themselves work).\n"
    "- commitments: only promises the USER made in text they wrote/sent.\n"
    "- claims: short, stable facts about identifiable people, organizations, "
    "projects, or tools visible in context (roles, relationships, states).\n"
    "- entities/relations: organizations, projects, tools, places on screen "
    "and their clear relationships to people.\n"
    "Public social / news / short-form FEEDS are not personal memory: do not "
    "emit tasks, commitments, claims, people, or relations about celebrities, "
    "influencers, meme accounts, or posts the user is only browsing. Only "
    "extract from social surfaces when the text is clearly the USER's own "
    "draft or reply they are writing.\n"
    "Precision over recall: when unsure who said or owns something, emit "
    "nothing. Never infer beyond the visible text. source_span MUST be a "
    "verbatim quote of the provided text."
)


def enabled() -> bool:
    return os.getenv("QUILL_SCREEN_EXTRACT", "1") not in ("0", "false", "False")


def _extract(text: str) -> dict:
    from app.services.extractor import _SCHEMA, EXTRACTOR_MODEL
    from app.services.model_router import router
    out = router.complete_json(
        "extract", system=_SYSTEM,
        messages=[{"role": "user", "content": f"Screen text:\n\n{text}"}],
        schema=_SCHEMA, max_tokens=1024, model=EXTRACTOR_MODEL)
    return out or {}


def run_once() -> dict:
    """Mine one batch of un-mined screen frames. Returns counters. Safe to
    call any time — no frames, or the feature off, is a cheap no-op."""
    if not enabled():
        return {"enabled": False}
    from app.services.documents import _persist_facts
    from app.storage import get_store

    store = get_store()
    now = time.time()
    # Filter by source IN SQL: a Python-side filter over an oldest-first
    # vision window let never-marked webcam frames starve screen frames
    # forever (head-of-line blocking — same bug class the modality filter
    # exists for). Fresh frames first; the backlog drains behind them.
    lane = "fresh"
    screen = store.unextracted_events(limit=BATCH * 8, modality="vision",
                                      source=SOURCE, since=now - FRESH_S)
    if not screen:
        lane = "backlog"
        screen = store.unextracted_events(limit=BATCH * 8, modality="vision",
                                          source=SOURCE)
    if not screen:
        return {"events": 0, "facts": 0}
    from app.services.surface_filters import (
        event_is_activity_only_social, is_self_window, strip_noise_lines,
    )

    batch, skipped_ids, used = [], [], 0
    for eid, ev in screen:
        # Never mine the app's own UI: frames of our chat/console teach the
        # graph our own labels ("Memory Console" became an entity). Intake now
        # filters these too — this catches frames captured before that fix.
        meta0 = ev.meta if isinstance(getattr(ev, "meta", None), dict) else {}
        if is_self_window(str(meta0.get("window") or "")):
            skipped_ids.append(eid)
            continue
        # Intake already scrubs console frames; strip residual CLI/log lines
        # from older events so they aren't mined as tasks.
        text = strip_noise_lines((ev.raw or ev.summary or "").strip())
        if len(text) < MIN_CHARS:
            skipped_ids.append(eid)   # too thin to teach — mark and move on
            continue
        # Public feeds stay in activity timeline but must not mint
        # people/claims (unless user compose — gated inside the helper).
        if event_is_activity_only_social(ev):
            skipped_ids.append(eid)
            continue
        if len(batch) >= BATCH or used + len(text) > _MAX_TEXT:
            break
        meta = ev.meta if isinstance(getattr(ev, "meta", None), dict) else {}
        window = str(meta.get("window") or "")
        batch.append((eid, text, window))
        used += len(text)

    n = 0
    if batch:
        joined = "\n\n".join(t for _, t, _ in batch)
        # Deduped window titles for source_policy (news / email / code / …).
        windows = list(dict.fromkeys(w for _, _, w in batch if w))
        window_blob = " | ".join(windows)
        try:
            facts = _extract(joined)
            anchor = batch[0][0]      # provenance: first frame of the batch
            n = _persist_facts(
                store, facts, anchor, joined, now,
                event_source=SOURCE, window=window_blob)
            # Desktop email → People v2 contacts / works_at (capture-first CRM).
            try:
                from app.services import people_pipeline as pp
                from app.services import source_policy as sp
                if sp.classify_source(
                        event_source=SOURCE, window=window_blob,
                        text=joined) == "email":
                    pp.ingest_email_network(
                        joined, store=store, event_id=anchor,
                        event_source=SOURCE, window=window_blob, now=now)
            except Exception as exc:
                print(f"[screen_extract] email-network skipped ({exc}).")
            # Plan 4.2 (c): Sent-toast / Sent-folder OCR → completion
            # *candidate* (offer only — never auto-complete).
            try:
                from app.services import commitment_complete as cc
                if cc.looks_like_sent_toast(joined):
                    cc.offer_matches_for_text(
                        joined, source="screen_sent",
                        event_id=anchor, store=store, force=True)
            except Exception as exc:
                print(f"[screen_extract] sent-candidate skipped ({exc}).")
        except Exception as exc:
            print(f"[screen_extract] extract failed ({exc}); will retry later.")
            store.mark_extracted(skipped_ids, now)
            return {"events": 0, "facts": 0, "error": str(exc)}
    store.mark_extracted(skipped_ids + [eid for eid, _, _ in batch], now)
    if n:
        print(f"[screen_extract] {n} fact(s) from {len(batch)} "
              f"{lane} screen frame(s).")
        try:
            from app.services.worker import worker
            worker.enqueue("graph", unique=True)
        except Exception:
            pass
    # Exact check (one indexed row): a fresh-lane pass must still report the
    # backlog behind it, or the drain chain stops after every fresh frame.
    remaining = bool(store.unextracted_events(limit=1, modality="vision",
                                              source=SOURCE))
    return {"events": len(batch), "facts": n, "lane": lane,
            "remaining": remaining}
