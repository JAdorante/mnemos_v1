"""One-shot backfill: escalate_distill.jsonl → learning_pairs (Workstream A.3).

Imports every LABELED row (user_outcome accepted/rejected/edited) from the
legacy distill trail into the canonical SQLite learning store, with
verdict_source="legacy_distill". Idempotent: dedupe rides the store's
UNIQUE(task_type, content_hash) index, so re-running imports nothing twice.
Unlabeled rows are skipped — a pair without a verdict teaches nothing.

    python scripts/backfill_learning_pairs.py            # import
    python scripts/backfill_learning_pairs.py --dry-run  # count only
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

_LABELED = ("accepted", "rejected", "edited")


def load_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
        if not ln.strip():
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be imported, write nothing")
    args = ap.parse_args()

    from app.config import settings
    from app.services import learning_store

    if not learning_store.enabled():
        sys.exit("learning store is disabled (QUILL_LEARNING=0) — enable it "
                 "before backfilling.")

    rows = load_rows(Path(settings.escalate_log.path))
    labeled = [r for r in rows if r.get("user_outcome") in _LABELED]
    print(f"{len(rows)} distill rows, {len(labeled)} labeled")
    if args.dry_run:
        by = Counter((r.get("modality") or "vision", r["user_outcome"])
                     for r in labeled)
        for (mod, outcome), n in sorted(by.items()):
            print(f"  would import {n:>4}  {mod}/{outcome}")
        return

    imported, skipped = 0, 0
    for r in labeled:
        pair_id = learning_store.record_from_distill(
            r, r["user_outcome"], verdict_source="legacy_distill")
        if pair_id:
            imported += 1
        else:
            skipped += 1
    print(f"imported {imported}, skipped {skipped} "
          "(dupes, stubs, or empty inputs)")


if __name__ == "__main__":
    main()
