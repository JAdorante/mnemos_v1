"""Project rollup — give every tool/idea/org a home project, derived from facts.

The graph knows which facts each entity appears in (fact→entity `about` edges
from the rebuild's text match and context_attribution's room stamping), but
nothing ever rolls that up into "LanceDB belongs to Sparrow" — so the entities
page is a flat list where tools and ideas float free. This computes, for each
non-project entity, how its facts co-attribute across project entities and
mints a single entity→project `associated_project` edge when one project
dominates.

Design constraints:
  * single-home or none: an entity that spreads across projects (Claude, AWS,
    "AI") gets NO association rather than five — dominance share below the
    threshold means ambiguous, and ambiguity is represented by absence.
  * context beats text: a fact born in a project's meeting/window (edge
    origin="context") is stronger project evidence than the project's name
    merely appearing in the sentence, so those co-occurrences count double.
  * distinguishable + revertible: edges carry origin="rollup", which nothing
    else writes — graph.rebuild clears only origin="derived", and each rollup
    run clears/re-mints its own class, so it can never wipe or double-count
    anyone else's edges (and never dual-writes into the KG belief store).
  * bind-only: associations are derived over EXISTING entities; a rollup can
    never mint a node.

Kill-switch: QUILL_PROJECT_ROLLUP=0.
"""
from __future__ import annotations

import os
import time

PREDICATE = "associated_project"
ORIGIN = "rollup"
# A co-occurrence via an origin="context" edge (fact born in the project's
# room) counts this many times a plain text-match co-occurrence.
_CONTEXT_BOOST = 2.0


def enabled() -> bool:
    return os.getenv("QUILL_PROJECT_ROLLUP", "1") not in ("0", "false", "False")


def _min_facts() -> int:
    try:
        return max(1, int(os.getenv("QUILL_ROLLUP_MIN_FACTS", "3")))
    except ValueError:
        return 3


def _dominance() -> float:
    try:
        return min(1.0, max(0.0, float(os.getenv("QUILL_ROLLUP_DOMINANCE", "0.6"))))
    except ValueError:
        return 0.6


def _home_kinds() -> frozenset[str]:
    """Entity kinds that can BE a home ("project" by default). Env-overridable
    so a user whose projects live as orgs can widen it without a code change."""
    raw = os.getenv("QUILL_ROLLUP_HOME_KINDS", "project")
    return frozenset(k.strip().lower() for k in raw.split(",") if k.strip())


def compute(store) -> list[dict]:
    """Score fact co-attribution and return the dominant associations.

    [{entity_id, entity_name, entity_kind, project_id, project_name,
      share, facts}] — one row per entity that clears both gates
    (>= _min_facts shared facts with the winner, winner share >= _dominance).
    """
    entities = {int(e["id"]): e for e in store.all_entities()}
    home = _home_kinds()
    projects = {eid for eid, e in entities.items()
                if (e.get("kind") or "").lower() in home}
    if not projects:
        return []

    # fact_id -> [(entity_id, origin)], visible endpoints only.
    by_fact: dict[int, list[tuple[int, str]]] = {}
    for r in store.entity_about_edges():
        eid = int(r["entity_id"])
        if eid in entities:
            by_fact.setdefault(int(r["fact_id"]), []).append(
                (eid, (r.get("origin") or "")))

    # score[entity][project] — context-boosted co-occurrence; count = #facts.
    score: dict[int, dict[int, float]] = {}
    count: dict[int, dict[int, int]] = {}
    for pairs in by_fact.values():
        fact_projects = [(eid, org) for eid, org in pairs if eid in projects]
        if not fact_projects:
            continue
        for eid, e_org in pairs:
            for pid, p_org in fact_projects:
                if eid == pid:
                    continue
                w = _CONTEXT_BOOST if "context" in (e_org, p_org) else 1.0
                score.setdefault(eid, {})[pid] = \
                    score.get(eid, {}).get(pid, 0.0) + w
                count.setdefault(eid, {})[pid] = \
                    count.get(eid, {}).get(pid, 0) + 1

    min_facts, dominance = _min_facts(), _dominance()
    out: list[dict] = []
    for eid, per_project in score.items():
        total = sum(per_project.values())
        pid, best = max(per_project.items(), key=lambda kv: kv[1])
        share = best / total if total else 0.0
        if count[eid][pid] < min_facts or share < dominance:
            continue
        e, p = entities[eid], entities[pid]
        out.append({
            "entity_id": eid, "entity_name": e["name"],
            "entity_kind": e.get("kind") or "idea",
            "project_id": pid, "project_name": p["name"],
            "share": round(share, 3), "facts": count[eid][pid],
        })
    out.sort(key=lambda r: (r["project_name"].lower(), -r["share"]))
    return out


def run(store=None, *, now: float | None = None) -> dict:
    """Recompute all associations: clear this origin's edges, mint the current
    dominant set. Safe to run any time — same inputs give the same edges."""
    if store is None:
        from app.storage import get_store
        store = get_store()
    if not enabled():
        return {"enabled": False, "associated": 0, "cleared": 0}
    assocs = compute(store)
    cleared = store.clear_relations(origin=ORIGIN)
    ts = now if now is not None else time.time()
    for a in assocs:
        store.add_relation(
            "entity", a["entity_id"], PREDICATE, "entity", a["project_id"],
            weight=a["share"], origin=ORIGIN, confidence=a["share"], ts=ts)
    return {"enabled": True, "associated": len(assocs), "cleared": cleared,
            "associations": assocs}


def current(store) -> dict[int, dict]:
    """{entity_id: {"id", "name", "share"}} from the STORED rollup edges —
    read path for the entities page; never recomputes."""
    emap = {int(e["id"]): e["name"] for e in store.all_entities()}
    out: dict[int, dict] = {}
    for r in store.relations_by_predicate(PREDICATE, origin=ORIGIN):
        if r.get("subj_type") != "entity" or r.get("obj_type") != "entity":
            continue
        pid = int(r["obj_id"])
        if pid not in emap:
            continue
        out[int(r["subj_id"])] = {
            "id": pid, "name": emap[pid],
            "share": round(float(r.get("weight") or 0.0), 3)}
    return out
