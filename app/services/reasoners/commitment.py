"""Commitment / follow-through reasoner (Track D wedge).

Reads at-risk + dropped-thread signals and WM fact slots; proposes a single
follow-through action. Execution goes through readiness + the chat offer gate.
"""
from __future__ import annotations

from typing import Any

from app.services.reasoners.base import Proposal


def propose(store, *, now: float | None = None) -> list[Proposal]:
    out: list[Proposal] = []
    try:
        from app.services import meta_memory
        risks = meta_memory.scan_at_risk(store, now=now)
        dropped = meta_memory.scan_dropped_threads(store, now=now)
    except Exception:
        risks, dropped = [], []

    # Prefer urgent overdue commitments over quietly dropped threads.
    for r in risks[:6]:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        fid = r.get("fact_id")
        why = list(r.get("why") or [])
        why.append("commitment_follow_through")
        subject = (r.get("subject") or "").strip() or None
        goal = (
            f"Follow through on this open commitment"
            + (f" with {subject}" if subject else "")
            + f": {text}"
        )
        out.append(Proposal(
            reasoner="commitment",
            goal=goal,
            summary=f"Follow through: {text[:80]}",
            confidence=min(0.95, 0.7 + float(r.get("risk") or 0.75) * 0.2),
            fact_id=int(fid) if fid is not None else None,
            person=subject,
            why=why,
            deliverable_only=True,  # briefing first; user can ask to act
            meta={"source": "at_risk", "risk": r.get("risk")},
        ))

    for d in dropped[:4]:
        text = (d.get("text") or "").strip()
        if not text:
            continue
        fid = d.get("fact_id")
        # Skip if already covered by at-risk above
        if any(p.fact_id == fid for p in out if fid is not None):
            continue
        quiet = d.get("quiet_days")
        why = [f"dropped_thread quiet {quiet}d"]
        goal = f"Reopen or close this dropped thread: {text}"
        out.append(Proposal(
            reasoner="commitment",
            goal=goal,
            summary=f"Dropped thread: {text[:80]}",
            confidence=0.72,
            fact_id=int(fid) if fid is not None else None,
            person=(d.get("subject") or None),
            why=why,
            deliverable_only=True,
            meta={"source": "dropped_thread", "quiet_days": quiet},
        ))

    # WM open work as a weak signal (same attention as the field).
    try:
        from app.services import working_memory as _wm
        _wm.ensure_fresh(store)
        for s in _wm.snapshot(store) or []:
            if s.get("node_type") != "fact":
                continue
            label = (s.get("label") or s.get("reason") or "").strip()
            if not label:
                # reason may be json
                reason = s.get("reason")
                if isinstance(reason, dict):
                    label = (reason.get("label") or "")[:120]
            if not label:
                continue
            nid = s.get("node_id")
            if any(p.fact_id == nid for p in out if nid is not None):
                continue
            # Only if already urgent-ish in WM
            if (s.get("att_state") or "").lower() not in ("urgent", "focused"):
                continue
            out.append(Proposal(
                reasoner="commitment",
                goal=f"Follow through on focused work: {label}",
                summary=f"WM follow-through: {label[:80]}",
                confidence=0.68,
                fact_id=int(nid) if nid is not None else None,
                why=["wm_urgent_or_focused"],
                deliverable_only=True,
                meta={"source": "wm"},
            ))
    except Exception:
        pass

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
            "fact_id": p.fact_id,
            "why": p.why[:3],
        } for p in props[:3]],
    }
