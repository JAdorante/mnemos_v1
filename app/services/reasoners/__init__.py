"""Track D reasoners — commitment, relationship, scheduling.

No blackboard: each pass picks at most one proposal across all reasoners,
gates it through readiness + cooldown, and surfaces via agent_bridge offers.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from app.services.reasoners import commitment, relationship, scheduling
from app.services.reasoners.base import (
    Proposal, daily_budget_exhausted, daily_budget_remaining, enabled,
    gate, mark_offered, pick_best,
)

_attach_lock = threading.Lock()
_timer: threading.Timer | None = None
_INTERVAL_S = float(os.environ.get("QUILL_REASONER_INTERVAL_S", "900") or "900")
_last_run: dict[str, Any] = {}


def scan(store=None, *, now: float | None = None) -> list[Proposal]:
    if store is None:
        from app.storage import get_store
        store = get_store()
    cands: list[Proposal] = []
    for mod in (commitment, relationship, scheduling):
        try:
            cands.extend(mod.propose(store, now=now))
        except Exception as exc:
            print(f"[reasoners] {mod.__name__} scan skipped ({exc}).")
    return cands


def run_once(store=None, *, surface: bool = True,
             now: float | None = None) -> dict[str, Any]:
    """One calm pass: scan → gate → at most one offer.

    Never raises. `surface=False` returns the would-be proposal without offering
    (console dry-run).
    """
    global _last_run
    if not enabled():
        out = {"ok": True, "enabled": False, "offered": False, "reason": "disabled"}
        _last_run = out
        return out
    try:
        if store is None:
            from app.storage import get_store
            store = get_store()
    except Exception as exc:
        out = {"ok": False, "error": str(exc)}
        _last_run = out
        return out

    cands = scan(store, now=now)
    best = pick_best(cands)
    result: dict[str, Any] = {
        "ok": True,
        "enabled": True,
        "n_candidates": len(cands),
        "offered": False,
        "proposal": None,
        "ts": time.time(),
    }
    if best is None:
        result["reason"] = "no_candidate"
        _last_run = result
        return result

    if daily_budget_exhausted():
        result["reason"] = "daily_budget"
        result["daily_remaining"] = 0
        _last_run = result
        return result

    ok, verdict = gate(best)
    result["proposal"] = {
        "reasoner": best.reasoner,
        "summary": best.summary,
        "goal": best.goal,
        "confidence": best.confidence,
        "fact_id": best.fact_id,
        "person": best.person,
        "why": best.why,
        "deliverable_only": best.deliverable_only,
        "kind": best.kind,
    }
    result["verdict"] = {
        "band": getattr(verdict, "band", None),
        "score": getattr(verdict, "score", None),
        "risk": getattr(verdict, "risk", None),
    }
    result["daily_remaining"] = daily_budget_remaining()
    if not ok:
        result["reason"] = f"readiness:{getattr(verdict, 'band', 'hold')}"
        try:
            from app.services.cog_telemetry import cog_telemetry, REASONER_OFFER
            cog_telemetry.record(REASONER_OFFER, False,
                                 reason=result["reason"],
                                 reasoner=best.reasoner)
        except Exception:
            pass
        _last_run = result
        return result

    if not surface:
        result["reason"] = "dry_run"
        _last_run = result
        return result

    try:
        from app.services.agent_bridge import worker
        shown = worker.propose_reasoner(best)
        mark_offered(best)
        result["offered"] = True
        result["shown"] = bool(shown)
        result["reason"] = "surfaced" if shown else "queued"
        result["daily_remaining"] = daily_budget_remaining()
        try:
            from app.services.cog_telemetry import cog_telemetry, REASONER_OFFER
            cog_telemetry.record(REASONER_OFFER, True,
                                 reason=result["reason"],
                                 reasoner=best.reasoner)
        except Exception:
            pass
    except Exception as exc:
        result["reason"] = f"offer_failed:{exc}"
        print(f"[reasoners] offer skipped ({exc}).")

    _last_run = result
    return result


def status(store=None) -> dict[str, Any]:
    from app.services.reasoners.base import daily_budget_remaining
    fulfill = None
    try:
        from app.services import fulfillment
        if store is None:
            from app.storage import get_store
            store = get_store()
        facts = (store.list_facts(kind="task", limit=5000)
                 + store.list_facts(kind="commitment", limit=5000))
        fulfill = fulfillment.summarize(facts)
        fulfill = fulfillment.with_baseline(fulfill)
    except Exception as exc:
        fulfill = {"error": str(exc)}
    return {
        "enabled": enabled(),
        "interval_s": _INTERVAL_S,
        "daily_remaining": daily_budget_remaining(),
        "last": _last_run or None,
        "fulfillment": fulfill,
        "commitment": commitment.status(store),
        "relationship": relationship.status(store),
        "scheduling": scheduling.status(store),
    }


def attach() -> None:
    """Background tick — same spirit as anticipation / todo watchers."""
    if not enabled():
        return
    with _attach_lock:
        _schedule_next(immediate=True)
    print(f"[reasoners] attached (every {int(_INTERVAL_S)}s when agent on).")


def _schedule_next(*, immediate: bool = False) -> None:
    global _timer
    delay = 5.0 if immediate else max(60.0, _INTERVAL_S)

    def _tick() -> None:
        try:
            run_once(surface=True)
        except Exception as exc:
            print(f"[reasoners] tick skipped ({exc}).")
        with _attach_lock:
            _schedule_next(immediate=False)

    t = threading.Timer(delay, _tick)
    t.daemon = True
    t.start()
    _timer = t
