"""Priors-continuity replay (Track A1) — shared by the CLI, worker, and console.

Invariant I-5: at shipped priors the v2 shadow must rank like gravity
(Kendall tau >= gate, default 0.6) before any Field v2 cutover. This module
scores recent field impressions, persists the result, and answers due_for()
so the worker can run it on a nightly cadence without changing live ranking.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.config import settings
from app.services.traces import kendall_tau

DEFAULT_GATE = 0.6
DEFAULT_DAYS = 7.0
# Same spirit as reflect_daily: enqueue on boot when the last run is stale.
DUE_AFTER_S = 20 * 3600.0


def _field_v2() -> bool:
    """Delegate to graph's helper — the one mockable seam for the v2 flag
    (settings is a frozen dataclass, so tests patch the function instead)."""
    try:
        from app.services.graph import _field_v2_enabled
        return bool(_field_v2_enabled())
    except Exception:
        return bool(settings.attention.field_v2)


def score_renders(rows: list[dict], *, min_nodes: int = 4
                  ) -> dict[str, Any]:
    """Group ledger rows into renders and compute per-render Kendall tau.

    Each field impression carries score (gravity) + decomposition.shadow.
    Same context_id (or 120s bucket) = one render.
    """
    groups: dict[object, list[tuple[float, float]]] = {}
    shadow_n = 0
    for r in rows:
        try:
            d = json.loads(r["decomposition"] or "")
        except Exception:
            continue
        shadow = d.get("shadow")
        # Continuity gate always compares shipped gravity (g1) to the A1
        # shadow. When Field v2 is on, `score` is the v2 rank (includes
        # activation) and must NOT be used for the I-5 gate.
        g1 = d.get("g1")
        score = g1 if g1 is not None else r.get("score")
        if shadow is None or score is None:
            continue
        shadow_n += 1
        key = r["context_id"] if r.get("context_id") is not None \
            else ("t", int(float(r["ts"]) // 120))
        groups.setdefault(key, []).append((float(score), float(shadow)))

    taus: list[float] = []
    for pairs in groups.values():
        if len(pairs) < min_nodes:
            continue
        tau = kendall_tau([p[0] for p in pairs], [p[1] for p in pairs])
        if tau is not None:
            taus.append(tau)

    mean = (sum(taus) / len(taus)) if taus else None
    return {
        "renders": len(taus),
        "impressions_with_shadow": shadow_n,
        "mean_tau": round(mean, 4) if mean is not None else None,
        "min_tau": round(min(taus), 4) if taus else None,
        "max_tau": round(max(taus), 4) if taus else None,
        "taus": [round(t, 4) for t in taus],
    }


def run(*, days: float | None = None, gate: float | None = None,
        store=None) -> dict[str, Any]:
    """Score the last `days` of field impressions and persist the result."""
    if store is None:
        from app.storage import get_store
        store = get_store()
    days = float(days if days is not None else settings.attention.replay_days)
    gate = float(gate if gate is not None else settings.attention.replay_gate)
    since = time.time() - days * 86400.0
    with store._lock:
        rows = [dict(r) for r in store._conn.execute(
            "SELECT ts, context_id, score, decomposition "
            "FROM attention_impressions "
            "WHERE surface = 'field' AND ts >= ? AND decomposition IS NOT NULL "
            "ORDER BY ts", (since,)).fetchall()]
    scored = score_renders(rows)
    mean = scored["mean_tau"]
    if scored["renders"] == 0:
        passed = None   # insufficient data — not a fail
        status = "insufficient"
    else:
        passed = bool(mean is not None and mean >= gate)
        status = "pass" if passed else "fail"
    result = {
        "ts": time.time(),
        "days": days,
        "gate": gate,
        "status": status,
        "passed": passed,
        **{k: v for k, v in scored.items() if k != "taus"},
        "field_v2": _field_v2(),
    }
    try:
        store.add_attention_replay_run(result)
    except Exception as exc:
        print(f"[attention_replay] persist skipped ({exc}).")
    return result


def due_for(*, store=None) -> bool:
    """True when no successful-enough run in the last DUE_AFTER_S window.

    'Insufficient' still counts as a run (don't spin nightly with empty ledger);
    only a missing/stale row re-triggers.
    """
    if store is None:
        from app.storage import get_store
        store = get_store()
    last = store.last_attention_replay_run()
    if last is None:
        return True
    return time.time() - float(last.get("ts") or 0) > DUE_AFTER_S


def status(*, store=None) -> dict[str, Any]:
    """Console-facing A1 panel: trace counts + last replay + gate config."""
    if store is None:
        from app.storage import get_store
        store = get_store()
    traces = store.node_dynamics_counts()
    last = store.last_attention_replay_run()
    return {
        "traces": traces,
        "replay": last,
        "gate": settings.attention.replay_gate,
        "days": settings.attention.replay_days,
        "due": due_for(store=store),
        "field_v2": _field_v2(),
        "observe_only": not _field_v2(),
    }
