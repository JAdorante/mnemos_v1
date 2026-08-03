"""Structured entity details — status, owner, url, location (Track B).

The person_details pattern generalized to projects / orgs / tools / places:
what the USER typed (entity_attrs, shown as "you") always beats what memory
MINED from the entity's linked facts (shown as "from memory"). Mining is
deterministic regex over fact text — best-effort by design; the edit boxes
are the guarantee. Every value carries its receipt (backing fact id + quote)
and its epistemics: a confidence (1.0 for user assertions, the source fact's
confidence for mined values) and a per-key decay class, so the view can say
not just WHAT it believes but HOW MUCH and WHETHER IT'S GETTING STALE —
"project status" ages in weeks; a URL is good for a year.

When the user sets a field, the route also writes an APPROVED claim phrased
by `claim_text` — deliberately in the same shapes `mine` recognises, so even
if the override table were lost, mining would recover the value.

Pure functions over plain dicts (no Store, no I/O) — testable in isolation,
same philosophy as person_details.py.
"""
from __future__ import annotations

import re
import time

DETAIL_KEYS = ("status", "owner", "url", "location")

# Per-key decay class: how many days before an un-refreshed value renders as
# stale. Status is a fast-moving belief; a URL or location is near-identity.
FRESHNESS_DAYS = {
    "status": 14.0,
    "owner": 90.0,
    "url": 365.0,
    "location": 365.0,
}

# How a user-set value is phrased as a claim (name, value) — readable by chat
# grounding AND re-minable by the patterns below.
_CLAIM_PHRASES = {
    "status": "{name}'s status is {value}",
    "owner": "{value} is responsible for {name}",
    "url": "{name}'s website is {value}",
    "location": "{name} is located in {value}",
}

_URL = re.compile(r"\bhttps?://[^\s)>\]]+|\bwww\.[\w-]+\.[\w./-]{2,}", re.I)
_STATUS_IS = re.compile(r"(?:'s|s)\s+status\s+is\s+([\w /&-]{2,60})", re.I)
_STATUS_STATE = re.compile(
    r"\bis\s+(on hold|on track|blocked|shipped|launched|live|done|cancelled|"
    r"canceled|delayed|in progress|paused|at risk)\b", re.I)
_OWNER = re.compile(
    r"\b([A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+)?)\s+"
    r"(?:owns|runs|leads|is responsible for|is running|is leading)\b")
_LOC = re.compile(
    r"\b(?:based in|located in|headquartered in|moved to)\s+"
    r"([A-Z][\w ,.'-]{1,40})")


def claim_text(key: str, name: str, value: str) -> str:
    """The claim written when the user sets `key` to `value` by hand."""
    return _CLAIM_PHRASES[key].format(name=name, value=value)


def _clean(v: str) -> str:
    return v.strip().rstrip(" ,.;:")


def _mine_one(text: str, found: dict[str, dict], fact: dict,
              name_tokens: set[str]) -> None:
    """First match wins per key (caller feeds facts best-first)."""
    def put(key: str, value: str) -> None:
        value = _clean(value)
        if value and key not in found:
            found[key] = {
                "value": value,
                "fact_id": fact.get("fact_id"),
                "quote": text,
                "confidence": float(fact.get("confidence") or 0.5),
                "ts": fact.get("updated_at"),
            }

    m = _URL.search(text)
    if m:
        put("url", m.group(0))
    m = _STATUS_IS.search(text)
    if m:
        put("status", m.group(1))
    m = _STATUS_STATE.search(text)
    if m:
        put("status", m.group(1).lower())
    m = _OWNER.search(text)
    if m:
        # Owner must be asserted ABOUT this entity: the fact has to name it.
        after = text[m.end():].lower()
        if name_tokens & set(re.findall(r"\w{3,}", after)):
            put("owner", m.group(1))
    m = _LOC.search(text)
    if m:
        put("location", m.group(1))


def mine(name: str, aliases: list[str], facts: list[dict]) -> dict[str, dict]:
    """Best-effort details from the entity's linked facts.

    `facts` are the entity's ACTIVE, non-dismissed facts (any order). Facts a
    human blessed (approved/edited) beat unreviewed ones; recency breaks
    ties. Facts that literally name the entity are tried first, so a
    co-mentioned fact can't steal a field a name-anchored fact answers."""
    ranked = sorted(
        (f for f in facts if (f.get("text") or "").strip()),
        key=lambda f: (0 if f.get("review") in ("approved", "edited") else 1,
                       -(f.get("updated_at") or 0)))
    tokens = {t.lower() for t in re.findall(r"\w{3,}", name or "")}
    tokens |= {a.lower() for a in (aliases or []) if len(a) >= 3}

    found: dict[str, dict] = {}
    anchored = [f for f in ranked
                if tokens & {w.lower() for w in re.findall(r"\w{3,}", f["text"])}]
    for batch in (anchored, ranked):
        for f in batch:
            _mine_one(f["text"], found, f, tokens)
        if len(found) == len(DETAIL_KEYS):
            break
    return found


def merge(mined: dict[str, dict], attrs: dict[str, dict],
          now: float | None = None) -> dict[str, dict]:
    """Final per-field view: the user's override wins, memory fills the rest.

    Every field answers the Track B contract — what is believed (`value`),
    on whose word (`source`), how strongly (`confidence`), based on what
    (`fact_id`/`quote`), and whether the belief is aging out (`stale`,
    per the key's decay class)."""
    now = now or time.time()
    out: dict[str, dict] = {}
    for k in DETAIL_KEYS:
        a = attrs.get(k)
        if a and (a.get("value") or "").strip():
            row = {"value": a["value"], "source": "you", "confidence": 1.0,
                   "fact_id": a.get("fact_id"), "quote": None,
                   "ts": a.get("updated_at")}
        elif k in mined:
            row = {**mined[k], "source": "memory"}
        else:
            continue
        horizon = FRESHNESS_DAYS[k]
        age_days = ((now - float(row["ts"])) / 86400.0) if row.get("ts") else None
        row["freshness_days"] = horizon
        row["age_days"] = round(age_days, 1) if age_days is not None else None
        row["stale"] = bool(age_days is not None and age_days > horizon)
        out[k] = row
    return out
