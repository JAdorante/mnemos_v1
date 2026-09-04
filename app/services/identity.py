"""Identity grounding — who the assistant is, and who the user is.

Two questions every assistant should answer without gambling on semantic
retrieval: "what are you?" and "who am I?". Before this, both fell through to
fuzzy timeline search, so "who am I?" could miss the very fact that defines the
user. This resolves both deterministically:

  * the ASSISTANT's identity is a product constant (Sparrow) — general, the
    same on every install;
  * the USER's identity is read from THIS install's own onboarding profile /
    accepted memory at call time, never hardcoded — so on another person's
    machine it describes THAT person (the general-code invariant).

Surfaced as a compact section (`identity_lines`) injected FIRST into every chat's
grounding block, so local, Claude, and agent answers all know it.
Best-effort: any lookup failure degrades to "user not known yet", never raises.
"""
from __future__ import annotations

SELF_ROLE = "personal AI memory assistant"

# The approved claims the onboarding survey writes for the identity block
# (services/onboarding.py). Read back here so identity survives even when the
# profile JSON is absent (e.g. it was ingested via an inline POST /ingest).
_CLAIM_PREFIX = {
    "name": "the user's name is",
    "role": "the user works as",
    "description": "how the user describes their work:",
    "primary_email": "the user's primary email is",
    "secondary_email": "the user's secondary email is",
    "phone": "the user's phone number is",
}

_IDENTITY_FIELDS = (
    "name", "role", "description",
    "primary_email", "secondary_email", "phone",
)


def brand() -> str:
    """Product name — single source of truth is mnemos_theme.BRAND. Lazy import
    (and a literal fallback) so a services-layer caller never hard-depends on the
    UI layer or breaks if it's unavailable."""
    try:
        from app.api.mnemos_theme import BRAND
        return BRAND
    except Exception:
        return "Sparrow"


def assistant_identity() -> dict:
    """Who the assistant is — product-level, identical for every user."""
    name = brand()
    return {
        "name": name,
        "role": SELF_ROLE,
        "summary": (
            f"You are {name}, the user's {SELF_ROLE}: you observe what they see, "
            "hear, and do (with their consent), remember it, and help them recall "
            "and act on it. You are grounded in this one user's memory — not a "
            "generic chatbot."
        ),
    }


def _user_from_profile() -> dict:
    try:
        from app.services.onboarding import load_profile
        prof = load_profile() or {}
    except Exception:
        prof = {}
    ident = prof.get("identity") if isinstance(prof, dict) else None
    if not isinstance(ident, dict):
        return {}
    out = {k: (str(ident.get(k) or "")).strip()
           for k in _IDENTITY_FIELDS}
    return out if out.get("name") else {}


def _user_from_store(store) -> dict:
    """Fallback: reconstruct identity from the approved onboarding claims, so it
    works even when the profile sheet isn't on disk."""
    if store is None:
        return {}
    try:
        claims = store.list_facts(kind="claim", limit=200)
    except Exception:
        return {}
    out: dict = {}
    for c in claims:
        text = (c.get("text") or c.get("source_span") or "").strip()
        low = text.lower()
        for field, pfx in _CLAIM_PREFIX.items():
            if not out.get(field) and low.startswith(pfx):
                out[field] = text[len(pfx):].strip().strip(".").strip()
    return out if out.get("name") else {}


def user_identity(store=None) -> dict:
    """The user's identity: profile sheet first (what they stated), then approved
    memory. Returns {} with no reliable name — the honest 'not known yet' signal.
    `source` records which layer answered."""
    prof = _user_from_profile()
    if prof.get("name"):
        prof["source"] = "profile"
        return prof
    st = _user_from_store(store)
    if st.get("name"):
        st["source"] = "memory"
        return st
    return {}


def identity_lines(store=None) -> list[str]:
    """Grounding section: the assistant's identity (always) + the user's (when
    known). Header first so it renders as one labeled block."""
    a = assistant_identity()
    lines = [
        "ABOUT YOU (the assistant) AND THE USER:",
        f"- You are {a['name']}, the user's {a['role']} — grounded in this user's "
        "own memory, not a generic chatbot. If asked who or what you are, say so.",
    ]
    u = user_identity(store)
    if u.get("name"):
        role = f", {u['role']}" if u.get("role") else ""
        desc = f" {u['description']}" if u.get("description") else ""
        lines.append(f"- The user you are assisting is {u['name']}{role}.{desc} "
                     "Address them by name when natural.")
        if u.get("primary_email"):
            lines.append(f"- Primary email: {u['primary_email']}")
        if u.get("secondary_email"):
            lines.append(f"- Secondary email: {u['secondary_email']}")
        if u.get("phone"):
            lines.append(f"- Phone: {u['phone']}")
    else:
        lines.append("- You do not yet know who the user is — they haven't "
                     "introduced themselves. If asked 'who am I', say you don't "
                     "know yet and invite them to complete their profile.")
    return lines
