"""CAL Stage 4 — graph propagation, kept on a very short leash.

Propagation is where systems like this quietly destroy themselves. Everything
connects to everything eventually, so the question is never "can we reach that
node" but "how far may a reason travel before it stops being a reason". Four
constraints, and the fourth is the one usually missed.

1. ONE HOP. `John → Acme → Ravenry` is two hops. This returns evidence for
   Acme; getting from Acme to Ravenry requires a separate, explicitly damped
   pass by the caller. No transitive closure, ever.

2. DAMPING. λ = 0.4, multiplied by the source edge's own confidence, so a
   weak edge carries proportionally less.

3. IT CANNOT MINT. The output is a single supporting feature. It may reorder
   candidates that direct evidence already proposed; it may never create a
   candidate, and it may never write a binding.

4. LAYER SEPARATION. Only `asserted` (a human said so) and `derived` (a
   deterministic key) edges propagate. An `inferred` edge may not serve as
   evidence for another inference. This is the constraint that keeps the
   system's own guesses from becoming its own evidence, which is how a graph
   drifts into a confidently self-consistent fiction that nothing inside it
   can detect.

Plus a fan-out cap: a hub node — a person in four hundred conversations —
would otherwise dominate every score it touches.
"""
from __future__ import annotations

DAMPING = 0.4
MIN_EDGE_CONFIDENCE = 0.55
FAN_OUT = 32
PROPAGATING_LAYERS = ("asserted", "derived")


def neighbors(store, node_type: str, node_id, *, min_confidence: float = MIN_EDGE_CONFIDENCE,
              fan_out: int = FAN_OUT) -> list[dict]:
    """One hop out, filtered by layer and confidence, capped by fan-out.

    Ordered by confidence so the cap keeps the strongest edges rather than
    whichever the planner happened to emit first — a silent truncation that
    kept arbitrary edges would make the score depend on row order.
    """
    sql = (
        "SELECT obj_type AS n_type, obj_id AS n_id, predicate, confidence, layer "
        "FROM kg_predicates "
        "WHERE subj_type=? AND subj_id=? AND status='active' "
        f"  AND layer IN ({','.join('?' * len(PROPAGATING_LAYERS))}) "
        "  AND confidence >= ? "
        "ORDER BY confidence DESC LIMIT ?")
    args = [node_type, int(node_id), *PROPAGATING_LAYERS,
            float(min_confidence), int(fan_out)]
    try:
        with store._lock:
            rows = store._conn.execute(sql, args).fetchall()
    except Exception as exc:
        print(f"[cal.propagate] neighbours unavailable ({exc}).")
        return []
    return [dict(r) for r in rows]


def propagate(store, seeds: dict, *, damping: float = DAMPING,
              min_confidence: float = MIN_EDGE_CONFIDENCE,
              fan_out: int = FAN_OUT) -> dict:
    """One damped hop from every seed. `seeds` maps (node_type, node_id) → weight.

    Returns the same shape, holding only NEIGHBOURS and their propagated
    weight. Seeds are deliberately absent from the result: a node does not
    propagate to itself, and returning it would let a caller double-count
    direct evidence as graph evidence.
    """
    out: dict = {}
    for (ntype, nid), w in (seeds or {}).items():
        if w <= 0:
            continue
        for edge in neighbors(store, ntype, nid, min_confidence=min_confidence,
                              fan_out=fan_out):
            key = (edge["n_type"], edge["n_id"])
            if key in (seeds or {}):
                continue
            contrib = float(w) * damping * float(edge["confidence"] or 0.0)
            if contrib <= 0:
                continue
            # A node reachable from two seeds takes the STRONGEST path, not
            # their sum: two routes to the same neighbour are usually one
            # reason seen twice, and summing rewards density over evidence.
            out[key] = max(out.get(key, 0.0), contrib)
    return out


def as_features(propagated: dict) -> dict:
    """Propagated weights as resolver features — `f_graph` only, by design.

    Returns {(node_type, node_id): {"f_graph": w}}. `f_graph` is classified
    SUPPORTING in the resolver, so the 0.30 clamp applies and this cannot
    carry a candidate on its own however many neighbours agree.
    """
    return {k: {"f_graph": min(1.0, v)} for k, v in (propagated or {}).items()}


def reweight(scored_candidates, propagated: dict):
    """Attach graph evidence to candidates DIRECT evidence already proposed.

    Deliberately takes candidates rather than producing them, and silently
    ignores any propagated node that is not already a candidate. That refusal
    is constraint 3, expressed as a signature rather than a comment.
    """
    feats = as_features(propagated)
    out = []
    for c in scored_candidates:
        extra = feats.get(c.key)
        if not extra:
            out.append(c)
            continue
        merged = dict(c.features)
        merged.update(extra)
        out.append(type(c)(c.node_type, c.node_id, c.name, merged,
                           c.strong_keys))
    return out


__all__ = ["DAMPING", "MIN_EDGE_CONFIDENCE", "FAN_OUT", "PROPAGATING_LAYERS",
           "neighbors", "propagate", "as_features", "reweight"]
