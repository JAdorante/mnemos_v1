"""Memory traces (Track A1) — base-level strength B and long-run value V.

The Field v2 design un-conflates gravity's single scalar. This module is the
first two of its per-node variables, plus the SHADOW score that proves
continuity before anything changes behavior:

  B  base-level strength — ACT-R's base-level learning: the log power-law
     sum over the node's ACCESS HISTORY (creation, re-assertion, retrieval
     into grounding, user engagement). Recency and frequency stop being two
     hand-weighted terms; the history IS the decay.
        B(t) = ln( Σ_j (t − t_j)^(−d) )        d = 0.5
     Stored compressed: the K=8 most recent access times exactly, older
     accesses folded into (n_older, mean_ts) — the standard hybrid
     approximation, O(1) per node.

  V  long-run value — what mattering has meant before. Seeded from the
     shipped per-kind priors (the old `sem` caste system, demoted from law
     to prior) and moved by engagement outcomes; learning later replaces the
     seed entirely, per node, per user.

  shadow score — the v1 GRAVITY formula with temp→B̂ and sem→V̂ swapped in.
     Logged next to gravity in every field impression (never ranked on), so
     the replay gate can measure agreement (Kendall tau ≥ 0.6 at priors)
     before the cutover. Instrument first, switch second.

Pure math over plain values — no Store, no I/O. Storage keeps the history;
callers pass it in.
"""
from __future__ import annotations

import math

# ACT-R base-level decay exponent. 0.5 is the literature default; per-kind
# fitting (bounded 0.3..0.8) is a learning-phase concern, not an A1 one.
DECAY_D = 0.5

# How many access timestamps are kept exactly; older ones fold into the tail.
RECENT_K = 8

# V priors by constellation kind — the shipped `sem` values, verbatim. This
# is deliberate: v1's per-kind constants become v2's STARTING value, which
# engagement then moves per node. Same numbers, different epistemic status.
KIND_V = {
    "person": 0.55,
    "project": 0.40,
    "tool": 0.32,
    "task": 0.35,
    "commitment": 0.35,
    "place": 0.20,
    "idea": 0.20,
}
V_DEFAULT = 0.35
V_MIN, V_MAX = 0.05, 1.0

# Engagement → V deltas (additive with clamp — the v1 of the design's slow
# EMA; deliberately small so V moves on a weeks scale, not per click).
V_DELTAS = {
    "pin": 0.15,
    "unpin": -0.05,
    "hide": -0.25,
    "click": 0.05,
    "dwell": 0.05,
    "used": 0.05,
    "edited": 0.03,
    "rejected": -0.05,
    "reclassify": 0.02,
}


def base_level(recent: list[float], n_older: int, t_older: float | None,
               now: float, *, d: float = DECAY_D) -> float:
    """Raw B: ln of the power-law activation sum over the access history.
    Empty history → very weak (-inf avoided with a floor age of ~10s)."""
    total = 0.0
    for t in recent or []:
        age = max(10.0, now - float(t))
        total += age ** (-d)
    if n_older and t_older:
        age = max(10.0, now - float(t_older))
        total += float(n_older) * age ** (-d)
    if total <= 0.0:
        return -12.0
    return math.log(total)


def b_hat(b_raw: float) -> float:
    """Map raw B (log space, unbounded) into [0,1] for the score, sigmoid on
    a day scale: one fresh access ≈ 0.5+, a month-old single trace ≈ 0.1."""
    # Scale relative to "one access, one day old": B = -d*ln(86400) ≈ -5.68.
    return 1.0 / (1.0 + math.exp(-(b_raw + 5.68)))


def fold_access(recent: list[float], n_older: int, t_older: float | None,
                ts: float) -> tuple[list[float], int, float | None]:
    """Append one access, keeping the newest RECENT_K exactly and folding the
    overflow into the compressed tail (count + running mean timestamp)."""
    recent = sorted([float(t) for t in (recent or [])] + [float(ts)])
    while len(recent) > RECENT_K:
        oldest = recent.pop(0)
        if n_older and t_older:
            t_older = (t_older * n_older + oldest) / (n_older + 1)
            n_older += 1
        else:
            n_older, t_older = 1, oldest
    return recent, int(n_older or 0), t_older


def v_seed(kind: str, *, pinned: bool = False, profiled: bool = False) -> float:
    """Starting V: the kind prior, lifted by durable user signals."""
    v = KIND_V.get(kind, V_DEFAULT)
    if profiled:
        v = max(v, 0.60)     # named in the onboarding profile: mattered on day one
    if pinned:
        v = max(v, 0.80)     # pinned: the strongest standing statement of value
    return v


def v_bump(v: float, outcome: str) -> float:
    """Move V on an engagement outcome, clamped to [V_MIN, V_MAX]."""
    return max(V_MIN, min(V_MAX, float(v) + V_DELTAS.get(outcome, 0.0)))


def shadow_score(*, kind: str, confidence: float, age_days: float,
                 pinned: bool, prospective: float, relationship: float,
                 future: float, unresolved: float, centrality: float,
                 repeats: float, b: float, v: float,
                 act: float = 0.0, w: dict | None = None) -> float:
    """The v2 shadow at shipped priors: GRAVITY's exact formula with the
    temporal term replaced by B̂ (access-history strength) and the semantic
    caste replaced by V̂ (per-node value). Same weights, same sigmoid, same
    decay and trust gates — divergence comes only from the two swapped
    inputs, which is exactly what the continuity replay measures.

    `w` optionally overrides the weights (the A4 live-ranking path passes the
    Thompson-drawn β). DELIBERATELY not read from ranking_learn here: the
    default MUST stay at shipped priors so the `shadow` logged in every
    impression remains the I-5 replay anchor — a drawn β in the default path
    would make the nightly continuity gate compare gravity against noise."""
    from app.services.graph import GRAVITY, _sigmoid, decay_for_kind, trust_gate

    if w is None:
        w = GRAVITY["w"]
    unc = 1.0 - max(0.05, min(1.0, confidence))
    raw = (
        w.get("pin", 1.35) * (1.0 if pinned else 0.0)
        + w.get("pros", 1.55) * prospective
        + w.get("rel", 1.15) * relationship
        + w.get("fut", 0.95) * future
        + w.get("unres", 0.85) * unresolved
        + w.get("cent", 0.70) * centrality
        + w.get("sem", 0.55) * v
        + w.get("rep", 0.45) * repeats
        + w.get("temp", 0.70) * b
        + w.get("act", 0.90) * act
        - w.get("unc", 0.80) * unc
    )
    gate = decay_for_kind(kind, age_days) * trust_gate(confidence, pinned=pinned)
    return _sigmoid(raw - GRAVITY["sigmoid_offset"]) * gate


def kendall_tau(a: list[float], b: list[float]) -> float | None:
    """Rank agreement between two scorings of the same items, in [-1, 1].
    O(n²) — candidate sets are ≤ 40 by construction. None if degenerate."""
    n = min(len(a), len(b))
    if n < 2:
        return None
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            da, db = a[i] - a[j], b[i] - b[j]
            prod = da * db
            if prod > 0:
                concordant += 1
            elif prod < 0:
                discordant += 1
    pairs = concordant + discordant
    if pairs == 0:
        return None   # all ties on one side
    return (concordant - discordant) / pairs
