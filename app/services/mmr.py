"""MMR-style mutual inhibition for focus / Working Memory selection (Track A3).

Spec (Field §8.2):
    pick argmax_i [ score_i − γ · max_{j ∈ selected} sim(i, j) ]      γ = 0.35

Near-duplicate open work is *hard-excluded* once a head is picked (sim ≥ τ),
and counted on that head as `cluster_n`. Soft MMR alone at γ=0.35 cannot beat
a 20-task flood against quieter people/entities; hard collapse is how the
cluster chip ("+7 related") emerges and how the old quota invariants hold
without the quota mechanism.
"""
from __future__ import annotations

from typing import Callable

GAMMA = 0.35
# Same-kind open work above this similarity joins the picked head's cluster
# and is removed from further competition.
CLUSTER_TAU = 0.70

_OPEN = frozenset({"task", "commitment"})
_ENTITY = frozenset({"project", "tool", "place", "idea", "product", "org"})


def _tokens(label: str) -> set[str]:
    return {t for t in "".join(
        ch.lower() if ch.isalnum() else " " for ch in (label or "")
    ).split() if len(t) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / max(1, len(ta | tb))


def structural_sim(a: dict, b: dict) -> float:
    """Heuristic sim blending kind affinity and label overlap.

    No embedding load on the hot path — MiniLM can refine later; structural
    signals are enough for the flood / diversity contracts.
    """
    if a.get("id") == b.get("id"):
        return 1.0
    ka, kb = a.get("kind") or "", b.get("kind") or ""
    if ka in _OPEN and kb in _OPEN:
        return 0.78 + 0.20 * jaccard(a.get("label") or "", b.get("label") or "")
    if ka == "person" and kb == "person":
        return 0.18 + 0.15 * jaccard(a.get("label") or "", b.get("label") or "")
    if ka in _ENTITY and kb in _ENTITY:
        same = 0.15 if ka == kb else 0.0
        return 0.28 + same + 0.20 * jaccard(
            a.get("label") or "", b.get("label") or "")
    return 0.04 + 0.10 * jaccard(a.get("label") or "", b.get("label") or "")


def mmr_select(
    candidates: list[dict],
    k: int,
    *,
    gamma: float = GAMMA,
    sim_fn: Callable[[dict, dict], float] = structural_sim,
    score_key: str = "gravity",
    pinned_first: bool = True,
    hard_cluster: bool = True,
    cluster_tau: float = CLUSTER_TAU,
) -> list[dict]:
    """Deterministic MMR selection. Returns up to `k` candidates (copies) with
    `cluster_n` / `cluster_members` filled when hard_cluster is on.

    Pins (n['pinned']) always enter first and never inhibit each other out.
    """
    if k <= 0 or not candidates:
        return []

    pool = list(candidates)
    selected: list[dict] = []
    claimed: set[str] = set()

    def _take(n: dict, members: list[dict] | None = None) -> None:
        out = dict(n)
        mem = [m for m in (members or []) if m.get("id") != n.get("id")]
        out["cluster_n"] = 1 + len(mem)
        out["cluster_members"] = [m.get("id") for m in mem]
        selected.append(out)
        claimed.add(n["id"])
        for m in mem:
            claimed.add(m["id"])

    if pinned_first:
        pins = [n for n in pool if n.get("pinned")]
        pins.sort(key=lambda n: -float(n.get(score_key) or 0))
        for n in pins:
            if n["id"] in claimed:
                continue
            if len(selected) >= k:
                break
            members = []
            if hard_cluster:
                members = [
                    c for c in pool
                    if c["id"] not in claimed and c["id"] != n["id"]
                    and sim_fn(n, c) >= cluster_tau
                ]
            _take(n, members)

    while len(selected) < k:
        best = None
        best_val = None
        for c in pool:
            if c["id"] in claimed:
                continue
            score = float(c.get(score_key) or 0)
            if not selected:
                val = score
            else:
                max_sim = max(sim_fn(c, s) for s in selected)
                val = score - gamma * max_sim
            if best is None or val > best_val:
                best, best_val = c, val
        if best is None:
            break
        members = []
        if hard_cluster:
            members = [
                c for c in pool
                if c["id"] not in claimed and c["id"] != best["id"]
                and sim_fn(best, c) >= cluster_tau
            ]
        _take(best, members)

    return selected
