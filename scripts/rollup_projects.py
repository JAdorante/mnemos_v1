"""Project rollup — assign each tool/idea/org its home project, right now.

Computes fact co-attribution over the EXISTING fact→entity `about` edges and
mints one entity→project `associated_project` edge per entity whose facts
dominantly live in a single project (see app/services/project_rollup.py).
The nightly graph job re-runs this automatically; this script is the
run-it-now path.

    python scripts/rollup_projects.py              # compute + write edges
    python scripts/rollup_projects.py --dry-run    # print, write nothing
    python scripts/rollup_projects.py --rebuild    # graph.rebuild() first,
                                                   # so brand-new facts count
    python scripts/rollup_projects.py --min-facts 2 --dominance 0.5
                                                   # loosen the gates
    python scripts/rollup_projects.py --home-kinds project,org
                                                   # let orgs be homes too
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the associations without writing edges")
    ap.add_argument("--rebuild", action="store_true",
                    help="run graph.rebuild() first so new facts have edges")
    ap.add_argument("--min-facts", type=int, default=None,
                    help="min shared facts with the winning project (default 3)")
    ap.add_argument("--dominance", type=float, default=None,
                    help="min share of the winning project (default 0.6)")
    ap.add_argument("--home-kinds", default=None,
                    help="comma-separated entity kinds that can be a home "
                         "(default: project)")
    args = ap.parse_args()

    if args.min_facts is not None:
        os.environ["QUILL_ROLLUP_MIN_FACTS"] = str(args.min_facts)
    if args.dominance is not None:
        os.environ["QUILL_ROLLUP_DOMINANCE"] = str(args.dominance)
    if args.home_kinds:
        os.environ["QUILL_ROLLUP_HOME_KINDS"] = args.home_kinds

    from app.services import project_rollup
    from app.storage import get_store

    store = get_store()
    if args.rebuild:
        from app.services import graph
        print("rebuilding graph edges first…")
        graph.rebuild()

    assocs = project_rollup.compute(store)
    by_project: dict[str, list[dict]] = {}
    for a in assocs:
        by_project.setdefault(a["project_name"], []).append(a)

    if not assocs:
        print("\nNo dominant associations found. Loosen with --min-facts / "
              "--dominance, or check that facts have `about` edges "
              "(--rebuild).")
    for pname in sorted(by_project, key=str.lower):
        rows = by_project[pname]
        print(f"\n{pname}  ({len(rows)} entities)")
        for a in rows:
            print(f"  {a['entity_name']:<40} {a['entity_kind']:<8} "
                  f"share {a['share']:.2f}  facts {a['facts']}")

    if args.dry_run:
        print(f"\nDRY RUN — {len(assocs)} associations computed, "
              "no edges written.")
        return 0

    res = project_rollup.run(store)
    print(f"\nWrote {res['associated']} associated_project edges "
          f"(cleared {res['cleared']} stale). The entities page now groups "
          "by these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
