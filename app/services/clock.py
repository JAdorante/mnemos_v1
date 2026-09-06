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
# Unambiguous US slash dates the model sometimes emits instead of ISO.
_US_DATE_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{4})"
    r"(?:[,\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?)?$",
    re.I,
)

_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday",
                  "saturday", "sunday")
_WEEKDAY_RE = re.compile(
    r"\b(next|last|this)?\s*(" + "|".join(_WEEKDAY_NAMES) + r")\b", re.I)
_MONTH_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sept?|oct|"
    r"nov|dec)\b", re.I)


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


def _try_us_due(s: str) -> str | None:
    """Parse M/D/YYYY[ time] → ISO. None if not that shape."""
    m = _US_DATE_RE.match(s.strip())
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh, mm, ss = m.group(4), m.group(5), m.group(6)
    ampm = (m.group(7) or "").upper()
    try:
        if hh is None:
            return dt.date(year, month, day).isoformat()
        hour = int(hh)
        minute = int(mm)
        second = int(ss or 0)
        if ampm == "PM" and hour < 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        return dt.datetime(year, month, day, hour, minute, second).strftime(
            "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def coerce_due(value: str | None) -> str | None:
    """Normalize dues to ISO, or None when unrankable.

    Empty → None. Valid ISO → canonical form. Unambiguous US M/D/YYYY → ISO.
    Opaque free text ("Friday", "immediate", "Jul 7") → None so at-risk /
    reasoners never treat junk as a due (audit: free-text dues were stored and
    then silently ignored by ISO-only parsers).
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if is_iso_due(s):
        try:
            parsed = parse_due(s)
        except ValueError:
            return None
        if len(s) <= 10 or ("T" not in s and " " not in s[10:]):
            return parsed.date().isoformat()
        return parsed.strftime("%Y-%m-%dT%H:%M:%S")
    us = _try_us_due(s)
    if us is not None:
        return us
    return None


def reconcile_due_with_span(due: str | None, span: str | None,
                            now: Optional[dt.datetime] = None) -> str | None:
    """Snap an LLM-resolved due back onto the weekday the speaker named.

    Models routinely miscount weekday arithmetic ("by Friday" said on a
    Sunday resolved to Thursday), so when the source span names exactly one
    weekday and the resolved ISO date falls on a different one, move the due
    to the next occurrence of the named weekday (keeping any stated time).

    Deliberately conservative — the due passes through untouched when:
    the due isn't ISO, the span has no weekday / more than one distinct
    weekday, the span says "last <weekday>", or the span carries digits or a
    month name (a "Friday the 12th" is not ours to second-guess). A due
    already on the named weekday is never moved, so a correct resolution —
    including a deliberate "next Friday" a week+ out — survives.
    """
    if not due or not span or not is_iso_due(due):
        return due
    if re.search(r"\d", span) or _MONTH_RE.search(span):
        return due
    hits = _WEEKDAY_RE.findall(span)
    named = {w.lower() for _, w in hits}
    if len(named) != 1:
        return due
    if any((q or "").lower() == "last" for q, _ in hits):
        return due
    target_wd = _WEEKDAY_NAMES.index(next(iter(named)))
    try:
        resolved = parse_due(due)
    except ValueError:
        return due
    if resolved.weekday() == target_wd:
        return due
    today = (now or now_local()).date()
    corrected = today + dt.timedelta(days=(target_wd - today.weekday()) % 7)
    if len(due.strip()) <= 10:
        return corrected.isoformat()
    return dt.datetime.combine(corrected, resolved.time()).strftime(
        "%Y-%m-%dT%H:%M:%S")


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
