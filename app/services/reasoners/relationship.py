"""Relationship reasoner (Track D) — quiet contacts, review-first nudges.

Never auto-messages. Proposals are deliverable briefings / optional drafts that
still pass the chat yes/no offer gate (I-9 calm).
"""
from __future__ import annotations

from typing import Any

from app.services.reasoners.base import Proposal


def propose(store, *, now: float | None = None) -> list[Proposal]:
    out: list[Proposal] = []
    try:
        from app.services import meta_memory
        weak = meta_memory.scan_weakening_relationships(store, now=now)
    except Exception:
        weak = []

    for w in weak[:5]:
        name = (w.get("name") or "").strip()
        if not name:
            continue
        quiet = w.get("quiet_days")
        goal = (
            f"Prepare a brief relationship check-in note for {name} "
            f"(quiet {quiet}d) — draft only, do not send"
        )
        out.append(Proposal(
            reasoner="relationship",
            goal=goal,
            summary=f"Quiet contact: {name} ({quiet}d)",
            confidence=min(0.9, 0.55 + min(60.0, float(quiet or 30)) / 120.0),
            person=name,
            why=[f"weakening_relationship {quiet}d"],
            deliverable_only=True,
            meta={"source": "weakening", "quiet_days": quiet,
                  "node_id": w.get("node_id")},
        ))

    # Horizon person with open commitments — warm the relationship before meet.
    try:
        from app.services import horizon as _horizon
        items = _horizon.predict(store=store, limit=3)
    except Exception:
        items = []
    for i in items:
        if i.get("node_type") != "person":
            continue
        name = (i.get("label") or "").strip()
        if not name:
            continue
        if any(p.person and p.person.lower() == name.lower() for p in out):
            continue
        when = i.get("when_label") or "soon"
        title = i.get("event_title") or "upcoming meeting"
        goal = (
            f"Brief me on my relationship with {name} before {title} ({when})"
        )
        out.append(Proposal(
            reasoner="relationship",
            goal=goal,
            summary=f"Pre-meet relationship: {name}",
            confidence=float(i.get("p_need") or 0.7),
            person=name,
            why=["horizon_person"] + list(i.get("reason") or [])[:2],
            deliverable_only=True,
            meta={"source": "horizon", "when_label": when},
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
            "person": p.person,
            "confidence": p.confidence,
            "why": p.why[:3],
        } for p in props[:3]],
    }
