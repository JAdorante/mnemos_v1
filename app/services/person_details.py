"""Structured person details — phone, email, role, org, team, location.

Two sources, one precedence rule: what the USER typed in the People tab
(person_attrs, shown as "you") always beats what memory MINED from the
person's linked facts (shown as "from memory"). Mining is deterministic
regex over fact text — best-effort by design; the edit boxes are the
guarantee. Every mined value carries the fact it came from, so the UI can
show the receipt.

When the user sets a field, the route also writes an APPROVED claim phrased
by `claim_text` — deliberately in the same shapes `mine` recognises, so even
if the override table were lost, mining would recover the value from the
claim.

Multi-value keys (phone, email, org, team) also surface contact_points and
affiliation edges so a person can hold more than one of each. Role and
location stay single-valued.

Pure functions over plain dicts (no Store, no I/O) — testable in isolation,
same philosophy as desktop_agent/guards.py.
"""
from __future__ import annotations

import re

DETAIL_KEYS = ("phone", "email", "role", "org", "team", "location")
# Keys the UI can hold more than one of (contact points / affiliation edges).
MULTI_KEYS = frozenset({"phone", "email", "org", "team"})
_CONTACT_KEYS = frozenset({"phone", "email"})
_AFFIL_ORG_PREDICATES = frozenset({"works_at", "part_of"})
_AFFIL_TEAM_PREDICATE = "member_of"

# Per-key decay class (Track B parity with entity_details): days before an
# un-refreshed value renders as stale. Contacts are near-identity; role/org move.
FRESHNESS_DAYS = {
    "phone": 365.0,
    "email": 365.0,
    "role": 90.0,
    "org": 180.0,
    "team": 180.0,
    "location": 365.0,
}

# How a user-set value is phrased as a claim (name, value) — readable by chat
# grounding AND re-minable by the patterns below.
_CLAIM_PHRASES = {
    "phone": "{name}'s phone number is {value}",
    "email": "{name}'s email address is {value}",
    "role": "{name}'s role is {value}",
    "org": "{name} works at {value}",
    "team": "{name} is on the {value} team",
    "location": "{name} is based in {value}",
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
# $-guard keeps "$49" out; the char class breaks on '/' so dates don't match.
_PHONE = re.compile(r"(?<![\w$])(\+?\d[\d\s().-]{6,}\d)(?!\w)")
_PHONE_CTX = re.compile(r"\b(phone|number|cell|mobile|call|text|reach)\b", re.I)
_ROLE = re.compile(
    r"\b(?:is|as|works as|working as|hired as|appointed|named)\s+"
    r"(?:the\s+|a\s+|an\s+)?([A-Za-z][\w /&-]{2,40}?)\s+(?:at|of|for)\s+"
    r"([A-Z][\w&.' -]{1,40})")
_ROLE_IS = re.compile(r"\b(?:'s|s)\s+role\s+is\s+([\w /&-]{2,60})", re.I)
_RUNS = re.compile(r"\bruns\s+([\w /&-]{2,40}?)\s+at\s+([A-Z][\w&.' -]{1,40})")
_WORKS_AT = re.compile(r"\bworks?\s+at\s+([A-Z][\w&.' -]{1,40})")
_TEAM = re.compile(
    r"\bon\s+(?:the\s+)?([A-Za-z][\w /&-]{2,40}?)\s+team\b", re.I)
_LOC = re.compile(
    r"\b(?:lives in|based in|located in|moved to|relocated to)\s+"
    r"([A-Z][\w ,.'-]{1,40})")

_ORG_PREDICATES = ("works_at", "member_of", "part_of")


def claim_text(key: str, name: str, value: str) -> str:
    """The claim written when the user sets `key` to `value` by hand."""
    return _CLAIM_PHRASES[key].format(name=name, value=value)


def _clean(v: str) -> str:
    return v.strip().rstrip(" ,.;:")


def normalize_value(key: str, value: str) -> str:
    """Canonical compare key for deduping multi-value rows."""
    v = _clean(value or "")
    if key == "email":
        return v.lower()
    if key == "phone":
        return re.sub(r"\D+", "", v)
    return v.lower()


def _phone_ok(candidate: str, text: str) -> bool:
    digits = sum(c.isdigit() for c in candidate)
    if not 7 <= digits <= 15:
        return False
    return bool(_PHONE_CTX.search(text)) or candidate.strip().startswith("+")


def _name_tokens(name: str, aliases: list[str] | None = None) -> set[str]:
    tokens = {t.lower() for t in re.findall(r"\w{3,}", name or "")}
    tokens |= {a.lower() for a in (aliases or []) if len(a) >= 3}
    return tokens


# Plan 2.4 / People intel §F — auto-write only when score ≥ τ_attr.
ATTR_MIN = 2.0


def contact_attribution_score(
    text: str,
    *,
    kind: str,
    value: str,
    start: int,
    end: int,
    tokens: set[str],
) -> float:
    """Score how strongly a contact value belongs to `tokens`' person.

    Mirrors people_intelligence_architecture.md §F (possessive / reach-at /
    local-part; penalties for "will email X at Y" subject confusion).
    Auto-write when score ≥ ATTR_MIN; weaker scores stay review / unassigned.
    """
    if not tokens:
        return 0.0
    low = text.lower()
    val = (value or "").lower()
    score = 0.0

    for t in tokens:
        te = re.escape(t)
        if kind == "email":
            if re.search(rf"\b{te}'s\s+(?:e-?mail|mail)(?:\s+address)?\b", low):
                score = max(score, 3.0)
            if re.search(
                rf"\b{te}\s+(?:e-?mail|mail)(?:\s+address)?\s+(?:is|:)\s*"
                rf"{re.escape(val)}",
                low,
            ):
                score = max(score, 3.0)
            if re.search(
                rf"\b(?:e-?mail|reach|contact|message)\s+{te}\s+at\s+"
                rf"{re.escape(val)}",
                low,
            ):
                score = max(score, 2.5)
            # "X will email Y at addr" — addr is Y's, not X's.
            if re.search(
                rf"\b{te}\s+will\s+(?:e-?mail|mail|send)\b", low
            ) and not re.search(
                rf"\b(?:e-?mail|reach|contact|message)\s+{te}\s+at\s+", low
            ):
                score -= 4.0
        else:  # phone
            if re.search(
                rf"\b{te}'s\s+(?:phone|number|cell|mobile)\b", low):
                score = max(score, 3.0)
            if re.search(
                rf"\b(?:reach|call|text|phone)\s+{te}\s+(?:at|on)\s+"
                rf"{re.escape(val)}",
                low,
            ):
                score = max(score, 2.5)
            if re.search(
                rf"\b{te}\s+(?:phone|number|cell|mobile)\s+(?:is|:)\s*"
                rf"{re.escape(val)}",
                low,
            ):
                score = max(score, 3.0)

    if kind == "email" and "@" in val and score < ATTR_MIN:
        local = val.split("@", 1)[0]
        root = re.split(r"[._+-]", local)[0]
        if len(root) >= 3 and any(
            root == t or t.startswith(root) or root.startswith(t)
            for t in tokens
        ):
            # Name must appear OUTSIDE the contact value (local-part alone
            # matching the email text is circular — "marc@" ≠ mentioning Marc).
            lo = max(0, start - 60)
            hi = min(len(text), end + 60)
            neighborhood = low[lo:start] + " " + low[end:hi]
            if any(re.search(rf"\b{re.escape(t)}\b", neighborhood)
                   for t in tokens):
                score = max(score, 1.5)

    return score


def _contact_belongs(
    text: str,
    *,
    kind: str,
    value: str,
    start: int,
    end: int,
    tokens: set[str],
) -> bool:
    """True only when attribution score clears ATTR_MIN (auto-write gate)."""
    return contact_attribution_score(
        text, kind=kind, value=value, start=start, end=end, tokens=tokens,
    ) >= ATTR_MIN


def _mine_bio(text: str, found: dict[str, dict], fid, quote: str) -> None:
    """Role / org / team / location — pronouns OK; not used for email/phone."""
    def put(key: str, value: str) -> None:
        value = _clean(value)
        if value and key not in found:
            found[key] = {"value": value, "fact_id": fid, "quote": quote}

    m = _ROLE_IS.search(text)
    if m:
        put("role", m.group(1))
    m = _ROLE.search(text)
    if m:
        put("role", m.group(1))
        put("org", m.group(2))
    m = _RUNS.search(text)
    if m:
        put("role", "runs " + _clean(m.group(1)))
        put("org", m.group(2))
    m = _WORKS_AT.search(text)
    if m:
        put("org", m.group(1))
    m = _TEAM.search(text)
    if m:
        put("team", m.group(1))
    m = _LOC.search(text)
    if m:
        put("location", m.group(1))


def _mine_contacts(
    text: str, found: dict[str, dict], fid, quote: str, tokens: set[str],
) -> None:
    """Email/phone only when clearly about this person."""
    def put(key: str, value: str) -> None:
        value = _clean(value)
        if value and key not in found:
            found[key] = {"value": value, "fact_id": fid, "quote": quote}

    m = _EMAIL.search(text)
    if m and _contact_belongs(
            text, kind="email", value=m.group(0),
            start=m.start(), end=m.end(), tokens=tokens):
        put("email", m.group(0))
    m = _PHONE.search(text)
    if (m and _phone_ok(m.group(1), text)
            and _contact_belongs(
                text, kind="phone", value=m.group(1),
                start=m.start(), end=m.end(), tokens=tokens)):
        put("phone", m.group(1))


def mine(name: str, aliases: list[str], facts: list[dict],
         affiliations: list[dict] | None = None) -> dict[str, dict]:
    """Best-effort details from the person's linked facts (+ graph employer).

    `facts` are the person's ACTIVE, non-dismissed facts (any order). Facts a
    human blessed (approved/edited) beat unreviewed ones; recency breaks
    ties. Email/phone ONLY come from name-anchored facts with explicit
    attribution (possessive / "reach X at" / matching email local-part) so a
    co-mention ("Justin will email marc@…") cannot steal Marc's address onto
    Justin. Role/org/location may still fill from other linked facts.
    """
    ranked = sorted(
        (f for f in facts if (f.get("text") or "").strip()),
        key=lambda f: (0 if f.get("review") in ("approved", "edited") else 1,
                       -(f.get("updated_at") or 0)))
    tokens = _name_tokens(name, aliases)

    found: dict[str, dict] = {}
    anchored = [f for f in ranked
                if tokens & {w.lower() for w in re.findall(r"\w{3,}", f["text"])}]
    # Contacts: name-anchored + belongs-check only.
    for f in anchored:
        _mine_contacts(f["text"], found, f.get("fact_id"), f["text"], tokens)
    # Bio fields: anchored first, then other linked facts (pronouns).
    for batch in (anchored, ranked):
        for f in batch:
            _mine_bio(f["text"], found, f.get("fact_id"), f["text"])
        if all(k in found for k in DETAIL_KEYS if k not in _CONTACT_KEYS):
            if "email" in found and "phone" in found:
                break

    # A graph-asserted employer edge beats a regex guess for org (primary only).
    for a in affiliations or []:
        if a.get("predicate") in _AFFIL_ORG_PREDICATES and (a.get("name") or "").strip():
            found["org"] = {"value": a["name"].strip(), "fact_id": None,
                            "quote": f"graph: {a['predicate']} {a['name']}"}
            break
    for a in affiliations or []:
        if a.get("predicate") == _AFFIL_TEAM_PREDICATE and (a.get("name") or "").strip():
            found.setdefault(
                "team",
                {"value": a["name"].strip(), "fact_id": None,
                 "quote": f"graph: {a['predicate']} {a['name']}"})
            break
    return found


def merge(mined: dict[str, dict], attrs: dict[str, dict],
          now: float | None = None) -> dict[str, dict]:
    """Final per-field view: the user's override wins, memory fills the rest.

    Track B contract: value, source, confidence, receipt, and stale flag per
    the key's decay class (same shape as entity_details.merge).
    """
    import time as _time
    now = now if now is not None else _time.time()
    out: dict[str, dict] = {}
    for k in DETAIL_KEYS:
        a = attrs.get(k)
        if a and (a.get("value") or "").strip():
            row = {"value": a["value"], "source": "you", "confidence": 1.0,
                   "fact_id": a.get("fact_id"), "quote": None,
                   "ts": a.get("updated_at")}
        elif k in mined:
            row = {**mined[k], "source": "memory"}
            row.setdefault("confidence", 0.5)
        else:
            continue
        horizon = FRESHNESS_DAYS[k]
        age_days = ((now - float(row["ts"])) / 86400.0) if row.get("ts") else None
        row["freshness_days"] = horizon
        row["age_days"] = round(age_days, 1) if age_days is not None else None
        row["stale"] = bool(age_days is not None and age_days > horizon)
        out[k] = row
    return out


def detail_lists(
    *,
    merged: dict[str, dict],
    attrs: dict[str, dict],
    contact_points: list[dict] | None = None,
    affiliations: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """Multi-value rows for the People DETAILS UI.

    Phone/email pull every active contact point plus the primary attr/mined
    value. Org pulls works_at/part_of affiliations; team pulls member_of.
    Role/location stay length-0-or-1 lists mirroring `merged`.
    """
    out: dict[str, list[dict]] = {k: [] for k in DETAIL_KEYS}
    seen: dict[str, set[str]] = {k: set() for k in DETAIL_KEYS}

    def _add(key: str, value: str, *, source: str, ref: str,
             quote: str | None = None, confidence: float | None = None) -> None:
        value = _clean(value)
        if not value:
            return
        norm = normalize_value(key, value)
        if not norm or norm in seen[key]:
            return
        seen[key].add(norm)
        row = {"value": value, "source": source, "ref": ref}
        if quote:
            row["quote"] = quote
        if confidence is not None:
            row["confidence"] = confidence
        out[key].append(row)

    # User primary overrides first.
    for k, a in (attrs or {}).items():
        if k in DETAIL_KEYS and (a.get("value") or "").strip():
            _add(k, a["value"], source="you", ref=f"attr:{k}")

    # Contact points (multi phone/email).
    for cp in contact_points or []:
        key = cp.get("type")
        if key not in _CONTACT_KEYS:
            continue
        src = "you" if (cp.get("created_by") or "") == "user" else "attributed"
        _add(key, cp.get("value_display") or cp.get("value_normalized") or "",
             source=src, ref=f"cp:{cp.get('contact_point_id')}",
             quote=cp.get("evidence_quote"),
             confidence=cp.get("confidence"))

    # Affiliation edges → org / team.
    for a in affiliations or []:
        name = (a.get("name") or "").strip()
        pred = a.get("predicate") or ""
        eid = a.get("id") or a.get("entity_id")
        if pred in _AFFIL_ORG_PREDICATES and name:
            _add("org", name, source="graph",
                 ref=f"rel:{pred}:{eid}" if eid is not None else f"rel:{pred}:{name}",
                 quote=f"graph: {pred}")
        elif pred == _AFFIL_TEAM_PREDICATE and name:
            _add("team", name, source="graph",
                 ref=f"rel:{pred}:{eid}" if eid is not None else f"rel:{pred}:{name}",
                 quote=f"graph: {pred}")

    # Mined / merged primary fills any gaps (memory source).
    for k, row in (merged or {}).items():
        if k not in DETAIL_KEYS:
            continue
        _add(k, row.get("value") or "", source=row.get("source") or "memory",
             ref=f"merged:{k}", quote=row.get("quote"),
             confidence=row.get("confidence"))

    return out
