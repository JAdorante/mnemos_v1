"""Ground voice-transcribed recipients against real contacts.

Speech-to-text emits unconstrained guesses — you say "text Abby", the recognizer
writes "Abby Nagle", but the real contact is "Abby Nengel". Typing that guess
verbatim into Phone Link's To field matches no one (or the wrong person). This
module snaps the guess to the closest *known* contact using string + token
similarity — no network, no LLM. The LLM tiebreak (llm.resolve_recipient) is only
called when this can't decide confidently, and body cleanup lives there too.

Everything here is pure and deterministic so it can be unit-tested without a
browser, a phone, or an API key.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# A trailing timestamp (and everything after it — the message preview) in a
# Phone Link list row, e.g. "Abby Nengel i love you 3:35 PM".
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*[ap]\.?m\.?\b", re.I)

# Sources that appear in Phone Link's Notifications panel but are apps/services,
# not people you'd text. Matched (prefix) against the cleaned name head. This is
# a best-effort denylist — the real safeguard is the similarity floor in
# llm.resolve_recipient, which refuses to "correct" a name to a poor match.
_NOT_PEOPLE = {
    # social / news / media
    "x", "twitter", "threads", "reddit", "instagram", "facebook", "tiktok",
    "snapchat", "youtube", "linkedin", "pinterest", "twitch", "discord", "slack",
    "whatsapp", "telegram", "new york post", "breaking911", "espn", "cnn", "fox news",
    "the athletic", "bleacher report", "news", "apple news",
    # delivery / rideshare / travel
    "uber", "uber eats", "lyft", "doordash", "grubhub", "instacart", "postmates",
    "airbnb", "expedia", "ticketmaster", "stubhub",
    # money
    "venmo", "paypal", "cash app", "zelle", "robinhood", "coinbase", "chase",
    "bank of america", "wells fargo", "capital one", "american express", "amex",
    "wallet", "apple pay", "google pay", "klarna", "afterpay",
    # shopping / big tech / system
    "amazon", "ebay", "walmart", "target", "costco", "apple", "google", "microsoft",
    "netflix", "spotify", "hulu", "disney", "life360", "reminders", "outlook",
    "gmail", "mail", "calendar", "photos", "maps", "weather", "app store",
    # noisy X/Twitter accounts seen in this inbox (not contacts)
    "il donaldo trumpo", "warren buffett", "christian heiens",
}


def clean_contact_name(raw: str) -> str:
    """Pull a plausible person-name out of a Phone Link list entry, which often
    concatenates the contact name with a message preview and a timestamp:
    "Abby Nengel 💜 i love you 3:35 PM" -> "Abby Nengel".

    Names are capitalized; message previews reach a lowercase word quickly, so we
    keep only the leading capitalized (or all-caps) tokens and stop at the first
    lowercase word or digit. This becomes the recipient we actually type, so it
    must be the real contact name — not the preview."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.splitlines()[0]                    # first visual line only
    s = _TIME_RE.split(s)[0]                 # drop trailing timestamp + preview
    s = re.sub(r"[^\w\s'\-]", " ", s)        # strip emoji / punctuation
    s = re.sub(r"\s+", " ", s).strip()
    name: list[str] = []
    for t in s.split():
        if any(ch.isdigit() for ch in t):    # phone number / time -> stop
            break
        if t[:1].islower():                  # a lowercase word starts the preview
            break
        name.append(t)
        if len(name) >= 4:                   # real names rarely exceed 4 tokens
            break
    return " ".join(name).strip()


def is_person(name: str) -> bool:
    """Filter out app/notification rows so they don't pollute the candidate set.
    Prefix match, since cleaned rows may keep a trailing word ("Uber Eats It's")."""
    n = (name or "").strip().lower()
    if not n:
        return False
    return not any(n == app or n.startswith(app + " ") for app in _NOT_PEOPLE)


def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> list[str]:
    return _norm(s).split()


def score(spoken: str, contact: str) -> float:
    """Similarity in [0, 1] between a spoken name and a candidate contact.

    Blends whole-string similarity with two signals that matter for names: an
    exact first-name match (people usually say a first name) and shared tokens
    (so "Abby" alone still strongly matches "Abby Nengel")."""
    a, b = _norm(spoken), _norm(contact)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = a.split(), b.split()
    first_bonus = 0.25 if ta and tb and ta[0] == tb[0] else 0.0
    shared = len(set(ta) & set(tb))
    token_bonus = 0.15 * shared / max(len(ta), 1)
    return round(min(1.0, ratio + first_bonus + token_bonus), 3)


def rank(spoken: str, contacts: list[str]) -> list[tuple[str, float]]:
    """Return candidate contacts scored against `spoken`, best first, deduped."""
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    for c in contacts:
        c = (c or "").strip()
        key = _norm(c)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((c, score(spoken, c)))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def safe_to_remap(spoken: str, contact: str) -> bool:
    """True when rewriting `spoken` → `contact` cannot invent a different person.

    Allows expansions like "Abby" → "Abby Nengel" (shared given name) and light
    surname/typo fixes when the surname matches. Blocks cross-person remaps
    (e.g. a two-word name snapping to an unrelated one-word contact).
    """
    ta, tb = _tokens(spoken), _tokens(contact)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    # Same tokens, different order — Phone Link lists "Falloon, Chris" for a
    # spoken "Chris Falloon". Same person.
    if set(ta) == set(tb):
        return True
    # Single spoken token: may expand to a longer contact that contains it.
    if len(ta) == 1:
        return ta[0] == tb[0] or ta[0] in tb
    # Multi-token spoken: a shared given name is NOT enough — "Conor Kane"
    # must never snap to "Conor McGregor" (live failure: a memory question
    # about Conor Kane silently became a read of McGregor's thread). When the
    # user SAID a surname, the contact must agree with it: either the contact
    # is the bare given name ("Conor Kane" → "Conor"), or its surname is a
    # near-typo of the spoken one ("Abby Nagle" → "Abby Nengel" 0.55;
    # "Kane" vs "McGregor" 0.17 — 0.5 splits them).
    if ta[0] == tb[0]:
        if len(tb) == 1:
            return True
        return SequenceMatcher(None, ta[-1], tb[-1]).ratio() >= 0.5
    if len(ta) >= 2 and len(tb) >= 2 and ta[-1] == tb[-1]:
        return SequenceMatcher(None, ta[0], tb[0]).ratio() >= 0.8
    # Same multi-token shape with high overall similarity only — still require
    # at least one shared token so "Ann Smith" never becomes "Bob Jones".
    if set(ta) & set(tb):
        return SequenceMatcher(None, _norm(spoken), _norm(contact)).ratio() >= 0.86
    return False
