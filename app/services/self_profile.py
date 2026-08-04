"""The self node — first-class memory of who the USER is.

Identity (services/identity.py) answers "who am I?" from the onboarding sheet.
This module goes further: it designates one person row as the user's own node
in the knowledge graph and routes first-person memory to it, so the system's
picture of the user is a LIVING profile, not a form:

  * "I'll send Marc the deck" → task/commitment owned by the self node
  * "I prefer morning meetings" → claim linked to the self node (about_self)
  * "I work at Acme" → relation edge FROM the self node (works_at)

Everything flows through the existing pipeline — extraction, the hygiene gate
(so "I'm off coffee" supersedes "I love espresso"), recency ranking — and the
result renders as a compact USER PROFILE grounding section in every answer.

Generic code: WHO the self node is comes from this install's own onboarding /
accepted memory at call time (identity.user_identity), never from code. With
no known user yet, everything degrades to today's behavior (no self routing).
"""
from __future__ import annotations

import re
import time

# First-person signal: the fact is about the speaker themselves. Word-bounded,
# case-insensitive; singular only (a "we" fact is about the team, not the user).
_FIRST_PERSON = re.compile(
    r"\b(i|i'm|i'd|i'll|i've|me|my|mine|myself)\b", re.I)

# Owner/party spellings that mean "the user" in extractor output.
_SELF_TOKENS = frozenset({"me", "i", "myself", "self", "the user", "user"})

# Relation predicate for "this fact is about the user personally". Asserted
# origin so graph.rebuild (which wipes derived edges) never loses it.
SELF_PREDICATE = "about_self"

_cache: dict = {}


def reset() -> None:
    """Drop the cached self node id (tests / profile change)."""
    _cache.clear()


def is_first_person(text: str) -> bool:
    return bool(_FIRST_PERSON.search(text or ""))


def is_self_name(name: str) -> bool:
    """Does this extractor-emitted owner/party name mean the user?

    Only first-person tokens ("me", "i", …). The OS account label is rejected
    as a person elsewhere — it must NOT route onto the self node, or path OCR
    / machine-username owners park contacts on the real user.
    """
    return (name or "").strip().lower() in _SELF_TOKENS


def speaker_is_enrolled_user(speaker: str, store=None) -> bool:
    """True when the turn's speaker label is the enrolled user (plan 2.1).

    `owner='me'` may map to the self node only when this is True; otherwise
    'me' is the labeled speaker of that turn (or unresolved if unknown).
    """
    spk = (speaker or "").strip()
    if not spk or spk.lower() == "unknown speaker":
        return False
    try:
        from app.services.identity import user_identity
        name = (user_identity(store).get("name") or "").strip()
    except Exception:
        return False
    if not name:
        return False
    return spk.casefold() == name.casefold()


def self_person_id(store=None) -> int | None:
    """The user's own person row id — resolved from this install's identity
    (onboarding sheet / accepted memory), created on first use, cached.
    None while the user is still unknown (pre-onboarding)."""
    if "pid" in _cache:
        return _cache["pid"]
    pid = None
    try:
        from app.services.identity import user_identity
        from app.storage import get_store
        st = store if store is not None else get_store()
        name = (user_identity(st).get("name") or "").strip()
        if name:
            pid = st.resolve_person(name, ts=time.time())
    except Exception as exc:
        print(f"[self_profile] self node unavailable ({exc}).")
        return None  # don't cache a transient failure
    _cache["pid"] = pid
    return pid


def link_self(store, fact_id: int, ts: float) -> bool:
    """Attach a fact to the self node (about_self edge). Best-effort."""
    pid = self_person_id(store)
    if pid is None:
        return False
    try:
        store.add_relation("person", pid, SELF_PREDICATE, "fact", fact_id,
                           origin="asserted", ts=ts)
        return True
    except Exception:
        return False


def profile_lines(store, max_items: int = 6) -> list[str]:
    """The living USER PROFILE grounding section: the freshest ACTIVE facts
    attached to the self node (self-claims first, then owned open work).
    Empty list when the user is unknown or nothing is linked yet."""
    pid = self_person_id(store)
    if pid is None:
        return []
    try:
        edges = store.relations_of("person", pid)
    except Exception:
        return []
    self_ids, owned_ids = [], []
    for e in edges.get("out", []):
        if e.get("obj_type") != "fact":
            continue
        (self_ids if e.get("predicate") == SELF_PREDICATE
         else owned_ids).append(e["obj_id"])
    ordered = list(dict.fromkeys(self_ids + owned_ids))
    if not ordered:
        return []
    fmap = store.facts_by_ids(ordered)
    items = []
    for fid in ordered:
        f = fmap.get(fid)
        if not f or (f.get("state") or "active") != "active":
            continue
        if f.get("review") == "dismissed":
            continue
        if f.get("status") in ("done", "cancelled"):
            continue
        text = (f.get("text") or f.get("source_span") or "").strip()
        if text:
            items.append((float(f.get("updated_at") or 0), text))
    if not items:
        return []
    items.sort(key=lambda t: -t[0])
    lines = ["USER PROFILE (what the user has said about themselves):"]
    lines += [f"- {text}" for _, text in items[:max_items]]
    return lines
