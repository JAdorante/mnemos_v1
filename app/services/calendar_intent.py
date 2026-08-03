"""Natural-language calendar-ADD intent for chat.

Turns "put coffee with Sam on my calendar tomorrow at 3" into a structured
event proposal, which chat surfaces as an approval before writing it to iCloud
(see icloud_calendar.create_event). Two halves:

  looks_like_calendar_add(text)  cheap regex gate — an add/schedule VERB plus a
                                 calendar noun or a time. QUESTIONS about the
                                 calendar ("what's on tomorrow?") are NOT adds —
                                 they fall through to normal memory grounding,
                                 which already sees synced calendar events.
  parse(text)                    model slot-fill (current date injected so
                                 relative dates resolve) -> {summary, start,
                                 end, all_day, location, when_text} or None.

Model-agnostic and general (no user-specific logic): the LLM does the parsing;
this module only gates, normalizes, and renders a human-readable time.
"""
from __future__ import annotations

import datetime as dt
import re

_ADD_RE = re.compile(
    r"\b(add|schedule|put|book|set\s?up|create|make|pencil\s?in|block\s?off|"
    r"remind me to|new event|new meeting)\b", re.I)
_CAL_RE = re.compile(
    r"\b(calendar|meeting|appointment|appt|event|reminder|call|lunch|dinner|"
    r"breakfast|coffee|deadline|standup|sync|session|class|interview)\b", re.I)
_TIME_RE = re.compile(
    r"\b(today|tonight|tomorrow|tmrw|mon(day)?|tues(day)?|wed(nesday)?|"
    r"thurs(day)?|fri(day)?|sat(urday)?|sun(day)?|next week|this week|"
    r"\d{1,2}\s?(am|pm)|\d{1,2}:\d{2}|noon|midnight)\b", re.I)
# Leading question words mean "read my calendar", not "add to it".
_QUESTION_RE = re.compile(
    r"^\s*(what|when|whats|what'?s|show|list|do i|is there|are there|any|"
    r"how many|when'?s|remind me what)\b", re.I)

_SCHEMA = {
    "type": "object",
    "properties": {
        "is_event": {"type": "boolean",
                     "description": "true only if the user is asking to ADD/create "
                     "a calendar event. false for questions, or anything else."},
        "summary": {"type": "string", "description": "concise event title"},
        "start": {"type": "string",
                  "description": "local start, ISO 8601 no timezone: "
                  "'2026-07-19T15:00:00'. For an all-day event use a date only: "
                  "'2026-07-19'."},
        "end": {"type": "string",
                "description": "local end in the same format, or empty if unknown"},
        "all_day": {"type": "boolean"},
        "location": {"type": "string", "description": "place, or empty"},
    },
    "required": ["is_event", "summary", "start", "all_day"],
}


def looks_like_calendar_add(text: str) -> bool:
    """Cheap gate: an add/schedule verb + (a calendar noun OR a time), and not a
    question. Keeps the LLM parse off the vast majority of chat messages."""
    t = (text or "").strip()
    if not t or _QUESTION_RE.search(t):
        return False
    if not _ADD_RE.search(t):
        return False
    return bool(_CAL_RE.search(t) or _TIME_RE.search(t))


def _now() -> dt.datetime:
    return dt.datetime.now()


def _system_prompt(now: dt.datetime) -> str:
    from app.services.clock import clock_line
    return (
        "You extract a single calendar event to CREATE from the user's message. "
        f"{clock_line(now)}. "
        "Resolve relative dates and times ('tomorrow', 'next Friday', 'at 3') "
        "against that. Times are LOCAL, 24-hour, no timezone. If the user gives "
        "a day but no time, set all_day=true and start to the date only "
        "(YYYY-MM-DD). If the message is not a request to add/create an event "
        "(e.g. it's a question about the calendar), set is_event=false. Do not "
        "invent a location or attendees."
    )


def _humanize(start: str, end: str, all_day: bool) -> str:
    """A short, friendly 'when' line for the approval message."""
    try:
        if all_day or len(start) <= 10:
            d = dt.date.fromisoformat(start[:10])
            return d.strftime("%A, %b %d (all day)")
        s = dt.datetime.fromisoformat(start)
        txt = s.strftime("%A, %b %d at %I:%M %p").replace(" 0", " ")
        if end and len(end) > 10:
            e = dt.datetime.fromisoformat(end)
            txt += e.strftime("–%I:%M %p").replace(" 0", " ")
        return txt
    except ValueError:
        return start


def parse(text: str, *, now: dt.datetime | None = None, router=None) -> dict | None:
    """Model slot-fill. Returns a normalized event dict, or None if the message
    isn't actually a calendar-add (or extraction failed)."""
    text = (text or "").strip()
    if not text:
        return None
    now = now or _now()
    if router is None:
        from app.services.model_router import router as _r
        router = _r
    try:
        raw = router.complete_json(
            task="calendar_intent", system=_system_prompt(now),
            messages=[{"role": "user", "content": text}],
            schema=_SCHEMA, max_tokens=400) or {}
    except Exception as exc:
        print(f"[calendar_intent] parse failed ({exc}).")
        return None
    if not raw.get("is_event") or not raw.get("summary") or not raw.get("start"):
        return None
    all_day = bool(raw.get("all_day"))
    start = str(raw["start"]).strip()
    if all_day:
        start = start[:10]
    end = str(raw.get("end") or "").strip()
    event = {
        "summary": str(raw["summary"]).strip()[:200],
        "start": start,
        "end": end or None,
        "all_day": all_day,
        "location": str(raw.get("location") or "").strip()[:200],
        "calendar": "Home",
    }
    event["when_text"] = _humanize(start, end, all_day)
    return event
