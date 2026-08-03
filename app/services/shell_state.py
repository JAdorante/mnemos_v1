"""Track E shell state — read-only aggregate over field / WM / horizon / offers.

Does NOT invent a proposal channel. Pending offers are whatever agent_bridge
already queued (task_offer, reasoners, anticipation, calendar, …). The shell
only renders and forwards yes/no through the existing resolve path.
"""
from __future__ import annotations

import time
from typing import Any


def build(store, *, agent_worker=None, field_limit: int = 28) -> dict[str, Any]:
    """One payload for the shell page: world · attention · proposals."""
    now = time.time()
    out: dict[str, Any] = {
        "generated_at": now,
        "date_label": time.strftime("%A, %B %d"),
        "stage": "proposals",  # stages are cumulative: world ⊂ attention ⊂ proposals
        "world": {"nodes": [], "wm": [], "selection": None, "mode": None},
        "attention": {
            "horizon": [],
            "at_risk": [],
            "wm": [],
        },
        "proposal": None,
        "queued_offers": 0,
        "awaiting_approval": False,
        "waiting_on": None,
    }

    # --- Stage 1: read-only world (field + WM) -----------------------------
    try:
        from app.services import graph
        from app.services import working_memory as _wm
        _wm.ensure_fresh(store)
        field = graph.constellation(store, limit=max(12, min(field_limit, 40)))
        slots = _wm.snapshot(store) or []
        out["world"] = {
            "nodes": field.get("nodes") or [],
            "edges": field.get("edges") or [],
            "wm": slots,
            "selection": field.get("selection") or _wm.status(store).get("selection"),
            "mode": field.get("mode"),
            "context": field.get("context"),
        }
        out["attention"]["wm"] = [
            {
                "node_key": s.get("node_key"),
                "label": _slot_label(s),
                "node_type": s.get("node_type"),
                "score": s.get("score"),
                "cluster_n": s.get("cluster_n") or 1,
            }
            for s in slots[:12]
        ]
    except Exception as exc:
        out["world"]["error"] = str(exc)

    # --- Stage 2: attention-ordered (horizon + at-risk) --------------------
    try:
        from app.services import horizon as _horizon
        hz = _horizon.strip(store, refresh_first=False)
        out["attention"]["horizon"] = hz.get("items") or []
        out["attention"]["horizon_enabled"] = hz.get("enabled", True)
    except Exception as exc:
        out["attention"]["horizon_error"] = str(exc)

    try:
        from app.services import meta_memory
        risks = meta_memory.scan_at_risk(store, now=now)[:8]
        out["attention"]["at_risk"] = [
            {
                "fact_id": r.get("fact_id"),
                "text": r.get("text"),
                "risk": r.get("risk"),
                "why": r.get("why") or [],
                "subject": r.get("subject") or "",
            }
            for r in risks
        ]
    except Exception as exc:
        out["attention"]["at_risk_error"] = str(exc)

    # --- Stage 3: proposals from existing offer pipeline -------------------
    if agent_worker is not None:
        try:
            agent_worker.expire_stale_offers()
            peek = getattr(agent_worker, "pending_offer", None)
            if callable(peek):
                prop = peek()
            else:
                prop = _fallback_peek(agent_worker)
            if prop:
                out["proposal"] = prop
            out["queued_offers"] = int(getattr(agent_worker, "offer_queue_len",
                                               lambda: 0)())
            _, state = agent_worker.snapshot(10**9)
            out["awaiting_approval"] = bool(state.get("awaiting")
                                            or state.get("todo_pending"))
            out["waiting_on"] = state.get("waiting_on")
            if state.get("packet"):
                out["approval_packet"] = state.get("packet")
        except Exception as exc:
            out["proposal_error"] = str(exc)

    # Review-first: compacted events this month (shell links to restore).
    try:
        month_ago = now - 30 * 86400
        forgotten = store.compacted_events(since=month_ago, limit=8)
        out["forgotten"] = [
            {
                "event_id": f.get("event_id"),
                "summary": (f.get("summary") or f.get("stub") or "")[:120],
                "when": f.get("archived_at") or f.get("time"),
            }
            for f in (forgotten or [])
        ]
    except Exception:
        out["forgotten"] = []

    return out


def _slot_label(s: dict) -> str:
    label = (s.get("label") or "").strip()
    if label:
        return label[:120]
    reason = s.get("reason")
    if isinstance(reason, dict):
        return (reason.get("label") or reason.get("why") or "")[:120]
    if isinstance(reason, str):
        try:
            import json
            d = json.loads(reason)
            if isinstance(d, dict):
                return (d.get("label") or "")[:120]
        except Exception:
            return reason[:120]
    return (s.get("node_key") or "")[:120]


def _fallback_peek(worker) -> dict | None:
    with worker.lock:
        pend = worker.pending_todo
        if not pend:
            return None
        return {
            "kind": pend.get("kind"),
            "title": pend.get("title") or "",
            "message": pend.get("message") or "",
            "items": list(pend.get("items") or []),
            "reasoner": pend.get("reasoner"),
            "fact_id": pend.get("fact_id"),
            "confidence": pend.get("confidence"),
            "why": list(pend.get("why") or []),
            "created_at": pend.get("created_at"),
        }
