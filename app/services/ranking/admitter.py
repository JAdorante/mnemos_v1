"""Admitter — post-selection diversity quotas as swaps, not a fork.

If the Selector's focus violates people/entity minima, swap the
lowest-marginal-score non-pinned nodes for the best missing people/entities
from the ranked pool. Records admitted_by=quota on swapped-in nodes.
"""
from __future__ import annotations

from app.services.ranking.config import (
    ENTITY_FOCUS_KINDS,
    MIN_ENTITIES_IN_FOCUS,
    MIN_PEOPLE_IN_FOCUS,
)
from app.services.ranking.types import PipelineContext, ScoreBreakdown


def _is_person(n: dict) -> bool:
    return n.get("kind") == "person"


def _is_entity(n: dict) -> bool:
    return n.get("kind") in ENTITY_FOCUS_KINDS


def _marginal_score(n: dict) -> float:
    """Lower = more willing to swap out. Pins are never swapped."""
    if n.get("pinned"):
        return float("inf")
    return float(n.get("gravity") or 0.0)


def admit(
    focus: list[dict],
    ranked: list[dict],
    breakdowns: dict[str, ScoreBreakdown],
    ctx: PipelineContext,
) -> list[dict]:
    """Enforce people/entity quotas via lowest-score swaps."""
    focus_k = max(len(focus), int(ctx.focus_k))
    focus = [dict(n) for n in focus]
    for n in focus:
        n["layer"] = "focus"
        bd = breakdowns.get(n["id"])
        if n.get("pinned"):
            n["admitted_by"] = "pin"
            if bd:
                bd.admitted_by = "pin"
        else:
            n.setdefault("admitted_by", "score")
            if bd and bd.admitted_by == "score":
                pass

    have_ids = {n["id"] for n in focus}
    people_in = sum(1 for n in focus if _is_person(n))
    ents_in = sum(1 for n in focus if _is_entity(n))
    people_avail = sum(1 for n in ranked if _is_person(n))
    ents_avail = sum(1 for n in ranked if _is_entity(n))
    need_people = max(0, min(MIN_PEOPLE_IN_FOCUS, people_avail) - people_in)
    need_ents = max(0, min(MIN_ENTITIES_IN_FOCUS, ents_avail) - ents_in)

    if need_people == 0 and need_ents == 0:
        return focus

    # Candidates to swap in, in rank order, not already in focus.
    pool_people = [
        n for n in ranked
        if _is_person(n) and n["id"] not in have_ids
    ]
    pool_ents = [
        n for n in ranked
        if _is_entity(n) and n["id"] not in have_ids
    ]

    def _swap_in(incoming: dict, reason: str) -> bool:
        """Replace lowest-marginal eligible focus node with incoming."""
        nonlocal focus, have_ids
        # Prefer swapping a node that is NOT the type we're trying to add,
        # and never a pin.
        victims = [
            (i, n) for i, n in enumerate(focus)
            if not n.get("pinned")
            and n.get("admitted_by") != "pin"
        ]
        if reason == "person":
            # Prefer swapping non-people (usually tasks).
            prefer = [(i, n) for i, n in victims if not _is_person(n)]
            victims = prefer or victims
            # Don't drop below people minimum after swap (we're adding a person).
        elif reason == "entity":
            prefer = [(i, n) for i, n in victims if not _is_entity(n)]
            # Also avoid dropping the last people if we're at the floor.
            if people_in <= MIN_PEOPLE_IN_FOCUS:
                prefer = [(i, n) for i, n in prefer if not _is_person(n)]
            victims = prefer or [
                (i, n) for i, n in victims if not _is_person(n)
            ] or victims

        if not victims:
            # Focus full of pins — append if under capacity, else give up.
            if len(focus) < focus_k:
                out = dict(incoming)
                out["layer"] = "focus"
                out["admitted_by"] = "quota"
                focus.append(out)
                have_ids.add(out["id"])
                bd = breakdowns.get(out["id"])
                if bd:
                    bd.admitted_by = "quota"
                return True
            return False

        victims.sort(key=lambda pair: _marginal_score(pair[1]))
        vi, _victim = victims[0]
        out = dict(incoming)
        out["layer"] = "focus"
        out["admitted_by"] = "quota"
        # Drop absorbed cluster bookkeeping from victim; incoming is fresh.
        out.pop("cluster_members", None)
        focus[vi] = out
        have_ids = {n["id"] for n in focus}
        bd = breakdowns.get(out["id"])
        if bd:
            bd.admitted_by = "quota"
        return True

    for cand in pool_people:
        if need_people <= 0:
            break
        if _swap_in(cand, "person"):
            need_people -= 1
            people_in += 1

    # Refresh entity pool after people swaps.
    have_ids = {n["id"] for n in focus}
    pool_ents = [
        n for n in ranked
        if _is_entity(n) and n["id"] not in have_ids
    ]
    ents_in = sum(1 for n in focus if _is_entity(n))
    need_ents = max(0, min(MIN_ENTITIES_IN_FOCUS, ents_avail) - ents_in)

    for cand in pool_ents:
        if need_ents <= 0:
            break
        if _swap_in(cand, "entity"):
            need_ents -= 1
            ents_in += 1

    return focus
