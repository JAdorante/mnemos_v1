"""Scheduling reasoner (Track D) — block time / prep windows.

Medium-risk schedule proposals still go through readiness.decide + chat offer.
Never writes calendar without the human's yes (existing calendar offer path
can be used later; v1 delivers a scheduling brief / goal).
"""
from __future__ import annotations

from typing import Any

from app.services.reasoners.base import Proposal


def propose(store, *, now: float | None = None) -> list[Proposal]:
    out: list[Proposal] = []

    # Horizon: suggest a prep block before the next meeting.
    try:
        from app.services import horizon as _horizon
        items = _horizon.predict(store=store, limit=3)
    except Exception:
        items = []
    for i in items:
        when_s = float(i.get("when_s") or 0)
        # Only if event is 20–90 minutes out (prep window)
        if when_s < 20 * 60 or when_s > 90 * 60:
            continue
        label = (i.get("label") or i.get("event_title") or "upcoming event").strip()
        when = i.get("when_label") or "soon"
        goal = (
            f"Schedule a 15-minute prep block before {label} ({when}) "
            f"— propose times only, do not add to calendar without approval"
        )
        out.append(Proposal(
            reasoner="scheduling",
            goal=goal,
            summary=f"Prep block before {label[:60]}",
            confidence=float(i.get("p_need") or 0.65),
            person=label if i.get("node_type") == "person" else None,
            why=["horizon_prep_window", f"in_{int(when_s // 60)}m"]
                + list(i.get("reason") or [])[:1],
            deliverable_only=True,
            meta={"source": "horizon", "when_s": when_s},
        ))

    # Open tasks/commitments with a due date soon but no scheduled block language.
    try:
        facts = (store.list_facts(kind="task", status="open", limit=80)
                 + store.list_facts(kind="commitment", status="open", limit=80))
    except Exception:
        facts = []
    import time as _time
    now_ts = float(now if now is not None else _time.time())
    for f in facts:
        due = f.get("due")
        if not due:
            continue
        due_ts = None
        if isinstance(due, (int, float)):
            due_ts = float(due)
        elif isinstance(due, str) and due.strip():
            try:
                from datetime import datetime
                due_ts = datetime.fromisoformat(
                    due.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
        if due_ts is None:
            continue
        dt = due_ts - now_ts
        if dt < 0 or dt > 3 * 86400:
            continue
        text = (f.get("text") or "").strip()
        if not text:
            continue
        fid = f.get("fact_id") or f.get("id")
        days = max(1, int(dt / 86400) or 1)
        goal = (
            f"Propose a schedule window to finish: {text} "
            f"(due in ~{days}d) — do not book without approval"
        )
        out.append(Proposal(
            reasoner="scheduling",
            goal=goal,
            summary=f"Schedule work: {text[:70]}",
            confidence=0.7,
            fact_id=int(fid) if fid is not None else None,
            why=[f"due_in_{days}d"],
            deliverable_only=True,
            meta={"source": "due_soon", "due": due},
        ))

    return out


def status(store=None) -> dict[str, Any]:
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    props = propose(store)
    return {
        "ok": True,
        "n_candidates": len(props),
        "top": [{
            "summary": p.summary,
            "confidence": p.confidence,
            "why": p.why[:3],
        } for p in props[:3]],
    }
