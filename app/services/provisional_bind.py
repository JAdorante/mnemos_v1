"""People v3 WS-D part 2 — provisional-bind band + merge-as-training.

Design: medium-confidence matches must not mint a twin, and must not sit
forever as leave_open either. Behind QUILL_PROVISIONAL_BIND (default OFF):

- When the resolver is about to hard-mint (create_new), park a recurrence
  pending_mint, or leave_open — AND the best EXISTING candidate scores in
  [score_lo, score_hi] (default 0.55–0.80) — bind provisionally to that
  person instead. Slot: after the WS-C recurrence gate, before the hard
  mint (see people_pipeline.resolve_person_mention).
- The mention lands as resolution_status='provisional' (not a conclusive
  alias_rule). touch_person still records the spelling so exact repeats
  resolve; promotion is NOT bumped — provisional evidence has not earned
  an active node.
- Human soft_merge is the training signal (WS-D pt1 already writes
  positive alias_rules for absorbed spellings). Part 2 adds: provisional
  mentions that pointed at the absorbed person re-point to the survivor,
  flip to resolved, and each distinct spelling becomes a positive
  alias_rule — the provisional bind is confirmed by the merge.

Exact / prefix / auto_resolve (>= 0.92) paths are untouched. Scores
outside the band behave exactly as today. Flag OFF = byte-identical:
enabled() is checked first and nothing else runs.

Deterministic throughout — no models, no vector index.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings

PROVISIONAL_STATUS = "provisional"

_DEFAULT_LO = 0.55
_DEFAULT_HI = 0.80


def _cfg():
    return getattr(settings, "provisional_bind", None)


def enabled() -> bool:
    """QUILL_PROVISIONAL_BIND gate. getattr-chained so older suites that
    patch settings with a bare SimpleNamespace read as OFF, never crash."""
    return bool(getattr(_cfg(), "enabled", False))


def score_lo() -> float:
    try:
        return float(getattr(_cfg(), "score_lo", _DEFAULT_LO))
    except Exception:
        return _DEFAULT_LO


def score_hi() -> float:
    try:
        return float(getattr(_cfg(), "score_hi", _DEFAULT_HI))
    except Exception:
        return _DEFAULT_HI


@dataclass
class ProvisionalHit:
    """Best existing candidate inside the provisional-bind band, if any."""
    person_row: dict
    score: float


def best_in_band(
    scored: list[tuple[dict | None, float, dict]],
) -> ProvisionalHit | None:
    """Highest-scoring EXISTING person whose score sits in [lo, hi].

    `scored` is the people_pipeline candidate list (person_row | None,
    score, feats) — the synthetic 'new person' row (person_row is None) is
    ignored. Empty / out-of-band → None.
    """
    lo, hi = score_lo(), score_hi()
    if lo > hi:
        lo, hi = hi, lo
    best: ProvisionalHit | None = None
    for row, score, _feats in scored:
        if row is None:
            continue
        sc = float(score)
        if sc < lo or sc > hi:
            continue
        if best is None or sc > best.score:
            best = ProvisionalHit(person_row=row, score=sc)
    return best


# Decisions the band is allowed to override. auto_resolve / reject / self
# stay authoritative; we only intercept the "would mint or stall" paths.
_OVERRIDABLE = frozenset({"create_new", "leave_open", "pending_mint"})


def maybe_bind(
    scored: list[tuple[dict | None, float, dict]],
    decision: str,
) -> tuple[str, dict, float] | None:
    """If the band applies, return (provisional_bind, row, score); else None.

    Callers keep their current decision when this returns None. Never raises.
    """
    if not enabled() or decision not in _OVERRIDABLE:
        return None
    hit = best_in_band(scored)
    if hit is None:
        return None
    return "provisional_bind", hit.person_row, hit.score


def on_person_merged(store, survivor_id: int, absorbed_id: int,
                     ts: float) -> dict:
    """Merge-as-training: promote provisional mentions off the absorbed
    person onto the survivor and write conclusive positive alias_rules for
    each distinct spelling. Idempotent / best-effort; returns counts."""
    out = {"promoted": 0, "aliases": 0}
    try:
        rows = store.promote_provisional_mentions(
            from_person_id=int(absorbed_id),
            to_person_id=int(survivor_id),
            ts=float(ts),
        )
    except Exception as exc:
        print(f"[provisional_bind] promote on merge skipped ({exc}).")
        return out
    out["promoted"] = len(rows)
    seen: set[str] = set()
    for r in rows:
        spelling = (r.get("raw_text") or r.get("normalized_text") or "").strip()
        key = spelling.lower()
        if not spelling or key in seen:
            continue
        seen.add(key)
        try:
            if store.add_alias_rule(
                    int(survivor_id), spelling, "positive",
                    created_by="provisional_bind:merge", ts=ts):
                out["aliases"] += 1
        except Exception:
            pass
        try:
            store.touch_person(int(survivor_id), ts, alias=spelling)
        except Exception:
            pass
    return out
