"""Spreading activation (Track A2) — context lights its graph neighborhood.

Human recall is associative: activating "Scott" partially activates the
fundraise, the term sheet, the promise you made him. The typed graph already
encodes those associations; this module makes them conduct.

Two parts:

  Edge dynamics (computed at graph rebuild, persisted in `edge_dynamics`):
    every relation gets an attention conductance
        c = g(class) · pmi_factor · confidence
    g(class) is a per-class prior (obligation and user-asserted edges conduct
    strongly; provenance barely conducts — evidence is a receipt, not a
    thought). PMI normalizes the accumulated co_occurs counts so an edge is
    strong because two things co-occur MORE THAN THEIR POPULARITY PREDICTS —
    the rich-get-richer fix.

  Propagation (in-memory, on demand):
    two damped hops from the Now-Context seeds over the row-normalized
    conductance matrix:
        a¹ = α·s + (1−α)·Ĉᵀ s          α = 0.6, fan-out cap 32
        a² = α·s + (1−α)·Ĉᵀ a¹
    sparsified (floor ε, top 256). Activation is conserved, never amplified:
    row normalization means a hub spreads its light thinner, not brighter.
    Results are a pure function of (seeds, graph), cached briefly — seed
    decay in NowContext is the only clock that matters.

Nothing here ranks. The field-v2 scorer reads `activation_map()` as one term.
"""
from __future__ import annotations

import math
import threading
import time

NodeKey = tuple[str, int]

ALPHA = 0.6          # seed retention per hop
HOPS = 2
FANOUT = 32          # strongest edges per node that conduct
EPS = 0.01           # activation floor
TOP_N = 256          # sparsify the result
CACHE_TTL_S = 5.0

# Predicate -> edge class. Anything unlisted is 'aboutness' (mild).
EDGE_CLASS = {
    "responsible_for": "obligation", "committed": "obligation",
    "owed": "obligation", "promise": "obligation", "promised": "obligation",
    "works_at": "assertion", "member_of": "assertion", "part_of": "assertion",
    "about_self": "assertion", "located_in": "assertion",
    "about": "aboutness", "mentioned_in": "aboutness",
    "related_to": "aboutness",
    "co_occurs": "social", "associated_with": "social",
    "evidenced_by": "provenance",
    "linked": "user",
}
# Shipped conductance priors per class (learned per user later, bounded).
G_CLASS = {
    "obligation": 1.4,
    "user": 1.6,
    "assertion": 1.0,
    "aboutness": 0.8,
    "social": 0.7,
    "provenance": 0.3,
}
# Self-loop bookkeeping predicates that must never conduct.
_NEVER = {"pins", "constellation_hidden", "hides"}


def edge_class(predicate: str) -> str:
    return EDGE_CLASS.get((predicate or "").strip().lower(), "aboutness")


def compute_edge_dynamics(relations: list[dict],
                          *, now: float | None = None) -> list[dict]:
    """Per-edge class, PMI (co_occurs only), age decay, and conductance.

    PMI uses the co_occurs weight mass: w_i = a node's total co_occurs
    weight, W = the global total; pmi = ln(w_ij·W / (w_i·w_j)), clipped to
    [0, 3] and scaled to (0, 1]. Non-social classes skip the factor — their
    weights don't accumulate popularity the same way.

    Age decay: c *= exp(-age_days / τ_edge) with τ_edge = 45 d, using the
    relation's created_at (user-asserted and surviving edges keep real age;
    derived edges rebuilt wholesale start fresh — intentional).
    """
    now = now or time.time()
    tau_edge_days = 45.0
    node_w: dict[NodeKey, float] = {}
    total_w = 0.0
    for r in relations:
        if (r.get("predicate") or "").lower() != "co_occurs":
            continue
        w = float(r.get("weight") or 1.0)
        node_w[(r["subj_type"], int(r["subj_id"]))] = \
            node_w.get((r["subj_type"], int(r["subj_id"])), 0.0) + w
        total_w += w

    out: list[dict] = []
    for r in relations:
        pred = (r.get("predicate") or "").strip().lower()
        if pred in _NEVER:
            continue
        if (r.get("subj_type") == r.get("obj_type")
                and int(r.get("subj_id") or 0) == int(r.get("obj_id") or 0)):
            continue
        cls = edge_class(pred)
        pmi = None
        factor = 1.0
        if cls == "social" and pred == "co_occurs" and total_w > 0:
            w_ij = float(r.get("weight") or 1.0)
            w_i = node_w.get((r["subj_type"], int(r["subj_id"])), w_ij)
            w_j = node_w.get((r["obj_type"], int(r["obj_id"])), w_ij)
            raw = math.log(max(1e-9, (w_ij * total_w) / max(1e-9, w_i * w_j)))
            pmi = max(0.0, min(3.0, raw))
            factor = max(0.15, pmi / 3.0)   # floor: co-occurring at all counts
        conf = float(r.get("confidence") or 0.6)
        age_days = 0.0
        created = r.get("created_at")
        if created:
            try:
                age_days = max(0.0, (now - float(created)) / 86400.0)
            except Exception:
                age_days = 0.0
        age_factor = math.exp(-age_days / tau_edge_days)
        conductance = max(
            0.0, min(2.0, G_CLASS[cls] * factor * conf * age_factor))
        out.append({"relation_id": int(r["id"]), "class": cls,
                    "pmi": round(pmi, 4) if pmi is not None else None,
                    "conductance": round(conductance, 4)})
    return out


def propagate(seeds: dict[NodeKey, float],
              adjacency: dict[NodeKey, list[tuple[NodeKey, float]]],
              *, alpha: float = ALPHA, hops: int = HOPS) -> dict[NodeKey, float]:
    """Damped 2-hop spread. Pure function; row-normalizes on the fly."""
    if not seeds:
        return {}
    norm: dict[NodeKey, float] = {}

    def _out(key: NodeKey) -> list[tuple[NodeKey, float]]:
        edges = adjacency.get(key) or []
        if key not in norm:
            norm[key] = sum(c for _, c in edges) or 1.0
        return edges

    a = dict(seeds)
    for _ in range(hops):
        nxt: dict[NodeKey, float] = {}
        for key, val in a.items():
            if val < EPS:
                continue
            for nb, c in _out(key):
                nxt[nb] = nxt.get(nb, 0.0) + val * (1.0 - alpha) * (c / norm[key])
        a = {k: alpha * seeds.get(k, 0.0) + nxt.get(k, 0.0)
             for k in set(seeds) | set(nxt)}
    a = {k: v for k, v in a.items() if v >= EPS}
    if len(a) > TOP_N:
        a = dict(sorted(a.items(), key=lambda kv: -kv[1])[:TOP_N])
    return a


class ActivationField:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._adj: dict[NodeKey, list[tuple[NodeKey, float]]] = {}
        self._adj_key: tuple = ()          # (db_path, relation_count)
        self._cache: dict[NodeKey, float] = {}
        self._cache_key: tuple = ()
        self._cache_ts = 0.0

    def _ensure_adjacency(self, store) -> None:
        key = (str(getattr(store, "db_path", "")), store.relation_count())
        if key == self._adj_key:
            return
        adj: dict[NodeKey, list[tuple[NodeKey, float]]] = {}
        for s_t, s_i, o_t, o_i, c in store.conductive_edges():
            a, b = (s_t, s_i), (o_t, o_i)
            adj.setdefault(a, []).append((b, c))
            adj.setdefault(b, []).append((a, c))   # association is undirected
        for k in adj:
            adj[k].sort(key=lambda e: -e[1])
            del adj[k][FANOUT:]
        self._adj, self._adj_key = adj, key

    def activation_map(self, store) -> dict[NodeKey, float]:
        """Current activation over the graph — cached briefly, recomputed
        when the context generation or the graph changes."""
        from app.services.now_context import now_context
        try:
            with self._lock:
                self._ensure_adjacency(store)
                now = time.time()
                key = (now_context.generation, self._adj_key)
                if (key == self._cache_key
                        and now - self._cache_ts < CACHE_TTL_S):
                    return dict(self._cache)
                result = propagate(now_context.seeds(now), self._adj)
                self._cache, self._cache_key, self._cache_ts = result, key, now
                return dict(result)
        except Exception as exc:   # activation must never break the field
            print(f"[activation] skipped ({exc}).")
            return {}

    def invalidate(self) -> None:
        """Drop adjacency + activation caches (call after graph rebuild)."""
        with self._lock:
            self._adj = {}
            self._adj_key = ()
            self._cache = {}
            self._cache_key = ()
            self._cache_ts = 0.0


activation_field = ActivationField()
