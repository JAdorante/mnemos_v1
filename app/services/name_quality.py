"""Name-quality gate — keep junk out of the people/entity graph at write time.

The fact extractor runs over noisy speech, vision captions, and document text, so
it hands the resolver strings that aren't names at all: pronouns ("us"), role
words ("curator"), sentence fragments ("set it to 0"), env-var/system tokens
("QUILL_AGENT", "vinceo.ai"), and file paths ("app/services/memory.py"). Ungated,
each becomes a bogus person/entity node that clutters the constellation and
pollutes retrieval.

This is a GENERAL lexical filter — no user-specific data. It judges whether a
string looks like a proper name / named entity, and errs toward KEEPING borderline
cases: a wrong reject drops a real person, so it's precision-over-recall on
rejection (only clear junk is turned away). It's a net, not a guarantee — the
primary defense against document-derived junk is not ingesting code/docs in the
first place (services/documents.py).
"""
from __future__ import annotations

import getpass
import os
import re
from pathlib import Path

# Pronouns + generic placeholders that surface as "people" but name no one.
_GENERIC = frozenset({
    "she", "he", "him", "her", "me", "my", "i", "we", "us", "our", "they",
    "them", "their", "you", "your", "it", "its", "someone", "somebody",
    "anyone", "everyone", "everybody", "nobody", "guys", "folks", "people",
    "user", "users", "new user", "the user", "curator", "founder", "board",
    "member", "not specified", "unspecified", "unknown", "n/a", "na", "none",
    "admin", "agent", "assistant", "team", "everyone else",
})

# Product/system self-tokens — the assistant/product must never become a node.
# Includes the app's OWN UI surface names (page titles): screen capture reading
# our own dashboard minted "Memory Console" as an entity (live, July 28 2026).
_SELF_TOKENS = frozenset({
    "vinceo.ai", "vinceo", "mnemos", "quill", "quill_agent", "exec.ai",
    "memory console", "desktop access", "weekly check-in", "memory changes",
    "constellation field",
})

# Code / path / identifier punctuation, or a file-ext / domain suffix, that no
# human name (and virtually no brand label we store) carries.
_CODE_PUNCT = re.compile(
    r"[\\/(){}\[\]<>=;:|`~^@#$%*]|::|"
    r"\.(py|md|db|json|txt|js|ts|tsx|jsx|csv|log|toml|yaml|yml|ini|cfg|sh|bat|"
    r"ai|com|org|io|net|co|dev)\b",
    re.I)
# ALL_CAPS env-var-ish tokens: QUILL_AGENT, MAX_RETRIES.
_ENVVAR = re.compile(r"^[A-Z0-9]+(_[A-Z0-9]+)+$")
# snake_case identifiers: stack_of_five_layers, todo_list, improvement_loop.
_SNAKE = re.compile(r"[a-z0-9]+_[a-z0-9]+")
# Lowercase function/imperative words that mark a fragment, not a name.
_FUNC = frozenset({
    "and", "or", "the", "a", "an", "to", "of", "as", "is", "are", "was", "in",
    "on", "for", "with", "this", "that", "at", "by", "from", "then", "set",
    "page", "put", "make", "get", "so", "but", "if", "when",
})


def _tokens(name: str) -> list[str]:
    return [t for t in (name or "").split() if t]


def _has_capital(words: list[str]) -> bool:
    return any(w[:1].isupper() for w in words)


def _os_account_names() -> frozenset[str]:
    """Live OS account / home-folder labels — not people. Dynamic so we never
    hardcode a machine username into source (generality gate)."""
    names: set[str] = set()
    for raw in (
        os.environ.get("USERNAME"),
        os.environ.get("USER"),
        os.environ.get("LOGNAME"),
    ):
        if raw and raw.strip():
            names.add(raw.strip().lower())
    try:
        gu = (getpass.getuser() or "").strip()
        if gu:
            names.add(gu.lower())
    except Exception:
        pass
    try:
        home = Path.home().name.strip()
        if home:
            names.add(home.lower())
    except Exception:
        pass
    # Optional comma-separated aliases (e.g. other local account labels).
    extra = (os.environ.get("QUILL_PERSON_EXCLUDE_NAMES") or "").strip()
    for part in extra.split(","):
        p = part.strip().lower()
        if p:
            names.add(p)
    return frozenset(names)


def is_os_account_name(name: str) -> bool:
    """True when `name` matches the live OS account / home folder / exclude list."""
    n = (name or "").strip().lower()
    return bool(n) and n in _os_account_names()


def _shared_reject(n: str) -> bool:
    """Junk that's never a person OR an entity: the product's own name, code /
    path / domain punctuation, env-var-style tokens, and possessive UI labels
    ("My Contacts", "My Files" — app chrome scraped off someone's screen, not
    a thing in the user's world)."""
    low = n.lower()
    if low in _SELF_TOKENS:
        return True
    if low.startswith("my ") and len(n.split()) <= 3:
        return True
    if _CODE_PUNCT.search(n) or _ENVVAR.match(n):
        return True
    return False


def normalize_person_name(name: str) -> str:
    """Title-case alphabetic ASR/OCR names so the capital gate doesn't drop them.

    "justin adorante" → "Justin Adorante". Only 1–2 alphabetic tokens (first /
    first+last) — longer lowercase phrases are role/junk, not names.
    """
    n = (name or "").strip()
    if not n:
        return n
    words = _tokens(n)
    if not words or _has_capital(words):
        return n
    if len(words) > 2:
        return n
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z'-]*", w) for w in words):
        return n
    return " ".join(w[:1].upper() + w[1:].lower() for w in words)


def is_plausible_person(name: str) -> bool:
    n = normalize_person_name((name or "").strip())
    if len(n) < 2:
        return False
    if n.lower() in _GENERIC or _shared_reject(n) or _SNAKE.search(n):
        return False
    if n.lower() in _os_account_names():
        return False
    words = _tokens(n)
    if len(words) > 4:                       # a person's name isn't a sentence
        return False
    if not _has_capital(words):              # real names carry a capitalized token
        return False
    if any(w.lower() in _FUNC for w in words):   # "QA and CTO review", "set it to 0"
        return False
    if any(ch.isdigit() for ch in n):        # digits aren't part of a name
        return False
    return True


def is_plausible_entity(name: str) -> bool:
    """Deliberately permissive — an entity label can be snake_case
    ('alpaca_market_data'), lowercase multi-word ('sync shop campaign'), or a
    tech token ('edge-tts'), all of which are REAL user projects/tools. So this
    rejects only the unambiguous junk: the product's own name, file paths /
    domains / code punctuation, env vars, and over-long paragraph fragments.
    (The snake_case & all-lowercase rules that work for PEOPLE would delete real
    projects here, so they are intentionally not applied.)"""
    n = (name or "").strip()
    if len(n) < 2:
        return False
    if _shared_reject(n):
        return False
    if len(_tokens(n)) > 6:                   # a label, not a paragraph fragment
        return False
    return True


# Corporate / brand suffixes that keep a Title-Case name from looking like a person.
_CORP_SUFFIXES = frozenset({
    "llc", "inc", "ltd", "corp", "co", "plc", "corporation", "company",
    "party", "group", "office", "team", "lab", "labs", "ai", "dev", "app",
    "watch", "capital", "ventures", "partners", "foundation", "university",
    "college", "school", "hospital", "clinic",
})

# Common second (or either) tokens that mark projects/tools/places, not people.
# "Memory Console", "Claude Code", "Project Nexus", "United States", …
_NONPERSON_TOKENS = frozenset({
    "project", "console", "code", "pipeline", "architecture", "laptop",
    "computing", "contacts", "view", "change", "panels", "panel", "manager",
    "terms", "technology", "tech", "cards", "ownership", "properties",
    "market", "activities", "calendar", "solitaire", "studio", "exclude",
    "ghost", "screen", "link", "discussion", "notes", "idea", "overview",
    "connection", "sso", "editor", "document", "interface", "setup", "coast",
    "gen", "robotics", "states", "tool", "tools", "browser", "desktop",
    "home", "work", "design", "chat", "memory", "quantum", "safety",
    "nested", "decision", "loop", "issue", "real", "terminal", "industrywide",
    "election", "integrity", "uniparty", "procurement", "corporate",
    "activity", "symbol", "browser", "agency", "portrait", "klondike",
    "quilk", "studio", "fl", "win11", "notepad", "iphone", "west", "east",
    "united", "google", "microsoft", "my", "the", "a", "an", "of", "and",
    "port", "portdev", "dtc", "crm", "api", "sdk", "ui", "ux", "html",
    "python", "java", "docs", "page", "pages", "campaign", "event",
    "agenda", "suite", "watch", "radar", "sheets", "drive", "chrome",
    "edge", "outlook", "teams", "zoom", "news", "feed", "feeds",
    "pulse", "eats", "insider", "lists", "robots", "service", "venture",
    "pocket", "whale", "uber", "halos", "omniverse", "spacesk", "family",
    "student", "edition", "radar", "soft", "legacy",
})

_TOOL_KINDS = frozenset({
    "product", "software", "app", "service", "platform", "tool",
})
_ORG_KINDS = frozenset({"company", "organization", "org"})
_PLACE_KINDS = frozenset({"location", "venue", "place"})
_CANON_KINDS = frozenset({"project", "org", "idea", "place", "tool", "thing"})


def normalize_entity_kind(kind: str | None, *,
                          unknown: str | None = "idea") -> str | None:
    """Map extractor / legacy kind labels onto the store canonical set.

    product|software|… → tool; company|organization → org; other/empty → idea.
    Pass unknown=None for admin APIs that must reject unrecognized labels
    (e.g. "wizard") instead of silently collapsing them to idea.
    """
    k = (kind or "").strip().lower()
    if k in _TOOL_KINDS:
        return "tool"
    if k in _ORG_KINDS:
        return "org"
    if k in _PLACE_KINDS:
        return "place"
    if k in ("", "other", "?", "unknown", "none"):
        return "idea"
    if k in _CANON_KINDS:
        return k
    return unknown


def is_person_shaped_entity_name(name: str) -> bool:
    """True only for clear first+last person names misfiled as entities.

    Conservative on purpose — false positives hide real projects (Memory
    Console, Project Nexus). Does NOT title-case lowercase phrases; those
    are debris/projects, not people.
    """
    raw = (name or "").strip()
    if not raw:
        return False
    words = _tokens(raw)
    if len(words) != 2:
        return False
    # Must already look Title-Case in the source — do not invent capitals.
    if not all(w[:1].isupper() for w in words):
        return False
    if any(w.lower() in _NONPERSON_TOKENS for w in words):
        return False
    if any(w.lower() in _CORP_SUFFIXES for w in words):
        return False
    # Reject ALLCAPS tokens (NVIDIA, AWS) and mixed brand caps (iPhone).
    if any(len(w) > 1 and w.isupper() for w in words):
        return False
    if any(ch.isdigit() for ch in raw):
        return False
    # Alphabetic name parts only (allow hyphen/apostrophe inside a token).
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z'-]*", w) for w in words):
        return False
    # Final check against the person gate (pronouns, roles, paths, …).
    # Pass the already-capitalized form — do not normalize_person_name first.
    if not is_plausible_person(raw):
        return False
    return True


def should_mint_as_entity(name: str, kind: str | None) -> bool:
    """Write-time gate: person-shaped names must not mint as project/idea."""
    if not is_plausible_entity(name):
        return False
    nk = normalize_entity_kind(kind)
    if is_person_shaped_entity_name(name) and nk not in ("org", "tool", "place"):
        return False
    return True
