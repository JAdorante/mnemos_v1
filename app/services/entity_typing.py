"""Entity model: what a name looks like, and what kind of thing it is.

Two layers.

Name shapes, pure functions over a name string (no store, no app imports),
so the candidate gate, the kind vote and the repair script can all call
them:

- `code_like`: the name is an identifier, not a name. A commit hash, a row or
  milestone id ("k9", "t3", "Phase 1"), a version, a date, a ticket number.
  These are how milestone ids and commit hashes became projects. One
  sighting of one is never enough to make a node, even when the user typed
  it.
- `common_noun_like`: every word is lowercase and alphabetic ("pet
  groomers", "bowling alleys", "kite repair"). A description, not a name: it
  belongs in the text of a fact. snake_case, dotted and digit-bearing names
  are not common nouns (`mint_recurrence`, `app.config`, `qwen2.5`).
- `value_literal`: a value, not a thing: money, a percentage, a date or a
  weekday, a time, a bare number ("$49", "12%", "Nov 14", "Friday", "42").
- `company_name`: the name ends in a company's legal form, from any country
  ("Acme LLC", "Halcyon GmbH", "Volvo AB", "Tata Motors Limited"): an org
  even when the user files notes under it. Generic vocabulary, not anyone's
  data (`LEGAL_FORMS`); the organisation words in
  `name_quality._CORP_SUFFIXES` ("group", "partners", "university") are a
  different idea and are not legal forms.

`code_named_kind` and `weak_name` combine a shape with what the sightings
proposed: a code-shaped name keeps the org or tool kind the model gave it
only when two independent sightings agree, and a single lowercase word the
model called a tool or an org is not weak.

Neither shape rule lives in `name_quality.is_plausible_entity`: that gate
says whether a string can be an entity at all, and lowercase and snake_case
names must keep passing it.

The kind vote (`vote`, `retype`) reads the mention ledger: the kind of a
name is decided by its evidence, not by the first sighting. See `vote`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# A hex run long enough to be a hash: 7 is git's short form. A digit is
# required so an ordinary word made of a-f letters ("defaced") is not one.
_HEX = re.compile(r"^[0-9a-f]{7,}$", re.I)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Row / option / milestone ids written tight: k9, t3, v2, m12, ab4c, q3, 3m.
_COMPACT_ID = re.compile(r"^(?:[a-z]{1,3}\d{1,4}[a-z]?|\d{1,3}[a-z]{1,2})$",
                         re.I)
# Ticket ids: ENG-482, PR-12, JIRA_1234. Two digits or more, so a product
# name with a model number ("GPT-4") is not one.
_TICKET_ID = re.compile(r"^[a-z]{1,4}[-_]\d{2,}$", re.I)
# Series ids with a letter after the hyphen: a workstream, track or option
# "QA-B", "TR-B1", "PH-2C". Upper case only, a prefix of two letters or more
# and a suffix of one or two characters with a letter in it, so "X-ray",
# "Coca-Cola", initials ("J-P") and a model number ("GPT-4", the ticket
# rule's call) stay names. A shape, not anyone's ids: on the 24 practice
# worlds no entity name has it (their text has one, the standard "USB-C",
# which reads as one too); a product of that shape ("HP-UX") keeps its kind
# once two sightings agree (`code_named_kind`).
_SERIES_ID = re.compile(r"^[A-Z]{2,4}-(?:[A-Z]{1,2}|[A-Z]\d|\d[A-Z])$")
_VERSION = re.compile(r"^v?\d+(?:\.\d+)+[a-z]?$", re.I)
# A milestone word followed by its number: "Phase 1", "Stage 0", "Step 2b",
# "Sprint 14", "Q3 2026", "Week 12". A closed list on purpose: "Windows 11"
# and "Area 51" end in a number too and are names.
_MILESTONE = re.compile(
    r"^(?:phase|stage|step|round|sprint|milestone|iteration|version|release|"
    r"build|row|item|ticket|issue|task|run|batch|wave|tier|week|day|quarter|"
    r"q|fy|pr|workstream)\s*#?\d+[a-z]?(?:\s+\d{4})?$", re.I)
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|"
          r"july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
          r"nov(?:ember)?|dec(?:ember)?)\.?")
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_MONTH_DATE = re.compile(
    rf"^(?:{_MONTH}\s+{_DAY}(?:,?\s+\d{{4}})?"       # Nov 14, Nov 14 2026
    rf"|{_DAY}\s+{_MONTH}(?:,?\s+\d{{4}})?"          # 14 Nov 2026
    rf"|{_MONTH}\s+\d{{4}})$", re.I)                 # November 2026
_PIECE_SPLIT = re.compile(r"[-_.:/#]+")
# One word of a common noun phrase: lowercase letters, with an inner hyphen
# or apostrophe allowed ("follow-up", "builder's").
_NOUN_WORD = re.compile(r"^[^\W\d_]+(?:[-'’][^\W\d_]+)*$")


def _is_hash(piece: str) -> bool:
    return bool(_HEX.match(piece)) and any(c.isdigit() for c in piece)


def code_like(name: str | None) -> bool:
    """True when `name` reads as an identifier rather than a name: a hash or
    opaque id, a short letter-and-digit code, a ticket, series ("QA-B") or
    milestone id, a version, a date or a bare number."""
    s = " ".join((name or "").split())
    if not s:
        return False
    if any(c.isdigit() for c in s) and not any(c.isalpha() for c in s):
        return True                 # 42, 2026-11-14, 11/14, 12:30, 1,000
    if _MONTH_DATE.match(s) or _MILESTONE.match(s):
        return True
    if " " in s:
        return False                # any other multi-word name is a name
    if _UUID.match(s) or _is_hash(s):
        return True
    if _COMPACT_ID.match(s) or _TICKET_ID.match(s) or _VERSION.match(s) \
            or _SERIES_ID.match(s):
        return True
    # An opaque id with separators: wf_079a6cb8-b9e, agent-a5c3f8cf40e5932b7.
    return any(_is_hash(p) for p in _PIECE_SPLIT.split(s) if p)


def common_noun_like(name: str | None) -> bool:
    """True when every word of `name` is lowercase and alphabetic: a noun
    phrase, not a proper name. A name with a capital, a digit, an underscore
    or a dot is not one, and neither is a script without letter case."""
    words = (name or "").split()
    if not words:
        return False
    return all(_NOUN_WORD.match(w) and w.islower() for w in words)


# Money: "$49", "€1,200.50", "$1.2M", "49 USD", "12 dollars". A bare
# amount with a suffix ("3M", "100k") is not one: 3M is a company.
_CURRENCY = r"[$€£¥₹]"
_AMOUNT = r"\d+(?:[.,]\d+)*"
_MONEY = re.compile(
    rf"^(?:{_CURRENCY}\s*{_AMOUNT}\s*(?:[kmb]|bn|mm|million|billion|thousand)?"
    rf"|{_AMOUNT}\s*(?:[kmb]|bn|mm)?\s*(?:usd|eur|gbp|jpy|dollars?|euros?|"
    rf"pounds?|bucks))$", re.I)
_PERCENT = re.compile(rf"^[-+]?{_AMOUNT}\s*(?:%|percent|pct|bps)$", re.I)
_TIME = re.compile(r"^\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)$", re.I)
# Whole weekday names only: "Sun" and "Mon" are names too.
_WEEKDAY = re.compile(
    r"^(?:(?:next|last|this)\s+)?(?:mon|tues|wednes|thurs|fri|satur|sun)day$",
    re.I)
_RELATIVE_DAY = re.compile(r"^(?:today|tomorrow|yesterday|tonight)$", re.I)
# A duration: "13 weeks", "90 days", "6-month", "48 hrs".
_DURATION = re.compile(
    rf"^{_AMOUNT}\s*-?\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|"
    r"weeks?|wks?|months?|mos?|quarters?|years?|yrs?)$", re.I)
# Payment terms: "net 60", "Net 30", "net30".
_NET_TERMS = re.compile(r"^net\s*\d{1,3}(?:\s*days?)?$", re.I)
# A change by a percentage: "6 percent increase", "5% cut".
_PERCENT_CHANGE = re.compile(
    rf"^[-+]?{_AMOUNT}\s*(?:%|percent|pct|bps)\s+(?:increase|decrease|rise|"
    r"drop|cut|raise|hike|discount|markup|reduction|decline|gain|growth|"
    r"jump|fall)$", re.I)
# A quarter or a fiscal year: "Q3", "Q3 2026", "Q4 FY26", "FY2026", "FY 26".
_PERIOD = re.compile(
    r"^(?:q[1-4](?:\s*(?:fy)?\s*'?\d{2}(?:\d{2})?)?"
    r"|fy\s*'?\d{2}(?:\d{2})?(?:\s*q[1-4])?)$", re.I)


def value_literal(name: str | None) -> bool:
    """True when `name` is a value rather than a thing: an amount of money,
    a percentage or a change by one, a date, a weekday, a clock time, a
    duration, payment terms, a quarter or fiscal year, or a bare number. A
    claim's object ("$49", "2026-08-15", "Friday", "13 weeks", "net 60",
    "Q3") is one; it is never an entity of its own."""
    s = " ".join((name or "").split())
    if not s:
        return False
    if any(c.isdigit() for c in s) and not any(c.isalpha() for c in s):
        # 42, 1,000, 3.5, 2026-11-14, 11/14, 12:30, $49, -3%
        return True
    return bool(_MONEY.match(s) or _PERCENT.match(s) or _TIME.match(s)
                or _MONTH_DATE.match(s) or _WEEKDAY.match(s)
                or _RELATIVE_DAY.match(s) or _DURATION.match(s)
                or _NET_TERMS.match(s) or _PERCENT_CHANGE.match(s)
                or _PERIOD.match(s))


# Company legal forms, from every jurisdiction the list knows. Generic
# vocabulary: the same for every seat in every industry. Compared as the
# last word(s) of a name, after at least one word of the name itself.
#
# Matched in any letter case: forms that are never an ordinary word.
LEGAL_FORMS = frozenset({
    # US / UK / Commonwealth
    "llc", "l.l.c.", "llp", "pllc", "inc", "inc.", "incorporated", "corp",
    "corp.", "corporation", "co.", "company", "ltd", "ltd.", "limited", "plc",
    "ulc", "cic", "p.c.", "l.p.", "pty", "pty ltd", "pty. ltd.", "pvt ltd",
    "pvt. ltd.", "private limited", "pte ltd", "pte. ltd.",
    # German-speaking
    "gmbh", "ggmbh", "kgaa", "e.v.",
    # French / Spanish / Portuguese / Italian / Latin America
    "s.a.", "s.a.s.", "sas", "sarl", "s.a.r.l.", "s.à r.l.", "s.à.r.l.",
    "snc", "s.p.a.", "s.r.l.", "srl", "s.l.", "s.a.u.", "ltda", "ltda.",
    "s/a", "s.a. de c.v.",
    # Benelux, Nordics, Central and Eastern Europe
    "b.v.", "n.v.", "oyj", "a/s", "asa", "aps", "sp. z o.o.", "d.o.o.",
    "s.r.o.", "a.s.", "kft.", "zrt.", "nyrt.", "ehf.", "hf.", "jsc", "ooo",
    "pao", "pjsc", "d.d.", "s.a.b. de c.v.",
    # Asia
    "k.k.", "g.k.", "y.k.", "sdn bhd", "sdn. bhd.", "bhd", "tbk",
})
# Matched only in their usual case, only as the LAST word of a name of two
# words or more, and only after a word that starts with a capital or a
# digit: short forms that are also abbreviations, words or initials
# ("Plan B AB testing", a person "Ana Sa", "iPhone SE", "Day Spa").
SHORT_LEGAL_FORMS = frozenset({
    "AB", "AS", "SA", "AG", "SE", "PT", "KK", "NV", "BV", "SL", "PC", "LP",
    "KG", "eG", "Oy", "SpA",
})
# A short form is also an everyday abbreviation or initials ("Gaming PC",
# "Windows SE", a person "Maria AS"), and a one-word name before it cannot
# tell which ("Volvo AB" is the company): such a name is a company only
# when the evidence agrees (`org_by_evidence`). Two words or more before
# it ("Reyes & Kahn PC", "Banco Rojo SA") read as a name on their own.
#
# Forms written first, in Indonesia and the CIS ("PT Astra Graphia",
# "OOO Romashka", "PJSC Sberbank"), in capitals, before a capitalised word.
# "PT" and "AO" are initials as often ("PT Barnum", "AO Smith"): they count
# like a short form after one word.
PREFIX_LEGAL_FORMS = frozenset({"OOO", "JSC", "PJSC", "AO", "PT"})
_AMBIGUOUS_PREFIX_FORMS = frozenset({"PT", "AO"})
# Forms that are ordinary English words: the name before them must read as
# a name, not a lowercase phrase ("the company", "a limited").
_WORD_LEGAL_FORMS = frozenset({
    "company", "limited", "incorporated", "corporation", "private limited"})
_LEGAL_FORM_WORDS = max(len(f.split()) for f in LEGAL_FORMS)
# A word right before the form that makes it a noun of a sentence, not the
# end of a name: "form an LLC", "my PC", "the company".
_DETERMINERS = frozenset({
    "a", "an", "the", "my", "our", "your", "their", "his", "her", "its",
    "this", "that", "these", "those", "no", "any", "each", "every", "some"})
# What a name lifted from running text can carry around it: quotes,
# brackets, and the punctuation that closes a clause ("Acme LLC.",
# "(Northwind, Inc.)").
_WRAPPERS = "\"'“”‘’«»()[]{}"
_CLOSERS = ",;:!?"


def _legal_tail(tail: str, head: list[str]) -> bool:
    if tail in SHORT_LEGAL_FORMS:
        first = head[-1][:1]
        return first.isupper() or first.isdigit()
    form = tail.casefold()
    if form not in LEGAL_FORMS:
        return False
    return not (form in _WORD_LEGAL_FORMS
                and common_noun_like(" ".join(head)))


def legal_form(name: str | None) -> str | None:
    """How `name` carries a company's legal form: "form" when it ends in
    one ("Acme LLC", "Northwind, Inc.", "Wolnica Sp. z o.o.", "Banco
    Rojo SA") or starts with an unambiguous one ("OOO Romashka"); "short"
    when the only form is a short or ambiguous one next to a single word
    ("Volvo AB", "Gaming PC", "PT Astra"), which needs the evidence to
    agree; None when it has none. See `LEGAL_FORMS`, `SHORT_LEGAL_FORMS`
    and `PREFIX_LEGAL_FORMS`. A sentence's own full stop, quotes or
    brackets around the name do not hide the form ("Acme LLC.", "(Acme
    Inc.)")."""
    s, prev = " ".join((name or "").split()), None
    while s and s != prev:
        prev, s = s, s.strip(_WRAPPERS).rstrip(_CLOSERS).strip()
    words = " ".join(s.replace(",", ", ").split()).split(" ")
    if len(words) < 2:
        return None
    for n in range(min(_LEGAL_FORM_WORDS, len(words) - 1), 0, -1):
        head = [w.rstrip(",") for w in words[:-n]]
        head = [w for w in head if w]
        if not head or not any(c.isalnum() for c in "".join(head)):
            continue
        if head[-1].casefold() in _DETERMINERS:
            continue
        tail = " ".join(words[-n:])
        # "LLC." at the end of a sentence is "LLC"; "Co." keeps its dot
        # (a bare "Co" is no form).
        for t in (tail, tail[:-1] if tail.endswith(".") else ""):
            if t and _legal_tail(t, head):
                return "short" if (t in SHORT_LEGAL_FORMS
                                   and len(head) == 1) else "form"
    first, rest = words[0], [w for w in words[1:] if w]
    if first in PREFIX_LEGAL_FORMS and rest \
            and (rest[0][:1].isupper() or rest[0][:1].isdigit()):
        return "short" if (first in _AMBIGUOUS_PREFIX_FORMS
                           and len(rest) == 1) else "form"
    return None


def company_name(name: str | None) -> bool:
    """True when `name` carries a company's legal form (`legal_form`):
    "Acme LLC", "Volvo AB", "OOO Romashka". The shape alone: a short form
    next to one word ("Volvo AB", "Gaming PC") is a company only when the
    evidence agrees, which `org_by_evidence` decides."""
    return legal_form(name) is not None


# --------------------------------------------------- code-shaped names --
# G1: a code-shaped name ("3M", "S3", "H100", "K2") keeps the org, tool or
# place kind the model gave it only when (i) one sighting proposed that kind
# with model confidence >= code_kind_min_conf() and (ii) a second, independent
# sighting agrees: one from another session or another source class that
# proposed the same kind. The deterministic signals (an email domain, a
# filing, the user) decide before this rule is reached. Otherwise the name
# is a `label`, and it waits for recurrence like any code-like name.
_CODE_NAMED_KINDS = ("org", "tool", "place")
_DEFAULT_CODE_KIND_MIN_CONF = 0.8


def code_kind_min_conf() -> float:
    """QUILL_ENTITY_CODE_KIND_MIN_CONF (default 0.8), read at call time.

    Over the 848 entity sightings of 26 stores (24 of them synthetic seats
    from unrelated industries) the extraction model almost always stated
    0.9 or more for an org or a tool, and went below 0.8 only when it
    hedged (11 sightings), so 0.8 keeps the first and drops the second."""
    try:
        v = float(os.environ.get("QUILL_ENTITY_CODE_KIND_MIN_CONF",
                                 _DEFAULT_CODE_KIND_MIN_CONF))
    except (TypeError, ValueError):
        return _DEFAULT_CODE_KIND_MIN_CONF
    return min(1.0, max(0.0, v))


def _stated_confidence(row: dict) -> float | None:
    """The confidence the model stated, or None (no default: a sighting that
    stated none is no strong sighting)."""
    try:
        c = row.get("confidence")
        return None if c is None else float(c)
    except (TypeError, ValueError):
        return None


def code_named_kind(store, rows: list[dict],
                    summary: dict | None = None) -> str | None:
    """The org, tool or place kind a code-shaped name keeps (G1), or None
    when it is a `label`. `rows` are its ledger sightings; `summary` the
    counts kept for it after older sightings expired (`entity_candidates`). Only
    sightings from sources that may mint count: screen text and assistant
    notes repeat what another source said, they do not agree with it."""
    from app.services import entity_candidates as cand
    elig = [r for r in rows if cand.mint_eligible(r)]
    labels = cand.session_labels(
        store, [float(r.get("observed_at") or 0.0) for r in elig])
    floor = code_kind_min_conf()
    past = (summary or {}).get("kinds") or {}
    for kind in _CODE_NAMED_KINDS:
        mine = [(labels[i], r.get("source_class") or "",
                 _stated_confidence(r))
                for i, r in enumerate(elig)
                if _norm_kind(r.get("proposed_kind")) == kind]
        old = past.get(kind) or {}
        old_strong = float(old.get("c") or 0.0) >= floor
        old_n = int(old.get("s") or 0)
        strong = [m for m in mine if m[2] is not None and m[2] >= floor]
        # Two of today's sightings, apart in session or source class.
        if any(s is not o and (s[0] != o[0] or s[1] != o[1])
               for s in strong for o in mine):
            return kind
        # Or one side of the pair expired into the summary: those sightings
        # are older than the TTL, so another session.
        if (strong and old_n) or (old_strong and mine):
            return kind
        if old_strong and (old_n >= 2 or len(old.get("cls") or ()) >= 2):
            return kind
    return None


def weak_name(name: str | None, kinds=()) -> bool:
    """A name too generic to promote on what the user did not type. A
    lowercase phrase of two words or more ("pet groomers", "bowling alleys")
    always is. A single lowercase word is weak only when no sighting called
    it a tool or an org: "numpy" and "pgvector" read as a tool are names,
    "upholstery" read as a project is not."""
    if not common_noun_like(name):
        return False
    if len((name or "").split()) > 1:
        return True
    return not any(_norm_kind(k) in ("tool", "org") for k in kinds)


# ------------------------------------------------------------ kind vote --
# How much one sighting is worth, by where it was read. What the user typed
# is the best evidence of what a name is to them; mail is written to them;
# speech and meetings are said around them; a document names things; an
# assistant summarises; screen text was only on display.
TYPED_WEIGHT = 1.0
_CLASS_WEIGHTS = {
    "email": 0.8, "connector_mail": 0.8, "exhaust": 0.8,
    "direct_message": 0.7, "private_conversation": 0.7,
    "meeting_transcript": 0.7, "calendar": 0.7, "connector_calendar": 0.7,
    "user_authored_document": 0.5, "shared_document": 0.5,
    "presentation": 0.5, "code_editor": 0.5, "connector_item": 0.5,
    "assistant_capture": 0.35, "research_answer": 0.35, "agent_fetch": 0.35,
    "peer_answer": 0.35, "peer_signal": 0.35, "fleet_agent": 0.35,
    "org_coordinator": 0.35,
}
SCREEN_WEIGHT = 0.15
_DEFAULT_CLASS_WEIGHT = 0.5
DEFAULT_CONFIDENCE = 0.6
# The incumbent model kind starts with this much behind it, and another
# kind must beat it by FLIP_MARGIN with at least MIN_DISSENT sightings of
# its own: one odd sighting never flips a kind, and two sightings that
# disagree with each other cannot make it flap. Defaults; the vote reads
# QUILL_ENTITY_KIND_INCUMBENT_PRIOR / _FLIP_MARGIN / _MIN_DISSENT at call
# time (`_flip_rule`).
INCUMBENT_PRIOR = 0.5
FLIP_MARGIN = 0.3
MIN_DISSENT = 2


def _env_number(var: str, default, cast=float, lo=0.0):
    try:
        return max(lo, cast(os.environ.get(var, default)))
    except (TypeError, ValueError):
        return default


def _flip_rule() -> tuple[float, float, int]:
    """(incumbent prior, flip margin, minimum dissenting sightings)."""
    return (_env_number("QUILL_ENTITY_KIND_INCUMBENT_PRIOR", INCUMBENT_PRIOR),
            _env_number("QUILL_ENTITY_KIND_FLIP_MARGIN", FLIP_MARGIN),
            _env_number("QUILL_ENTITY_KIND_MIN_DISSENT", MIN_DISSENT,
                        cast=int, lo=1))
# The vote reads at most this many sightings, the latest ones.
MAX_MENTIONS = 500
# What a sighting's kind says nothing with ("other", "?"): no vote.
_NO_VOTE = frozenset({"", "other", "?", "unknown", "none"})
# Who vouches for a kind, weakest first (Store.KIND_SOURCE_RANK).
_MODEL_RANK = 1
_RULE_RANK = 2


@dataclass
class Verdict:
    """What the evidence says a name is. `source` is who would vouch for
    it: `filing`, `rule` (a deterministic prior), `model` (the vote), or the
    row's own source when a rule, a filing or the user set it and the vote
    may not touch it. `breakdown` says why."""
    kind: str | None = None
    source: str = "model"
    confidence: float | None = None
    breakdown: dict = field(default_factory=dict)


def source_weight(row: dict) -> float:
    """What one ledger sighting is worth, by where it was read."""
    if row.get("typed"):
        return TYPED_WEIGHT
    if (row.get("event_source") or "").lower().startswith("desktop.screen"):
        return SCREEN_WEIGHT
    cls = row.get("source_class") or ""
    if cls in ("browser_article", "news_page", "social_feed",
               "advertisement", "terminal", "system_ui", "filesystem",
               "social_composer", "notification"):
        return SCREEN_WEIGHT
    return _CLASS_WEIGHTS.get(cls, _DEFAULT_CLASS_WEIGHT)


def _confidence(row: dict) -> float:
    try:
        c = float(row.get("confidence"))
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE
    return min(1.0, max(0.0, c))


def _norm_kind(kind: str | None) -> str | None:
    """A sighting's proposed kind in stored form, or None for no vote."""
    k = (kind or "").strip().lower()
    if k in _NO_VOTE:
        return None
    from app.services.name_quality import normalize_entity_kind
    return normalize_entity_kind(k, unknown=None)


def _one_filing_project(rows: list[dict]) -> str | None:
    """The filing project every sighting carries, when they all carry the
    same one; else None."""
    from app.services import projects
    keys = {projects.norm(r.get("filing_project")) for r in rows}
    if not rows or len(keys) != 1:
        return None
    only = next(iter(keys))
    return only or None


def scope_hash(project: str | None) -> str:
    """A text-free token for the filing project a set of sightings shared:
    sha256 of its `projects.norm` key, "" for none. What a candidate summary
    keeps instead of the project's name."""
    import hashlib
    from app.services import projects
    key = projects.norm(project)
    return hashlib.sha256(key.encode("utf-8")).hexdigest() if key else ""


def mention_kind(proposed: str | None, *, code: bool,
                 scoped: bool) -> str | None:
    """The kind one sighting votes for. A model "project" is never a vote
    for `project`: the model calls anything someone is busy with a project
    ("k9", "the pilot", "a new tariff rule"). Only a filing, the user or
    the project registry make a project. It is a `label` when the name is
    code-like or every sighting sits inside one filed project, else a topic
    (`idea`)."""
    k = _norm_kind(proposed)
    if k == "project":
        return "label" if (code or scoped) else "idea"
    return k


def _is_filed_name(names, entity: dict | None) -> bool:
    if entity and (entity.get("kind") or "") == "project" \
            and (entity.get("kind_source") or "") in ("filing", "user"):
        return True
    from app.services import projects
    try:
        keys = projects.filed_keys()
    except Exception:
        return False
    return any(projects.norm(n) in keys for n in names if n)


# The email-domain prior, per store: the name keys of every org a known
# person's address domain names, rebuilt when a contact point is added or
# after _CACHE_TTL_S. The vote runs on every bound sighting; scanning the
# contact table each time is what this saves.
_CACHE_TTL_S = 60.0
_domain_cache: dict[str, tuple[float, int, frozenset]] = {}


def _domain_key(name: str) -> str:
    """A name as a domain label spells it: the name key without spaces
    ("Clearbrook Dental" and clearbrookdental.com, "Acme Robotics" and
    acme-robotics.com are one org)."""
    from app.services.kg_keys import entity_name_key
    return entity_name_key(name).replace(" ", "")


def email_domain_org_keys(store) -> frozenset[str]:
    """`_domain_key`s of the orgs known people's email domains name ("acme"
    for jane@acme.com; never a free-mail domain), cached per store."""
    import time as _time
    path = str(getattr(store, "db_path", "") or id(store))
    try:
        mark = int(store.contact_points_mark())
    except Exception:
        return frozenset()
    now = _time.monotonic()
    hit = _domain_cache.get(path)
    if hit is not None and hit[1] == mark and now - hit[0] < _CACHE_TTL_S:
        return hit[2]
    try:
        domains = store.contact_email_domains()
        from app.services.people_pipeline import org_from_email_domain
    except Exception:
        return frozenset()
    keys = set()
    for d in domains:
        org = org_from_email_domain(f"x@{d}")
        if org:
            keys.add(_domain_key(org))
    out = frozenset(k for k in keys if k)
    _domain_cache[path] = (now, mark, out)
    return out


def _is_email_domain_org(store, name: str) -> bool:
    """A known person's email domain is this name ("Acme" for a@acme.com,
    "Clearbrook Dental" for b@clearbrookdental.com): the same rule that
    mints email-domain orgs, read without the spaces a domain cannot
    have."""
    key = _domain_key(name)
    return bool(key) and key in email_domain_org_keys(store)


def org_by_evidence(store, names, kinds=None) -> str | None:
    """Why a name is an org whatever else it is, or None: a company's legal
    form in the name ("company_name") or a known person's email domain
    ("email_domain"). `names` are the canonical name and its confirmed
    aliases. A filed name that is an org by evidence stays an org.

    A short form next to one word ("Volvo AB", "Gaming PC": `legal_form`
    "short") counts only when the evidence agrees it is an org: a sighting
    proposed `org` (`kinds`, the kinds the caller's sightings proposed; by
    default the ledger's under each name), or the word before it is a known
    person's email domain ("Volvo" for a@volvo.com)."""
    names = [n for n in names if n]
    forms = [legal_form(n) for n in names]
    if "form" in forms:
        return "company_name"
    if "short" in forms and _short_form_vouched(
            store, [n for n, f in zip(names, forms) if f == "short"], kinds):
        return "company_name"
    if any(_is_email_domain_org(store, n) for n in names[:3]):
        return "email_domain"
    return None


def _short_form_vouched(store, names, kinds=None) -> bool:
    if kinds is None:
        from app.services.kg_keys import entity_name_key
        kinds = []
        for n in names:
            key = entity_name_key(n)
            try:
                kinds.extend(r.get("proposed_kind") for r in
                             store.entity_mentions(name_key=key,
                                                   limit=MAX_MENTIONS,
                                                   newest_first=True))
                kinds.extend(((store.entity_candidate_summary(key) or {})
                              .get("kinds") or {}).keys())
            except Exception:
                pass
    if any(_norm_kind(k) == "org" for k in kinds or ()):
        return True
    for n in names:
        words = [w for w in n.split() if w]
        head = words[1] if words and words[0] in PREFIX_LEGAL_FORMS \
            else (words[0] if words else "")
        if head and _is_email_domain_org(store, head.strip(_WRAPPERS + ",")):
            return True
    return False


def _user_touched(store, entity: dict | None) -> bool:
    """Did the user work on this row (`Store.entity_user_touched`)? Unknown
    (no row, or a store that cannot say) reads as touched: a prior then
    leaves a legacy kind alone, which is the safe side."""
    if not entity or entity.get("id") is None:
        return False
    try:
        return bool(store.entity_user_touched(int(entity["id"])))
    except Exception:
        return True


def _prior_names(store, entity: dict | None, name: str) -> list[str]:
    """The spellings the priors read: the canonical name and the confirmed
    aliases, never every spelling a sighting was folded in under (one
    mis-folded alias must not re-kind a row)."""
    out = [name] if name else []
    if entity is not None:
        try:
            rows = store.list_entity_aliases(entity_id=int(entity["id"]),
                                             confirmed=True, limit=50)
        except Exception:
            rows = []
        out.extend(r["alias"] for r in rows if r.get("alias"))
    return list(dict.fromkeys(out))


def vote(store, *, name_key: str | None = None,
         entity_id: int | None = None, name: str | None = None) -> Verdict:
    """What kind of thing is this name? Over every sighting in the ledger
    (`entity_id`: the entity's own; `name_key`: a name with no row yet).

    Deterministic priors come first and win outright. They read the
    canonical name and its confirmed aliases only (`_prior_names`):

    1. a filed project (the shared or filed tier of the project registry,
       or a project row a filing or the user made) is a `project`, unless
       it is an org by evidence (`org_by_evidence`: a legal form such as
       "LLC", or an email domain), which stays an `org`;
    2. an org by the same evidence, filed or not: an email domain of a
       known person ("Acme" for a@acme.com), or a company's legal form in
       the name ("Brannock Hale LLC", the rule the repair uses), is an
       `org`;
    3. a value ("$49", "Friday") is a `thing`;
    4. a code-like name ("k9", "Phase 1", "4e7a1f0c") is a `label`, unless
       two independent sightings agree it is an org, a tool or a place
       (`code_named_kind`: "3M" read as an org at 0.9 in two sessions
       stays an org; read once, it is a label until it recurs).

    A prior's verdict has source `rule` (`filing` for the filed project),
    and that is what the row stores (`retype`, the candidate gate's
    promotion). A `rule` row is voted on again by the priors only: a prior
    that still holds keeps or changes it, and a code-shaped row whose
    sightings now agree on an org, tool or place goes to the vote again
    (the G1 rule allows it); otherwise it keeps its kind, and the sighting
    vote never overrides it.

    Otherwise each sighting votes for its proposed kind with weight model
    confidence (0.6 when unknown) x source weight (`source_weight`). A
    model "project" votes for `label` or `idea` (`mention_kind`), so the
    vote never yields `project`. Sightings that expired while the name was
    a candidate are still counted, from the summary kept for its name key
    (`Store.entity_candidate_summary`), before and after it is promoted.

    For an entity, its current kind is the incumbent: it starts with
    INCUMBENT_PRIOR behind it, and a flip needs MIN_DISSENT sightings for
    the new kind and a lead of FLIP_MARGIN (all three read from the
    environment, `_flip_rule`). A row whose kind a filing or the user set
    is never overridden: the verdict is its own kind and source. A row
    from before kind provenance (`kind_source` NULL: the user may have
    chosen that kind) is changed by priors 1-3 only (prior 2 by the email
    domain only: an unfiled legal form is the repair's question), never by
    the code-shape prior 4 or the vote (`reason: legacy`), and not by those
    priors either once the user worked on it (`Store.entity_user_touched`:
    a rename, an alias, a pin, a detail...; `reason: legacy_user_touched`).

    `name` is the spelling the name-shape priors read for a candidate (the
    one a node would carry); by default the latest sighting's."""
    from app.services.kg_keys import entity_name_key
    from app.storage import Store
    entity = None
    summary = None
    if entity_id:
        try:
            entity = store.get_entity(int(entity_id))
        except Exception:
            entity = None
        if entity is None:
            return Verdict(None, breakdown={"reason": "no_entity"})
    if entity is not None:
        rows = store.entity_mentions(entity_id=int(entity_id),
                                     limit=MAX_MENTIONS, newest_first=True)
        if not rows:
            rows = store.entity_mentions(
                name_key=entity_name_key(entity.get("name") or ""),
                status="pending", limit=MAX_MENTIONS, newest_first=True)
        name = entity.get("name") or ""
        name_key = entity_name_key(name)
    else:
        # A candidate's evidence is what is still waiting under the key; a
        # bound row under the same key belongs to some entity already.
        rows = store.entity_mentions(name_key=name_key or "",
                                     status="pending", limit=MAX_MENTIONS,
                                     newest_first=True)
        name = (name or "").strip() or (rows[0]["name"] if rows else "")
    # What sightings that expired before the name became a node added up
    # to: evidence for the candidate and, once promoted, for the entity.
    try:
        summary = store.entity_candidate_summary(name_key or "")
    except Exception:
        summary = None
    names = _prior_names(store, entity, name)
    out = Verdict(breakdown={"mentions": len(rows)})
    bd = out.breakdown
    if summary:
        bd["summarised"] = int(summary.get("mentions") or 0)

    incumbent = None
    legacy = False
    rule_row = False
    if entity is not None:
        from app.services.name_quality import normalize_entity_kind
        incumbent = normalize_entity_kind(entity.get("kind"))
        bd["incumbent"] = incumbent
        src = (entity.get("kind_source") or "").strip().lower() or None
        if Store.kind_source_rank(src) > _RULE_RANK:
            out.kind, out.source = incumbent, src
            out.confidence = entity.get("kind_confidence")
            bd["reason"] = "held"
            return out
        legacy = src is None
        rule_row = Store.kind_source_rank(src) == _RULE_RANK

    # Deterministic priors.
    prior = None
    seen_kinds = [r.get("proposed_kind") for r in rows] \
        + list(((summary or {}).get("kinds") or {}).keys())
    if _is_filed_name(names, entity):
        why = org_by_evidence(store, names, seen_kinds)
        prior = (("org", "rule", 1.0, f"filed_{why}") if why
                 else ("project", "filing", 1.0, "filed_project"))
    elif any(_is_email_domain_org(store, n) for n in names[:3]):
        prior = ("org", "rule", 1.0, "email_domain")
    elif not legacy and org_by_evidence(store, names, seen_kinds):
        # A company's legal form. Not on a row from before kind provenance
        # (D4): there the reviewed repair asks (rule `legal_form`).
        prior = ("org", "rule", 1.0, "company_name")
    elif value_literal(name):
        prior = ("thing", "rule", 1.0, "value_literal")
    elif code_like(name) and legacy:
        # A legacy code-shaped row ("ZK42" a tool, "k7" a project) may be
        # the user's own choice from before provenance: the shape rule does
        # not change it, the reviewed repair plan does.
        bd["code_like"] = True
    elif code_like(name):
        named = code_named_kind(store, rows, summary)
        if named is None:
            prior = ("label", "rule", 1.0, "code_like")
        else:
            bd["code_kind"] = named
    if prior is not None and legacy and _user_touched(store, entity):
        # A row from before kind provenance that the user worked on (renamed
        # it, typed an alias, pinned it, wrote a detail...): its kind may be
        # their own choice, so no prior changes it; the reviewed repair asks.
        out.kind = incumbent
        out.confidence = entity.get("kind_confidence")
        bd["reason"] = "legacy_user_touched"
        bd["prior"] = prior[3]
        return out
    if prior is not None:
        out.kind, out.source, out.confidence, bd["reason"] = prior
        return out
    if rule_row and not bd.get("code_kind"):
        # A rule set this kind and no prior changes it: the sighting vote
        # never overrides a rule.
        out.kind, out.source = incumbent, "rule"
        out.confidence = entity.get("kind_confidence")
        bd["reason"] = "held"
        return out
    if bd.get("code_kind"):
        # A code-shaped name whose sightings agree on a kind (G1): the
        # code-shape rule lets the vote below decide, and vouches for it.
        out.source = "rule"

    project = _one_filing_project(rows)
    scoped = project is not None and (
        not summary or summary.get("scope") == scope_hash(project))
    code = code_like(name)
    scores: dict[str, float] = {}
    counts: dict[str, int] = {}
    confs: dict[str, float] = {}
    last: dict[str, float] = {}
    for r in rows:
        k = mention_kind(r.get("proposed_kind"), code=code, scoped=scoped)
        if not k:
            continue
        c = _confidence(r)
        scores[k] = scores.get(k, 0.0) + c * source_weight(r)
        counts[k] = counts.get(k, 0) + 1
        confs[k] = confs.get(k, 0.0) + c
        last[k] = max(last.get(k, 0.0), float(r.get("observed_at") or 0.0))
    # Sightings that expired still vote, from the summary: the same weights,
    # added up when they were folded.
    for raw, h in ((summary or {}).get("kinds") or {}).items():
        k = mention_kind(raw, code=code, scoped=scoped)
        n = int(h.get("n") or 0)
        if not k or n <= 0:
            continue
        scores[k] = scores.get(k, 0.0) + float(h.get("w") or 0.0)
        counts[k] = counts.get(k, 0) + n
        confs[k] = confs.get(k, 0.0) + float(h.get("cs") or 0.0)
        last.setdefault(k, float(summary.get("last_seen") or 0.0))
    bd["scores"] = {k: round(v, 4) for k, v in sorted(scores.items())}
    bd["counts"] = dict(sorted(counts.items()))
    if scoped:
        bd["filing_project"] = project
    total = sum(scores.values())
    if legacy:
        # Kind chosen before provenance existed, possibly by the user: the
        # sightings are recorded and shown, but only a prior (above) or the
        # reviewed repair plan changes it.
        out.kind = incumbent
        out.confidence = entity.get("kind_confidence")
        bd["reason"] = "legacy"
        return out
    if not scores:
        out.kind = incumbent or "idea"
        out.confidence = None
        bd["reason"] = "no_votes"
        return out
    best = max(scores, key=lambda k: (scores[k], counts[k], last[k], k))
    # How sure: the winner's share of the weighted vote times how sure the
    # model was, on average, when it said so. One sighting at 0.71 is 0.71.
    share = (round(scores[best] / total * confs[best] / counts[best], 4)
             if total else None)
    if incumbent is None or best == incumbent:
        out.kind, out.confidence = best, share
        bd["reason"] = "vote" if incumbent is None else "confirmed"
        return out
    prior_w, margin, dissent = _flip_rule()
    held = scores.get(incumbent, 0.0) + prior_w
    if counts[best] >= dissent and scores[best] >= held + margin:
        out.kind, out.confidence = best, share
        bd["reason"] = "flipped"
    else:
        out.kind = incumbent
        out.confidence = entity.get("kind_confidence") if entity else None
        bd["reason"] = "incumbent"
    return out


def stored_source(v: Verdict) -> str:
    """The `kind_source` a row stores for verdict `v`: `rule` or `filing`
    when a deterministic prior decided, else `model`."""
    return v.source if v.source in ("rule", "filing") else "model"


def retype(store, entity_id: int) -> Verdict | None:
    """Re-run the vote for one entity and write the result when it differs
    from the row's kind. Only a row whose kind is a model guess, a rule's
    (re-voted by the priors only), or from before kind provenance is ever
    changed, at the rank of what decided (`stored_source`: a prior writes
    at rule rank, the sighting vote at model rank), so a filing or the user
    always stands. Returns the verdict, or None when there is no such
    entity."""
    try:
        ent = store.get_entity(int(entity_id))
    except Exception:
        ent = None
    if not ent:
        return None
    from app.storage import Store
    if Store.kind_source_rank(ent.get("kind_source")) > _RULE_RANK:
        return Verdict(ent.get("kind"), ent.get("kind_source") or "model",
                       ent.get("kind_confidence"), {"reason": "held"})
    v = vote(store, entity_id=int(entity_id))
    from app.services.name_quality import normalize_entity_kind
    if v.kind and v.kind != normalize_entity_kind(ent.get("kind")):
        try:
            ok = store.set_entity_kind(int(entity_id), v.kind,
                                       source=stored_source(v),
                                       confidence=v.confidence)
        except Exception as exc:
            print(f"[entity_typing] kind write skipped ({exc}).")
            ok = False
        v.breakdown["written"] = bool(ok)
    return v
