"""Tier 4 — the feedback -> model loop.

The Recorder already persists every human verdict on an agent's action packets
(approve / edit / cancel, plus the edit revision text). This module reads those
verdicts back so the MODELS learn the user's preferences from the user's OWN data
— the natural successor to A2: where A2 sources prompt EXAMPLES from the user's
vocabulary, this sources them from the user's FEEDBACK.

Consumers (each conditions a model on the feedback, none hardcodes anything):
  1. drafting style     — accepted edit diffs become few-shot preference pairs in
                          the Writing Agent's prompt, so drafts match how the user
                          actually rewrites them.
  2. extraction tuning  — dismissed facts become dynamic NEGATIVE examples in the
                          extractor prompt ("these were not worth extracting").
  3. trust dial         — a sustained approval streak on an intent PROPOSES lower
                          friction for that pattern (propose-only; never auto-applies).

⚠ Same bias/eval guardrail as A2 (the repo documents the name-hallucination risk,
and the extractor is precision-critical): every consumer is OPT-IN and capped.
Enable a consumer once its eval harness shows no regression:
    QUILL_LEARN_DRAFTING=1              (gate: scripts/eval_agent.py drafting)
    QUILL_LEARN_EXTRACTION_NEGATIVES=1  (gate: scripts/eval_extraction.py)
    QUILL_LEARN_TRUST=1                 (propose-only; still gated for surfacing)
Default OFF keeps behavior identical and fully honors the invariant — the examples
live in the user's data, never in this file.
"""
from __future__ import annotations

import os
import time

_TTL = 120.0
_cache: dict = {}


def _flag(name: str) -> bool:
    return os.environ.get(name, "0") not in ("0", "false", "False")


def _store():
    from app.storage import get_store
    return get_store()


def _cached(key: str, builder):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    val = builder()
    _cache[key] = (now, val)
    return val


def reset_cache() -> None:
    _cache.clear()


# ---------------------------------------------------------------------------
# Consumer 1 — drafting preferences
# ---------------------------------------------------------------------------
def drafting_preferences(limit: int = 2) -> list[dict]:
    """Up to `limit` recent (before -> after) draft-revision pairs. Empty when the
    consumer is disabled or there's no feedback yet. `before` is the draft body the
    agent showed; `after` is the human's revision."""
    if not _flag("QUILL_LEARN_DRAFTING"):
        return []

    def _build():
        pairs = []
        for row in _store().learning_edit_pairs(limit=limit * 3):
            before = ((row.get("fields") or {}).get("body")
                      or row.get("summary") or "").strip()
            after = (row.get("user_edit") or "").strip()
            if after:
                pairs.append({"before": before[:600], "after": after[:600]})
            if len(pairs) >= limit:
                break
        return pairs

    return _cached("drafting", _build)


def drafting_preference_block(limit: int = 2) -> str:
    """Render the preference pairs as a prompt block, or "" when there are none."""
    prefs = drafting_preferences(limit=limit)
    if not prefs:
        return ""
    lines = ["HOW THIS USER REWRITES DRAFTS (match this style; examples from their "
             "own past edits, not rules):"]
    for i, p in enumerate(prefs, 1):
        if p["before"]:
            lines.append(f"  {i}. They changed:\n     draft: {p['before']}\n"
                         f"     to:    {p['after']}")
        else:
            lines.append(f"  {i}. Preferred phrasing: {p['after']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Consumer 2 — extraction negatives
# ---------------------------------------------------------------------------
def extraction_negatives(limit: int = 5) -> list[str]:
    """Short texts of facts the user DISMISSED — dynamic negative examples for the
    extractor. Empty when disabled or none exist."""
    if not _flag("QUILL_LEARN_EXTRACTION_NEGATIVES"):
        return []

    def _build():
        out = []
        try:
            for f in _store().list_facts(review="dismissed", limit=limit * 3):
                t = (f.get("text") or "").strip()
                if t:
                    out.append(t[:160])
                if len(out) >= limit:
                    break
        except Exception:
            return []
        return out

    return _cached("negatives", _build)


def extraction_negatives_block(limit: int = 5) -> str:
    """Render dismissed facts as an extractor-prompt negative block, or ""."""
    negs = extraction_negatives(limit=limit)
    if not negs:
        return ""
    lines = ["The user has DISMISSED facts like these before — they were not worth "
             "extracting. Do not emit near-duplicates of them; hold to the same bar:"]
    lines += [f"  - {n}" for n in negs]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Consumer 3 — trust dial (propose-only)
# ---------------------------------------------------------------------------
def trust_proposal(intent: str, *, min_streak: int = 5) -> dict | None:
    """If the user has approved this `intent` `min_streak`+ times in a row with no
    recent edit/cancel, PROPOSE lowering friction for it. Returns a suggestion dict
    or None. This NEVER changes behavior on its own — a caller (UI / readiness) may
    surface it; only a human acts on it."""
    if not _flag("QUILL_LEARN_TRUST") or not (intent or "").strip():
        return None
    intent = intent.strip().lower()

    def _build():
        rows = _store().learning_intent_verdicts(limit=300)
        return rows

    rows = _cached("verdicts", _build)
    streak = 0
    for r in rows:                              # newest-first
        if (r.get("intent") or "").strip().lower() != intent:
            continue
        ft = (r.get("feedback_type") or "").lower()
        if ft in ("approved", "useful"):
            streak += 1
        else:                                    # edited / cancelled / annoying breaks it
            break
    if streak >= min_streak:
        return {"intent": intent, "streak": streak, "proposal": "propose",
                "message": (f"You've approved '{intent}' {streak} times in a row. "
                            "Consider lowering friction for it (proposal only — you "
                            "decide).")}
    return None
