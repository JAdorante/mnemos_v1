"""Backfill memory traces (Track A1) — callable from CLI and the worker.

Every person, entity, and active fact gets a trace row: access history from
substrate timestamps, V from kind priors lifted by pins + onboarding profile.
Idempotent: INSERT OR IGNORE — live rows accumulating real accesses are safe.
"""
from __future__ import annotations

from typing import Any


def _profile_names() -> set[str]:
    names: set[str] = set()
    try:
        from app.services.onboarding import load_profile
        profile = load_profile() or {}

        def _walk(v):
            if isinstance(v, str) and 2 < len(v.strip()) < 60:
                names.add(v.strip().lower())
            elif isinstance(v, dict):
                for x in v.values():
                    _walk(x)
            elif isinstance(v, list):
                for x in v:
                    _walk(x)
        for key in ("people", "projects", "tools", "priorities"):
            _walk(profile.get(key))
    except Exception as exc:
        print(f"[backfill] onboarding profile skipped ({exc}).")
    return names


def run(store=None) -> dict[str, Any]:
    """Seed missing node_dynamics rows. Returns counts seeded per kind."""
    from app.services import traces
    from app.services.graph import entity_constellation_kind
    if store is None:
        from app.storage import get_store
        store = get_store()

    pinned = store.user_pinned_nodes()
    profiled = _profile_names()
    seeded = {"person": 0, "entity": 0, "fact": 0}

    for p in store.all_people():
        first, last = p.get("first_seen"), p.get("last_seen")
        access = [t for t in (first, last) if t]
        v = traces.v_seed("person",
                          pinned=("person", p["id"]) in pinned,
                          profiled=(p.get("name") or "").lower() in profiled)
        if store.seed_node_dynamics("person", p["id"], v=v,
                                    access=access or []):
            seeded["person"] += 1

    for e in store.all_entities():
        kind = entity_constellation_kind(e.get("kind"))
        access = [t for t in (e.get("first_seen"), e.get("last_seen")) if t]
        v = traces.v_seed(kind,
                          pinned=("entity", e["id"]) in pinned,
                          profiled=(e.get("name") or "").lower() in profiled)
        if store.seed_node_dynamics("entity", e["id"], v=v,
                                    access=access or []):
            seeded["entity"] += 1

    for f in store.list_facts(limit=100000):
        if (f.get("state") or "active") != "active":
            continue
        fid = f.get("fact_id")
        if not fid:
            continue
        kind = f.get("kind") if f.get("kind") in ("task", "commitment") else "idea"
        access = [t for t in (f.get("extracted_at"), f.get("updated_at")) if t]
        v = traces.v_seed(kind, pinned=("fact", int(fid)) in pinned)
        if store.seed_node_dynamics("fact", int(fid), v=v,
                                    access=access or []):
            seeded["fact"] += 1

    total = sum(seeded.values())
    print(f"[backfill] seeded traces: {seeded} "
          f"(existing rows untouched; profile names matched: {len(profiled)})")
    return {"seeded": seeded, "total": total, "profile_names": len(profiled)}
