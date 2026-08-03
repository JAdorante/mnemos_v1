"""Ambient / entity kind hygiene — soft-hide noise + remap kinds.

Targets celebrity/news people, person-shaped projects, product→tool remaps,
and ambient-only entities. Does NOT delete rows — reversible soft-hide and
kind updates only.

    python scripts/ambient_cleanup.py              # dry-run report
    python scripts/ambient_cleanup.py --apply      # reclassify + soft-hide
    python scripts/ambient_cleanup.py --apply --backfill-kg
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.services import ambient_cleanup as ac  # noqa: E402
from app.storage import get_store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="apply reclassify/hide (default: dry-run)")
    ap.add_argument("--backfill-kg", action="store_true",
                    help="also backfill asserted/user relations into kg_*")
    ap.add_argument("--limit", type=int, default=2000,
                    help="max people/entities to flag")
    args = ap.parse_args()

    store = get_store()
    plan = ac.plan(store, limit=args.limit)

    print(f"\nAmbient PEOPLE to soft-hide ({len(plan['people'])}):")
    for p in plan["people"]:
        print(f"  - {p['name']!r}  [{p['reason']}]")

    ents = plan["entities"]
    by_action = Counter((e.get("action") or "hide") for e in ents)
    by_reason = Counter((e.get("reason") or "?") for e in ents)
    print(f"\nEntity hygiene ({len(ents)}): "
          + ", ".join(f"{k}={v}" for k, v in sorted(by_action.items())))
    print("  by reason: "
          + ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))
    for e in ents:
        act = e.get("action") or "hide"
        extra = ""
        if act == "reclassify":
            extra = f" → {e.get('to_kind')}"
        print(f"  - [{act}{extra}] {e['name']!r} ({e.get('kind') or '?'})  "
              f"[{e['reason']}]")

    if not args.apply and not args.backfill_kg:
        print("\nDRY RUN — nothing changed. Re-run with --apply to "
              "reclassify/soft-hide, and/or --backfill-kg for legacy evidence.\n")
        return 0

    stamp = int(time.time())
    backup = Path("data") / f"ambient_cleanup_{stamp}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    result = {"people": [], "entities": [], "kg_backfill": None}

    if args.apply:
        result.update(ac.apply(store, plan))
        n_re = sum(1 for e in result["entities"]
                   if (e.get("action") or "") == "reclassify")
        n_hp = sum(1 for e in result["entities"]
                   if (e.get("action") or "") == "hide_person")
        n_h = sum(1 for e in result["entities"]
                  if (e.get("action") or "hide") == "hide")
        print(f"\nApplied: {len(result['people'])} people hidden; "
              f"{n_re} reclassified, {n_hp} hide+person, {n_h} hidden entities.")

    if args.backfill_kg:
        bf = ac.backfill_kg(store)
        result["kg_backfill"] = bf
        print(f"KG backfill: {bf.get('predicates', 0)} predicates, "
              f"{bf.get('evidence', 0)} evidence rows "
              f"({bf.get('skipped', 0)} skipped).")

    backup.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8")
    print(f"Backup log: {backup}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
