"""Undo over-aggressive ambient_cleanup hide_person false positives.

Re-reads a backup JSON from scripts/ambient_cleanup.py and un-hides any
entity that was hide_person'd but fails the *current* (stricter)
is_person_shaped_entity_name gate. True person rows stay hidden.

    python scripts/restore_entity_hygiene.py data/ambient_cleanup_1784748946.json
    python scripts/restore_entity_hygiene.py data/ambient_cleanup_1784748946.json --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.services.name_quality import is_person_shaped_entity_name  # noqa: E402
from app.storage import get_store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("backup", type=Path, help="ambient_cleanup_*.json backup")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    data = json.loads(args.backup.read_text(encoding="utf-8"))
    ents = data.get("entities") or []
    store = get_store()

    restore, keep = [], []
    for e in ents:
        if (e.get("action") or "") != "hide_person":
            continue
        name = (e.get("name") or "").strip()
        eid = int(e["id"])
        if is_person_shaped_entity_name(name):
            keep.append(e)
        else:
            restore.append(e)

    # Also: products remapped to tool that are actually person-shaped now
    # stay as tools — convert those to hide_person instead.
    flip_to_person = []
    for e in ents:
        if (e.get("action") or "") != "reclassify":
            continue
        name = (e.get("name") or "").strip()
        if is_person_shaped_entity_name(name):
            flip_to_person.append(e)

    print(f"hide_person keep (real people): {len(keep)}")
    for e in keep:
        print(f"  keep hidden: {e['name']!r}")
    print(f"hide_person restore (false positives): {len(restore)}")
    for e in restore:
        print(f"  unhide: {e['name']!r}")
    print(f"reclassify→person (people labeled product): {len(flip_to_person)}")
    for e in flip_to_person:
        print(f"  hide+person: {e['name']!r}")

    if not args.apply:
        print("\nDRY RUN — re-run with --apply to write.\n")
        return 0

    n_u = n_f = 0
    for e in restore:
        store.set_entity_hidden(int(e["id"]), hidden=False)
        n_u += 1
    for e in flip_to_person:
        eid = int(e["id"])
        store.set_entity_hidden(eid, hidden=True)
        try:
            store.resolve_person(e["name"])
        except Exception:
            pass
        n_f += 1
    print(f"\nUnhid {n_u}; flipped {n_f} product-people to hide+person.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
