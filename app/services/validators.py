"""Deterministic field validators for extracted facts (plan 1.4).

`gate_fact` calls `validate_fact_fields` before the insert path: a candidate
whose text/payload contains an obviously malformed email, phone, price, URL,
or (for tasks/commitments) due date is dropped with a reason instead of ever
reaching the store. Pure regex/string checks — no model calls, never raises
(a validator bug must never cost a fact that would otherwise be fine; on any
internal error it just says "clean").

Deliberately narrow: only flags tokens that already LOOK like an attempted
email/phone/price/URL/date but fail basic shape rules. Free text that merely
mentions numbers or words is never touched.
"""
from __future__ import annotations

import re

# Loosely finds anything shaped like "x@y" so we can validate the shape
# strictly afterward — a plain, well-formed email never trips this. Requires
# a char on both sides of '@' so a bare "@handle" mention is never mistaken
# for an attempted email.
_EMAIL_TOKEN = re.compile(r"[^\s@]+@[^\s@]+")
_EMAIL_VALID = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]{2,}$")

# Mirrors app/services/person_details.py's phone context + digit-count rule.
_PHONE_CTX = re.compile(r"\b(phone|number|cell|mobile|call|text|reach)\b", re.I)
_PHONE_TOKEN = re.compile(r"(?<![\w$])(\+?\d[\d\s().-]{4,}\d)(?!\w)")

_PRICE_TOKEN = re.compile(r"\$([^\s]+)")
_PRICE_NUMERIC = re.compile(r"^\d+(?:,\d{3})*(?:\.\d{1,2})?")

_URL_TOKEN = re.compile(r"\b\w+://\S+|\bwww\.\S+\.\S+", re.I)
_URL_VALID = re.compile(
    r"^(?:[a-z][\w+.-]*://)?(?:www\.)?[\w-]+(?:\.[\w-]+)+(?:[/?#]\S*)?$", re.I)

_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"}
_RELATIVE_DUE_WORDS = {"today", "tomorrow", "tonight", "yesterday"}
_ISO_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")

_STRIP_TRAIL = ".,;:!?)('\""


def _email_reason(text: str) -> str | None:
    for m in _EMAIL_TOKEN.finditer(text):
        token = m.group(0).strip(_STRIP_TRAIL)
        if not token or "@" not in token:
            continue
        if not _EMAIL_VALID.match(token):
            return f"malformed email: {token!r}"
    return None


def _phone_reason(text: str) -> str | None:
    if not _PHONE_CTX.search(text):
        return None
    for m in _PHONE_TOKEN.finditer(text):
        candidate = m.group(1)
        digits = sum(c.isdigit() for c in candidate)
        if digits == 0:
            continue
        if not (7 <= digits <= 15):
            return f"malformed phone: {candidate!r} ({digits} digits)"
    return None


def _price_reason(text: str) -> str | None:
    for m in _PRICE_TOKEN.finditer(text):
        token = m.group(1)
        num = _PRICE_NUMERIC.match(token)
        if not num:
            return f"malformed price: '${token}'"
        # Anything right after the matched number must not be more digits
        # ("$49.999" — three decimal digits — or "$1234extra5").
        rest = token[num.end():]
        if rest[:1].isdigit():
            return f"malformed price: '${token}'"
    return None


def _url_reason(text: str) -> str | None:
    for m in _URL_TOKEN.finditer(text):
        token = m.group(0).rstrip(_STRIP_TRAIL)
        if not _URL_VALID.match(token):
            return f"malformed url: {token!r}"
    return None


def _temporal_reason(due) -> str | None:
    s = (due or "").strip()
    if not s:
        return None
    low = s.lower()
    if low in _WEEKDAYS or low in _RELATIVE_DUE_WORDS:
        return f"unresolved relative due date: {s!r}"
    if _ISO_SHAPE.match(s):
        from app.services.clock import is_iso_due
        if not is_iso_due(s):
            return f"malformed due date: {s!r}"
    return None


def validate_fact_fields(kind: str, text: str, payload: dict | None) -> str | None:
    """Drop reason for one candidate fact, or None if nothing looks broken.

    Checks (in order): emails, phones, $ prices, and URLs found in `text`;
    plus, for tasks/commitments, the `due` field in `payload`. Never raises.
    """
    text = text or ""
    payload = payload or {}
    try:
        for check in (_email_reason, _phone_reason, _price_reason, _url_reason):
            reason = check(text)
            if reason:
                return reason
        if kind in ("task", "commitment"):
            reason = _temporal_reason(payload.get("due"))
            if reason:
                return reason
    except Exception:
        return None
    return None
