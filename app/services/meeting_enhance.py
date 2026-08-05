"""Session Enhance — structured meeting note with receipts (Meeting Layer P3).

When a calendar-linked or ≥5-min session settles, compose a note from:
  * speaker-labeled turns
  * co-timed notepad jots (P2)
  * facts already extracted from those turns (cite, don't re-extract)

Persists as `reflections`/`reflection_items` with scope='meeting',
subject_type='session'. Each item carries source_fact_ids for playback.
"""
from __future__ import annotations

import os
import time
from typing import Any

from app.config import settings
from app.storage import Store, get_store

ENHANCE_MODEL = os.environ.get("QUILL_ENHANCE_MODEL", "claude-sonnet-4-6")
MIN_DURATION_S = float(os.environ.get("QUILL_MEETING_ENHANCE_MIN_S", "300"))
MAX_TURN_CHARS = 12_000
MAX_FACTS = 80

_KINDS = {
    "decision", "commitment", "open_question", "next_step", "note", "summary",
}

_DEFAULT_TEMPLATES: dict[str, dict] = {
    "external_call": {
        "label": "External call",
        "focus": (
            "Emphasize commitments exchanged across the table, decisions with "
            "owners, open questions the counterparty raised, and concrete next "
            "steps with who/when. Capture pricing, timing, and named deliverables."
        ),
    },
    "internal_sync": {
        "label": "Internal sync",
        "focus": (
            "Emphasize blockers, ownership of follow-ups, decisions that change "
            "the plan, and open questions that need answers before the next sync."
        ),
    },
    "diligence_pitch": {
        "label": "Diligence / pitch",
        "focus": (
            "Emphasize investor/counterparty concerns, numbers and terms "
            "mentioned, commitments we made, diligence asks, and the clear "
            "next step (send deck, schedule, answer). Flag anything that "
            "sounds like a decision or a soft no."
        ),
    },
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "2–4 sentence meeting synthesis — what happened and what shifted.",
        },
        "confidence": {"type": "number"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": sorted(_KINDS - {"summary"}),
                    },
                    "text": {"type": "string"},
                    "detail": {"type": "string"},
                    "subject": {"type": "string"},
                    "confidence": {"type": "number"},
                    "source_fact_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": [
                    "kind", "text", "detail", "subject",
                    "confidence", "source_fact_ids",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "confidence", "items"],
    "additionalProperties": False,
}


def enabled() -> bool:
    return os.getenv("QUILL_MEETING_ENHANCE", "1") not in ("0", "false", "False")


def settle_at(session: dict) -> float:
    gap = float(settings.consolidation.session_gap_s)
    return float(session.get("end") or 0) + gap


def is_settled(session: dict, now: float | None = None) -> bool:
    now = float(now if now is not None else time.time())
    return now > settle_at(session)


def is_eligible(session: dict, now: float | None = None) -> bool:
    """Calendar-linked or ≥5-min session that has settled."""
    if not is_settled(session, now):
        return False
    if session.get("calendar_event_id"):
        return True
    dur = float(session.get("duration_s")
                or (float(session.get("end") or 0)
                    - float(session.get("start") or 0)))
    return dur >= MIN_DURATION_S


def ensure_templates(store: Store) -> None:
    """Seed meeting_template:<name> kg_config rows once."""
    for name, body in _DEFAULT_TEMPLATES.items():
        key = f"meeting_template:{name}"
        try:
            if store.get_kg_config(key) is None:
                store.set_kg_config(key, body)
        except Exception:
            pass


def pick_template(session: dict, *, explicit: str | None = None) -> str:
    if explicit and explicit in _DEFAULT_TEMPLATES:
        return explicit
    meta = session.get("meeting_meta") or {}
    title = (meta.get("title") or "").lower()
    blob = title + " " + " ".join(
        (a.get("email") or "") + " " + (a.get("name") or "")
        for a in (meta.get("attendees") or []) if isinstance(a, dict)
    ).lower()
    if any(k in blob for k in (
            "diligence", "pitch", "investor", "partner meeting", "fundraising")):
        return "diligence_pitch"
    if title and any(k in title for k in (
            "standup", "sync", "weekly", "1:1", "one on one", "standup")):
        return "internal_sync"
    return "external_call"


def template_focus(store: Store, name: str) -> str:
    key = f"meeting_template:{name}"
    try:
        got = store.get_kg_config(key)
        if got:
            body = got[1] if isinstance(got, tuple) else got
            if isinstance(body, dict) and body.get("focus"):
                return str(body["focus"])
    except Exception:
        pass
    return _DEFAULT_TEMPLATES.get(name, _DEFAULT_TEMPLATES["external_call"])["focus"]


def already_enhanced(store: Store, session: dict) -> bool:
    """Idempotent across session-id rebuilds — match on period window."""
    start = float(session.get("start") or 0)
    end = float(session.get("end") or 0)
    cal = session.get("calendar_event_id") or ""
    try:
        rows = store.list_reflections(scope="meeting", limit=80)
    except Exception:
        return False
    for r in rows:
        ps, pe = r.get("period_start"), r.get("period_end")
        if ps is None or pe is None:
            continue
        if abs(float(ps) - start) < 2.0 and abs(float(pe) - end) < 2.0:
            return True
        # Calendar-linked: also match via summary tag
        if cal and cal in (r.get("summary") or ""):
            # weak — prefer window match; skip false positives from title reuse
            pass
    return False


def _turns_for_session(store: Store, session: dict) -> list[dict]:
    start = float(session.get("start") or 0)
    end = float(session.get("end") or 0)
    try:
        turns = store.recent_turns(50_000)
    except Exception:
        return []
    return [
        t for t in turns
        if float(t.get("start") or 0) < end + 1
        and float(t.get("end") or 0) > start - 1
    ]


def _facts_for_events(store: Store, event_ids: list[int]) -> list[dict]:
    if not event_ids:
        return []
    idset = set(int(e) for e in event_ids)
    try:
        rows = store.list_facts(limit=800)
    except Exception:
        return []
    out = [r for r in rows if r.get("source_event_id") in idset]
    out.sort(key=lambda r: float(r.get("extracted_at") or r.get("source_time") or 0))
    return out[:MAX_FACTS]


def _ground(ids, allowed: set[int]) -> list[int]:
    out: list[int] = []
    for i in ids or []:
        try:
            n = int(i)
        except (TypeError, ValueError):
            continue
        if n in allowed and n not in out:
            out.append(n)
    return out


def _format_turn(t: dict) -> str:
    from app.services.consolidation import format_turn_transcript
    return format_turn_transcript(t)


def build_packet(store: Store, session: dict, *, template: str) -> tuple[str, set[int], dict]:
    """Assemble the enhance prompt packet. Returns (text, allowed_fact_ids, counts)."""
    turns = _turns_for_session(store, session)
    event_ids = list(session.get("event_ids") or [])
    for t in turns:
        event_ids.extend(int(e) for e in (t.get("event_ids") or []))
    # unique preserve order
    seen: set[int] = set()
    uniq_eids: list[int] = []
    for e in event_ids:
        if e not in seen:
            seen.add(e)
            uniq_eids.append(e)

    facts = _facts_for_events(store, uniq_eids)
    allowed = {int(f["fact_id"]) for f in facts if f.get("fact_id") is not None}

    meta = session.get("meeting_meta") or {}
    title = meta.get("title") or "(untitled meeting)"
    attendees = meta.get("attendees") or []
    att_line = ", ".join(
        ((a.get("name") or a.get("email") or "?").strip())
        for a in attendees if isinstance(a, dict)
    ) or "(none listed)"

    # Co-timed jots across the whole session (+ pad)
    jots: list[str] = []
    try:
        from app.services import meeting_notes as _mn
        rows = store.events_in_window(
            float(session["start"]) - 60,
            float(session["end"]) + 60,
            source=_mn.SOURCE, modality="text", limit=80,
        )
        jots = [(r.get("raw") or "").strip() for r in rows if (r.get("raw") or "").strip()]
    except Exception:
        jots = []

    turn_lines = []
    budget = MAX_TURN_CHARS
    for t in sorted(turns, key=lambda x: float(x.get("start") or 0)):
        line = _format_turn(t)
        if len(line) > budget:
            break
        turn_lines.append(line)
        budget -= len(line) + 1

    fact_lines = []
    for f in facts:
        fid = f.get("fact_id")
        kind = f.get("kind") or "?"
        text = (f.get("text") or f.get("source_span") or "").strip().replace("\n", " ")
        fact_lines.append(f"[{fid}] ({kind}) {text[:240]}")

    focus = template_focus(store, template)
    packet = [
        f"Meeting: {title}",
        f"Template: {template} — {focus}",
        f"Attendees: {att_line}",
        f"Window: {time.strftime('%Y-%m-%d %H:%M', time.localtime(session['start']))}"
        f" → {time.strftime('%H:%M', time.localtime(session['end']))}"
        f" ({int(session.get('duration_s') or 0)}s)",
        "",
        "TRANSCRIPT (speaker-labeled turns):",
        "\n".join(turn_lines) if turn_lines else "(no turns)",
        "",
        "USER'S LIVE NOTES (importance anchors — do NOT quote these as source_span;",
        "cite only fact ids below when grounding items):",
        ("\n".join(f'- "{j}"' for j in jots) if jots else "(none)"),
        "",
        "FACTS ALREADY EXTRACTED FROM THIS MEETING (cite only these [ids]):",
        ("\n".join(fact_lines) if fact_lines else "(none yet)"),
    ]
    counts = {
        "turns": len(turn_lines), "jots": len(jots), "facts": len(fact_lines),
        "template": template,
    }
    return "\n".join(packet), allowed, counts


_SYSTEM = (
    "You are Mnemos's meeting-note enhancer. You receive a settled meeting: "
    "speaker-labeled transcript turns, the user's live notepad jots, and facts "
    "already extracted from those turns (each tagged [fact_id]).\n\n"
    "Produce a structured meeting note. Rules:\n"
    "- Cite ONLY fact ids present in the FACTS list. Drop invented ids.\n"
    "- Do NOT invent quotes. Items about spoken content must cite fact ids; "
    "jots may appear as kind=note with empty source_fact_ids.\n"
    "- Prefer sharp decisions, commitments, open questions, and next steps "
    "over a laundry list. Empty items is fine if the meeting was small talk.\n"
    "- Weave important jots as kind=note items near related decisions.\n"
    "- Follow the template focus for what to emphasize.\n"
    "- summary: 2–4 sentences of what the meeting was actually about."
)


def enhance_session(
    session: dict, *,
    store: Store | None = None,
    template: str | None = None,
    verbose: bool = False,
    force: bool = False,
) -> dict:
    """Enhance one settled session. Returns result dict with reflection_id."""
    if not enabled():
        return {"skipped": "disabled", "reflection_id": None}
    store = store or get_store()
    ensure_templates(store)
    if not force and already_enhanced(store, session):
        return {"skipped": "already enhanced", "reflection_id": None,
                "session_id": session.get("id")}
    if not is_eligible(session) and not force:
        return {"skipped": "not eligible", "reflection_id": None}

    tmpl = pick_template(session, explicit=template)
    packet, allowed, counts = build_packet(store, session, template=tmpl)
    if counts["turns"] == 0 and counts["facts"] == 0:
        return {"skipped": "empty session", "reflection_id": None, **counts}

    from app.services.model_router import router
    out = router.complete_json(
        "enhance", system=_SYSTEM,
        messages=[{"role": "user", "content": packet}],
        schema=_SCHEMA, max_tokens=2500, model=ENHANCE_MODEL,
    )

    now = time.time()
    meta = session.get("meeting_meta") or {}
    title = (meta.get("title") or "Meeting").strip()
    summary = (out.get("summary") or "").strip()
    # Tag calendar id in summary for humans; period window is the idempotency key.
    cal = session.get("calendar_event_id") or ""
    header = f"{title}"
    if cal:
        header = f"{title} · {cal}"
    full_summary = (f"{header}\n\n{summary}" if summary else header)

    rid = store.add_reflection(
        scope="meeting",
        subject_type="session",
        subject_id=session.get("id"),
        period_start=float(session.get("start") or now),
        period_end=float(session.get("end") or now),
        summary=full_summary,
        model=ENHANCE_MODEL,
        confidence=out.get("confidence"),
        created_at=now,
    )

    # Persist jots as note items first (rough notes in).
    n_items = 0
    try:
        from app.services import meeting_notes as _mn
        jot_rows = store.events_in_window(
            float(session["start"]) - 60,
            float(session["end"]) + 60,
            source=_mn.SOURCE, modality="text", limit=40,
        )
        for jr in jot_rows:
            text = (jr.get("raw") or "").strip()
            if not text:
                continue
            store.add_reflection_item(
                rid, kind="note", text=text,
                detail="live notepad jot", subject="",
                confidence=1.0, source_fact_ids=[], created_at=now,
            )
            n_items += 1
    except Exception:
        pass

    kept = 0
    for it in out.get("items") or []:
        text = (it.get("text") or "").strip()
        if not text:
            continue
        kind = it.get("kind") if it.get("kind") in _KINDS else "decision"
        if kind == "summary":
            kind = "decision"
        # Skip duplicate jot text the model echoed
        ids = _ground(it.get("source_fact_ids"), allowed)
        # Notes may have empty cites; other kinds should preferably cite —
        # still keep uncited decisions (model may summarize) but prefer grounded.
        store.add_reflection_item(
            rid, kind=kind, text=text,
            detail=(it.get("detail") or "").strip(),
            subject=(it.get("subject") or "").strip(),
            confidence=it.get("confidence"),
            source_fact_ids=ids, created_at=now,
        )
        n_items += 1
        kept += len(ids)
        if verbose:
            print(f"  [{kind}] {text[:80]!r} cites={ids}")

    # Meeting Layer P5 — apply default retention once the note exists.
    retention = None
    try:
        from app.services import meeting_mode as _mm
        retention = _mm.apply_default_for_session(store, session)
        # Meeting mode window can end when the note lands.
        st = _mm.status()
        if st.get("active") and (
                st.get("session_id") == session.get("id")
                or (session.get("calendar_event_id")
                    and st.get("calendar_event_id")
                    == session.get("calendar_event_id"))):
            _mm.exit_mode(reason="note_ready")
    except Exception as exc:
        print(f"[meeting_enhance] retention apply skipped ({exc}).")

    return {
        "reflection_id": rid,
        "summary": summary,
        "items": n_items,
        "grounded_citations": kept,
        "session_id": session.get("id"),
        "template": tmpl,
        "retention": retention,
        **counts,
    }


def run_once(store: Store | None = None, *, verbose: bool = False,
             force: bool = False) -> dict:
    """Enhance all currently eligible unsettled-for-enhance sessions."""
    if not enabled():
        return {"ok": True, "enhanced": 0, "skipped": "disabled"}
    store = store or get_store()
    ensure_templates(store)
    now = time.time()
    try:
        sessions = store.recent_sessions(limit=40)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "enhanced": 0}

    results = []
    for sess in sessions:
        if not is_eligible(sess, now) and not force:
            continue
        if already_enhanced(store, sess) and not force:
            continue
        try:
            res = enhance_session(sess, store=store, verbose=verbose, force=force)
            results.append(res)
            if res.get("reflection_id") and verbose:
                print(f"[meeting_enhance] session {sess.get('id')} → "
                      f"reflection {res['reflection_id']} "
                      f"({res.get('items')} items, tmpl={res.get('template')})")
        except Exception as exc:
            print(f"[meeting_enhance] session {sess.get('id')} failed ({exc}).")
            results.append({"error": str(exc), "session_id": sess.get("id")})

    n_ok = sum(1 for r in results if r.get("reflection_id"))
    return {"ok": True, "enhanced": n_ok, "results": results}


def hydrate_meeting_note(store: Store, reflection: dict) -> dict:
    """Shape a meeting reflection for the note page — evidence + playback."""
    from app.services.evidence_playback import clip_from_event, find_span

    items = store.reflection_items(reflection["id"])
    all_ids = sorted({i for it in items for i in it.get("source_fact_ids", [])})
    fmap = store.facts_by_ids(all_ids) if all_ids else {}
    ev_ids = [f.get("source_event_id") for f in fmap.values()
              if f.get("source_event_id")]
    emap = store.by_ids_map([int(i) for i in ev_ids if i]) if ev_ids else {}

    views = []
    for it in items:
        evidence = []
        for fid in it.get("source_fact_ids", []):
            fr = fmap.get(fid)
            if not fr:
                continue
            clip: dict[str, Any] = {}
            span_hl = None
            ev = emap.get(fr.get("source_event_id"))
            if ev is not None:
                clip = clip_from_event(ev)
                span = fr.get("source_span") or ""
                hit = find_span(clip.get("transcript") or "", span) if span else None
                if hit:
                    span_hl = {
                        "before": hit["before"],
                        "match": hit["match"],
                        "after": hit["after"],
                    }
            evidence.append({
                "fact_id": fid,
                "kind": fr.get("kind"),
                "text": fr.get("text") or fr.get("source_span") or "",
                "source_span": fr.get("source_span") or "",
                "source_event_id": fr.get("source_event_id"),
                "play_path": clip.get("play_path"),
                "playable": bool(clip.get("play_path")),
                "span_highlight": span_hl,
                "transcript": clip.get("transcript") or "",
            })
        views.append({
            "id": it["id"], "kind": it["kind"], "text": it["text"],
            "detail": it.get("detail") or "", "subject": it.get("subject") or "",
            "confidence": it.get("confidence"), "review": it.get("review"),
            "converted_fact_id": it.get("converted_fact_id"),
            "source_fact_ids": list(it.get("source_fact_ids") or []),
            "evidence": evidence,
        })

    # Recover meeting title from summary first line
    summary = reflection.get("summary") or ""
    title = summary.split("\n", 1)[0].strip()
    body = summary.split("\n", 1)[1].strip() if "\n" in summary else ""
    if " · " in title:
        title = title.split(" · ", 1)[0].strip()

    privacy = {}
    try:
        from app.services import meeting_mode as _mm
        sid = reflection.get("subject_id") if reflection.get(
            "subject_type") == "session" else None
        privacy = _mm.note_privacy_block(session_id=sid)
    except Exception:
        privacy = {}

    return {
        "id": reflection["id"],
        "scope": reflection["scope"],
        "title": title or "Meeting note",
        "summary": body or summary,
        "model": reflection.get("model"),
        "confidence": reflection.get("confidence"),
        "period_start": reflection.get("period_start"),
        "period_end": reflection.get("period_end"),
        "created_at": reflection.get("created_at"),
        "subject_type": reflection.get("subject_type"),
        "subject_id": reflection.get("subject_id"),
        "items": views,
        "privacy": privacy,
    }
