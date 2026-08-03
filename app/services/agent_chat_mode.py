"""Student / study chat modes — sticky persona for chat turns.

Orthogonal to Field attention modes (ranking) and browser_agent task modes
(email/calendar/…). User picks a study mode on /ui; guidance is injected into
direct_answer and stacked onto browser plan/execute context.
"""
from __future__ import annotations

import threading
import time
from typing import Any

MANUAL_TTL_S = 2 * 3600.0
DEFAULT_MODE = "general"

_MODES: dict[str, dict[str, str]] = {
    "general": {
        "label": "General",
        "posture": "Default assistant — no study-specific coaching.",
        "guidance": "",
    },
    "lecture_notes": {
        "label": "Lecture notes",
        "posture": "Structured notes from slides and class; key terms first.",
        "guidance": (
            "STUDY MODE — Lecture notes: Turn slides, transcript, or screen "
            "context into clear structured notes (outline + bullets). Capture "
            "definitions, formulas, and 'write this down' cues. Prefer "
            "headings and key terms over prose. Flag unclear spots. Ask before "
            "dumping a very long note set. Do not invent lecture content that "
            "was not provided."
        ),
    },
    "homework": {
        "label": "Homework help",
        "posture": "Hints and steps first; full answers only if explicitly asked.",
        "guidance": (
            "STUDY MODE — Homework help: Be a tutor, not an answer key. Use a "
            "hint → next step → check understanding ladder. Ask what they have "
            "tried. Never dump a full final solution unless the user explicitly "
            "asks for the complete answer. For graded work, prioritize learning "
            "over finishing for them. If the problem is missing, ask for it."
        ),
    },
    "study_quiz": {
        "label": "Study / quiz",
        "posture": "Quiz from notes/context; explain misses; track weak spots.",
        "guidance": (
            "STUDY MODE — Study / quiz: Quiz the user from provided notes, "
            "context, or recent material. Ask one question at a time when "
            "practical; wait for their answer before revealing. Explain wrong "
            "answers briefly and note weak spots in-session. Offer short "
            "spaced-practice rounds. Prefer active recall over long summaries."
        ),
    },
    "syllabus": {
        "label": "Syllabus & deadlines",
        "posture": "Extract dates, weights, and next actions from course docs.",
        "guidance": (
            "STUDY MODE — Syllabus & deadlines: Extract due dates, exam dates, "
            "weights, and requirements from syllabus or LMS text. Prioritize "
            "what is due soon. Propose a short next-action list and "
            "calendar-ready phrasings (title + date/time). Do not invent "
            "deadlines that are not in the material."
        ),
    },
    "essay_rubric": {
        "label": "Essay / rubric",
        "posture": "Outlines, thesis options, rubric gaps — not full ghostwriting.",
        "guidance": (
            "STUDY MODE — Essay / rubric: Help with structure, thesis options, "
            "evidence slots, and rubric/criteria gap checks. Give short "
            "examples or sentence starters only — do not ghostwrite a full "
            "essay or paper unless the user explicitly asks for a complete "
            "draft. Compare the user's draft against any rubric or prompt in "
            "context and list missing criteria."
        ),
    },
    "reading": {
        "label": "Reading / textbook",
        "posture": "Explain open material; define jargon; offer flashcards.",
        "guidance": (
            "STUDY MODE — Reading / textbook: Explain the open page or passage "
            "in plain language. Define jargon. Relate ideas to the user's "
            "course context when available. Offer optional flashcard drafts "
            "(Q/A pairs) from the material. Do not invent citations or claims "
            "absent from the provided text."
        ),
    },
}

_lock = threading.RLock()
_manual: dict[str, Any] | None = None  # {id, until, set_at}


def registry() -> list[dict[str, Any]]:
    return [
        {
            "id": k,
            "label": v["label"],
            "posture": v["posture"],
        }
        for k, v in _MODES.items()
    ]


def _entry(mode_id: str) -> dict[str, str]:
    return _MODES.get(mode_id) or _MODES[DEFAULT_MODE]


def guidance_for(mode_id: str | None) -> str:
    key = (mode_id or "").strip().lower() or DEFAULT_MODE
    return (_entry(key).get("guidance") or "").strip()


def context_block(mode_id: str | None = None) -> str:
    """Block prepended to router/planner/executor/direct-answer context."""
    mid = (mode_id or "").strip().lower() if mode_id is not None else current()["id"]
    g = guidance_for(mid)
    if not g:
        return ""
    label = _entry(mid)["label"]
    return f"ACTIVE STUDY MODE — {label}:\n{g}\n\n"


def set_manual(name: str | None, *, ttl_s: float = MANUAL_TTL_S) -> dict[str, Any]:
    """Set sticky study mode. Pass None / '' / 'clear' to reset to general."""
    global _manual
    now = time.time()
    key = (name or "").strip().lower()
    with _lock:
        if not key or key in ("auto", "clear", "none", "default"):
            _manual = {
                "id": DEFAULT_MODE,
                "until": now + float(ttl_s),
                "set_at": now,
            }
            return current(now=now)
        if key not in _MODES:
            raise ValueError(f"unknown study mode: {name}")
        _manual = {"id": key, "until": now + float(ttl_s), "set_at": now}
        return current(now=now)


def clear() -> dict[str, Any]:
    return set_manual(DEFAULT_MODE)


def _manual_active(now: float) -> str | None:
    with _lock:
        if not _manual:
            return None
        if float(_manual.get("until") or 0) < now:
            return None
        mid = _manual.get("id")
        return mid if mid in _MODES else None


def current(*, now: float | None = None) -> dict[str, Any]:
    """Resolved sticky study mode (default: general)."""
    now = float(now if now is not None else time.time())
    mid = _manual_active(now) or DEFAULT_MODE
    m = _entry(mid)
    with _lock:
        until = _manual.get("until") if _manual and _manual_active(now) else None
        source = "manual" if _manual_active(now) else "default"
    return {
        "id": mid,
        "label": m["label"],
        "posture": m["posture"],
        "source": source,
        "until": until,
        "guidance": m.get("guidance") or "",
    }
