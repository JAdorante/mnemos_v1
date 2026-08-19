"""Merge duplicate entities: one survivor keeps the names, the rest go dark.

The extractor can mint the same thing under spelling variants faster than the
write-time corrector can bind them ("VenturePulse" / "Venture Pulse" /
"DTC Venture Pulse"). This transplants each absorbed entity's canonical name
and aliases onto the survivor, soft-hides the absorbed rows, and (by default)
re-runs graph.rebuild() + the project rollup — the rebuild re-derives every
text-match edge onto the survivor because it now owns the absorbed spellings,
and hidden rows drop out of derivation entirely. No rows are deleted.

    python scripts/merge_entities.py 89 311 38          # dry-run report
    python scripts/merge_entities.py 89 311 38 --apply  # merge + rebuild
    python scripts/merge_entities.py 89 311 38 --apply --no-rebuild
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("survivor", type=int, help="entity id that keeps the node")
    ap.add_argument("absorbed", type=int, nargs="+",
                    help="entity ids folded into the survivor")
    ap.add_argument("--apply", action="store_true",
                    help="write the merge (default: dry-run)")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="skip graph.rebuild() + project rollup afterwards")
    args = ap.parse_args()

    from app.storage import get_store

    store = get_store()
    emap = {int(e["id"]): e
            for e in store.all_entities(include_hidden=True)}
    surv = emap.get(args.survivor)
    if surv is None:
        print(f"no entity with id {args.survivor}")
        return 1
    victims = []
    for eid in args.absorbed:
        if eid == args.survivor:
            continue
        e = emap.get(eid)
        if e is None:
            print(f"skipping id {eid}: no such entity")
            continue
        victims.append(e)
    if not victims:
        print("nothing to merge")
        return 1

    print(f"survivor: [{surv['id']}] {surv['name']!r} "
          f"({surv.get('kind') or '?'})")
    new_aliases: list[str] = []
    seen = {(surv["name"] or "").lower()}
    seen.update((a or "").lower() for a in surv.get("aliases") or [])
    for v in victims:
        names = [v["name"] or ""] + list(v.get("aliases") or [])
        take = []
        for n in names:
            if n and n.lower() not in seen:
                seen.add(n.lower())
                take.append(n)
        new_aliases.extend(take)
        print(f"absorb:   [{v['id']}] {v['name']!r} ({v.get('kind') or '?'})"
              + (f"  → aliases {take}" if take else "  (no new aliases)"))

    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to merge.")
        return 0

    now = time.time()
    for alias in new_aliases:
        store.touch_entity(int(surv["id"]), ts=now, alias=alias)
    for v in victims:
        store.set_entity_hidden(int(v["id"]), hidden=True)
    print(f"\nmerged {len(victims)} entities into "
          f"{surv['name']!r} (+{len(new_aliases)} aliases).")

    if args.no_rebuild:
        print("skipped rebuild — derived edges still point at hidden rows "
              "until the next graph job.")
        return 0
    from app.services import graph, project_rollup
    print("rebuilding derived edges…")
    graph.rebuild(store)
    if project_rollup.enabled():
        res = project_rollup.run(store)
        print(f"rollup refreshed ({res['associated']} associations).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
