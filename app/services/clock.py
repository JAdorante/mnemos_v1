"""Local clock for prompts and due-date resolution.

Commitments/tasks used to store dues as opaque phrases ("Friday", "tomorrow").
Downstream overdue ranking only understands ISO, so those never became
trackable. Inject this clock into extractors and chat, and ask models to emit
absolute local dates.

Also used by calendar_intent (same "right now it is …" pattern).
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

# Loose ISO date or datetime (local, optional fractional seconds / Z).
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


def now_local() -> dt.datetime:
    return dt.datetime.now()


def clock_line(now: Optional[dt.datetime] = None) -> str:
    """One-line local clock for system prompts / grounding blocks."""
    n = now or now_local()
    return (
        f"RIGHT NOW (user's local time): {n:%A, %B %d %Y, %I:%M %p}".replace(
            " 0", " ")
    )


def clock_instruction(now: Optional[dt.datetime] = None) -> str:
    """Prompt appendix: resolve relatives against the real clock; emit ISO dues."""
    n = now or now_local()
    return (
        f"{clock_line(n)}\n"
        "Resolve relative dates and times ('today', 'tomorrow', 'next Friday', "
        "'by end of week', 'in two weeks') against that clock. When a task or "
        "commitment has a due/deadline, emit an absolute LOCAL value: "
        "YYYY-MM-DD for a day, or YYYY-MM-DDTHH:MM:SS when a time was stated. "
        "Do not invent a due date when none was implied. Leave due empty ('') "
        "if there is no timing."
    )


def is_iso_due(value: str | None) -> bool:
    if not value or not isinstance(value, str):
        return False
    s = value.strip()
    if not s or not _ISO_RE.match(s):
        return False
    try:
        parse_due(s)
        return True
    except ValueError:
        return False


def parse_due(value: str) -> dt.datetime:
    """Parse an ISO-ish due into a local-naive datetime (date → end of that day)."""
    s = (value or "").strip()
    if not s:
        raise ValueError("empty due")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if len(s) <= 10:
        d = dt.date.fromisoformat(s[:10])
        return dt.datetime(d.year, d.month, d.day, 23, 59, 59)
    raw = dt.datetime.fromisoformat(s)
    if raw.tzinfo is not None:
        return raw.astimezone().replace(tzinfo=None)
    return raw


def coerce_due(value: str | None) -> str | None:
    """Normalize extractor/UI dues: keep valid ISO, else keep original text.

    Empty → None. Valid ISO → canonical form. Free text left as-is so we never
    invent a date in code (the LLM + clock_instruction own that job).
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if not is_iso_due(s):
        return s
    try:
        parsed = parse_due(s)
    except ValueError:
        return s
    # Date-only inputs stay date-only; timed inputs keep second resolution.
    if len(s) <= 10 or ("T" not in s and " " not in s[10:]):
        return parsed.date().isoformat()
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def format_due_for_prompt(due: str | None,
                          now: Optional[dt.datetime] = None) -> str:
    """Human + absolute due for grounding lines, e.g. '2026-07-25 (tomorrow)'."""
    if not due:
        return ""
    s = str(due).strip()
    if not s:
        return ""
    if not is_iso_due(s):
        return s  # legacy free-text
    n = now or now_local()
    try:
        when = parse_due(s)
    except ValueError:
        return s
    day = when.date()
    today = n.date()
    delta = (day - today).days
    if delta == 0:
        rel = "today"
    elif delta == 1:
        rel = "tomorrow"
    elif delta == -1:
        rel = "yesterday"
    elif delta > 1:
        rel = f"in {delta} days"
    else:
        rel = f"{-delta} days overdue"
    abs_s = day.isoformat() if len(s) <= 10 else when.strftime("%Y-%m-%d %H:%M")
    return f"{abs_s} ({rel})"
