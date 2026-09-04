"""Display titles for tasks / commitments — short sky labels, not transcript echoes.

Meetings are a first-class form: when speech/chat presents a meeting, we log it
as ``Meet {person}[ about {topic}]`` — never as the user's meta-instruction
("make note of…", "remember that…").
"""
from __future__ import annotations

import re
from typing import Any

# User→Sparrow instructions that wrap the real commitment ("can you make note of…").
_META_NOTE = re.compile(
    r"^(?:"
    r"you asked (?:me )?to\s+"
    r"|(?:please\s+)?"
    r"(?:can you |could you |would you )?"
    r")?"
    r"(?:"
    r"make (?:a )?note of (?:that |this |it |the |a |an )?"
    r"|take (?:a )?note of (?:that |this |it |the |a |an )?"
    r"|note (?:down )?(?:that |this |the |a |an )?"
    r"|jot (?:down )?(?:that |this |the |a |an )?"
    r"|don'?t forget (?:about |that |to note )?"
    r"|remember (?:that |to note |to )?"
    r"|fyi:?\s+"
    r")",
    re.I,
)

_CHATTY_PREFIX = re.compile(
    r"^(?:"
    r"i(?:'ve| have) (?:got )?(?:a |an )?"
    r"|i(?:'m| am) "
    r"|i(?:'ll| will) "
    r"|i need to "
    r"|i have to "
    r"|we(?:'ve| have) (?:got )?(?:a |an )?"
    r"|we(?:'re| are) "
    r"|we(?:'ll| will) "
    r")",
    re.I,
)

_MEETING_WITH = re.compile(
    r"(?:^|\b)(?:a |an |the )?"
    r"(?P<kind>meeting|call|sync|appointment|standup|1:1|one[- ]on[- ]one)"
    r"\s+(?:with|w/)\s+(?P<body>.+)$",
    re.I,
)

# Cues that this commitment *is* a meeting (not a deliverable promise).
# Keep tight: bare "standup"/"sync" in passing ("after the standup") is NOT a meeting.
_MEETING_CUE = re.compile(
    r"\b(?:a |an |the )?(?:meeting|appointment|1:1|one[- ]on[- ]one)\b|"
    r"\b(?:call|zoom|teams|sync|standup)\s+(?:with|w/)\b|"
    r"\bmeet\s+(?:with|w/)\b",
    re.I,
)

_TIME_CLAUSE = re.compile(
    r"\b(?:"
    r"today|tonight|tomorrow|this\s+(?:morning|afternoon|evening|week)|"
    r"next\s+(?:week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?|"
    r"\d{1,2}(?::\d{2})\s*(?:am|pm|a\.m\.|p\.m\.)?|"
    r"\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)"
    r")\b[,.]?",
    re.I,
)

_MULTI_SPACE = re.compile(r"\s{2,}")
_TRAIL_PREP = re.compile(
    r"\b(?:by|on|before|after|until|till|at|of|that)\s*$", re.I,
)
_SELF = frozenset({"", "me", "i", "myself", "self", "us", "we"})


def _strip_times(s: str) -> str:
    s = _TIME_CLAUSE.sub(" ", s)
    s = _MULTI_SPACE.sub(" ", s).strip(" .,;:?!")
    s = _TRAIL_PREP.sub("", s).strip(" .,;:?!")
    return s


def _clean_person(name: str) -> str:
    n = " ".join((name or "").split()).strip(" .,;:?!\"'")
    if n.lower() in _SELF:
        return ""
    return n


def looks_like_meeting(
    text: str = "",
    *,
    source_span: str = "",
    form: str | None = None,
) -> bool:
    """True when extract fields / wording say this is a meeting or appointment."""
    if (form or "").strip().lower() == "meeting":
        return True
    blob = f"{text or ''} {source_span or ''}".strip()
    if not blob:
        return False
    return bool(_MEETING_CUE.search(blob))


def meeting_title(
    *,
    counterparty: str = "",
    topic: str = "",
    raw: str = "",
) -> str:
    """Canonical meeting log title: ``Meet {person}[ about {topic}]``."""
    who = _clean_person(counterparty)
    top = _strip_times((topic or "").strip(" .,;:?!\"'"))

    if not who and raw:
        # Recover person (+ topic) from chatty raw text.
        titled = titleize_work_item(raw, kind="commitment")
        if titled.lower().startswith("meet "):
            return titled
        m = _MEETING_WITH.search(_peel(raw))
        if m:
            return _meet_from_body(m.group("body"))

    # Structured person + topic still buried in raw ("…about Sparrow").
    if who and not top and raw:
        about = re.split(r"\babout\b", raw, maxsplit=1, flags=re.I)
        if len(about) == 2 and about[1].strip():
            top = _strip_times(about[1].strip(" .,;:?!\"'"))

    if who and top:
        return f"Meet {who} about {top}"
    if who:
        return f"Meet {who}"
    if top:
        return f"Meeting about {top}"
    return "Meeting"


def commitment_title(extract: dict[str, Any] | None = None, **fields: Any) -> str:
    """Build the stored commitment title from an extractor row (general path).

    Meetings → ``Meet {to_person}[ about {topic}]``.
    Promises → titleized imperative from ``text``.
    """
    c = dict(extract or {})
    c.update({k: v for k, v in fields.items() if v is not None})
    text = (c.get("text") or "").strip()
    span = (c.get("source_span") or "").strip()
    form = (c.get("form") or "").strip().lower()
    topic = (c.get("topic") or "").strip()
    to_person = c.get("to_person") or ""
    from_person = c.get("from_person") or ""

    if looks_like_meeting(text, source_span=span, form=form):
        # Counterparty is who we're meeting with — prefer to_person, else parse.
        who = _clean_person(to_person)
        if not who:
            who = _clean_person(from_person) if _clean_person(from_person) else ""
        return meeting_title(counterparty=who, topic=topic, raw=text or span)

    return titleize_work_item(text, kind="commitment") or text


def _peel(text: str) -> str:
    rest = " ".join((text or "").split()).strip(" .,;:?!\"'")
    for _ in range(3):
        nxt = _META_NOTE.sub("", rest).strip(" .,;:?!\"'")
        if nxt == rest:
            break
        rest = nxt or rest
    for _ in range(2):
        nxt = _CHATTY_PREFIX.sub("", rest).strip(" .,;:?!\"'")
        if nxt == rest:
            break
        rest = nxt or rest
    return rest or (text or "").strip()


def _meet_from_body(body: str) -> str:
    body = _strip_times(body)
    if not body:
        return "Meeting"
    about = re.split(r"\babout\b", body, maxsplit=1, flags=re.I)
    if len(about) == 2 and about[0].strip() and about[1].strip():
        who = about[0].strip(" .,;?!")
        topic = about[1].strip(" .,;?!")
        return f"Meet {who} about {topic}"
    return f"Meet {body}"


def titleize_work_item(text: str, *, kind: str = "commitment") -> str:
    """Rewrite chatty task/commitment text into a short, scannable title."""
    t = " ".join((text or "").split()).strip(" .,;:?!\"'")
    if not t:
        return ""

    rest = _peel(t)
    if not rest:
        rest = t

    m = _MEETING_WITH.search(rest)
    if m:
        return _meet_from_body(m.group("body"))

    if re.match(r"^(?:a |an |the )?meeting\b", rest, re.I) and len(rest) < 24:
        return "Meeting"

    cleaned = _strip_times(rest)
    out = cleaned or rest
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


def short_label(text: str, *, kind: str = "commitment", cap: int | None = None) -> str:
    """Sky / tip label: titleize then truncate on a word boundary."""
    titled = titleize_work_item(text, kind=kind) if kind in ("commitment", "task") else (
        " ".join((text or "").split())
    )
    if not titled:
        return "…"
    for sep in (" — ", " | "):
        if sep in titled and len(titled.split(sep, 1)[0]) >= 3:
            titled = titled.split(sep, 1)[0]
            break
    if cap is None:
        cap = 18 if kind == "person" else 28
    if len(titled) <= cap:
        return titled
    cut = titled[:cap]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    cut = cut.rstrip(" .,;:-")
    if len(cut) < max(6, cap // 3):
        cut = titled[: cap - 1]
    return cut + "…"
