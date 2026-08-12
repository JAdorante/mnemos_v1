"""One-shot people-network hygiene after capture/admission gate fixes.

Soft-hides diarization placeholders, merges first-name / typo duplicates into
canonical full names, and hides thin single-token ambient names that never
earned promotion. Does NOT delete rows — reversible via hide_from_people /
soft_merge.

    python scripts/people_network_cleanup.py            # dry-run
    python scripts/people_network_cleanup.py --apply    # write

Run with the app stopped (needs the SQLite write lock).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.services.name_quality import is_plausible_person  # noqa: E402
from app.storage import get_store  # noqa: E402

_SPEAKER = re.compile(r"(?i)^speaker(\s*\d+)?$")
_UNKNOWN = re.compile(r"(?i)^unknown\s+speaker$")

# First-name / typo → preferred full name (case-insensitive match on left).
_MERGE_INTO = {
    "justin": "Justin Adorante",
    "patrick": "Patrick Adorante",
    "hugh saiva": "Hugh Salva",
    "kristi sacco": "Kristi Adorante",
}

# Thin ambient first names with no open work — soft-hide unless they merge.
_HIDE_SINGLE = frozenset({"marc", "sarah", "scott"})


def _by_name(people: list[dict]) -> dict[str, dict]:
    out = {}
    for p in people:
        key = (p.get("name") or p.get("canonical_name") or "").strip().lower()
        if key:
            out[key] = p
    return out


def plan(store) -> dict:
    people = [p for p in store.all_people()
              if not p.get("hide_from_people") and not p.get("canonical_person_id")]
    by = _by_name(people)

    hide: list[dict] = []
    merge: list[dict] = []
    promote: list[dict] = []
    seen_ids: set[int] = set()

    for p in people:
        name = (p.get("name") or "").strip()
        low = name.lower()
        pid = int(p["id"])

        if _SPEAKER.match(name) or _UNKNOWN.match(name) or not is_plausible_person(name):
            hide.append({"id": pid, "name": name, "reason": "diarization_or_implausible"})
            seen_ids.add(pid)
            continue

        target_name = _MERGE_INTO.get(low)
        if target_name:
            survivor = by.get(target_name.lower())
            if survivor and int(survivor["id"]) != pid:
                merge.append({
                    "absorbed_id": pid, "absorbed_name": name,
                    "survivor_id": int(survivor["id"]),
                    "survivor_name": survivor.get("name"),
                    "reason": "alias_merge",
                })
                seen_ids.add(pid)
                continue

        if low in _HIDE_SINGLE and (p.get("promotion_state") or "") == "candidate":
            hide.append({"id": pid, "name": name, "reason": "thin_ambient_first_name"})
            seen_ids.add(pid)
            continue

        # Promote onboarding-shaped full names still stuck as candidates.
        tokens = [t for t in name.split() if t]
        if (len(tokens) >= 2
                and (p.get("promotion_state") or "") == "candidate"
                and pid not in seen_ids):
            promote.append({"id": pid, "name": name, "to": "recognized"})

    return {"hide": hide, "merge": merge, "promote": promote}


def apply(store, planned: dict) -> dict:
    out = {"hidden": [], "merged": [], "promoted": []}
    now = time.time()
    for row in planned["hide"]:
        store.set_person_hidden(int(row["id"]), hidden=True, ts=now)
        out["hidden"].append(row)
    for row in planned["merge"]:
        store.soft_merge_people(
            int(row["survivor_id"]), int(row["absorbed_id"]), ts=now)
        # Keep the absorbed spelling as an alias on the survivor.
        try:
            store.touch_person(int(row["survivor_id"]), now,
                               alias=row["absorbed_name"])
        except Exception:
            pass
        out["merged"].append(row)
    for row in planned["promote"]:
        store.set_person_promotion(int(row["id"]), row["to"], ts=now)
        out["promoted"].append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry-run)")
    args = ap.parse_args()

    store = get_store()
    planned = plan(store)

    print(f"\nHide ({len(planned['hide'])}):")
    for r in planned["hide"]:
        print(f"  - {r['name']!r}  [{r['reason']}]")
    print(f"\nMerge ({len(planned['merge'])}):")
    for r in planned["merge"]:
        print(f"  - {r['absorbed_name']!r} → {r['survivor_name']!r}")
    print(f"\nPromote ({len(planned['promote'])}):")
    for r in planned["promote"]:
        print(f"  - {r['name']!r} → {r['to']}")

    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply.\n")
        return 0

    result = apply(store, planned)
    stamp = int(time.time())
    backup = Path("data") / f"people_network_cleanup_{stamp}.json"
    backup.write_text(
        json.dumps({"plan": planned, "result": result}, indent=2,
                   ensure_ascii=False, default=str),
        encoding="utf-8")
    print(f"\nApplied: hid {len(result['hidden'])}, "
          f"merged {len(result['merged'])}, "
          f"promoted {len(result['promoted'])}.")
    print(f"Backup: {backup}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
