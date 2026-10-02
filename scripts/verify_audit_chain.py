"""Walk an org's hash-chained audit log and check its anchors.

    QUILL_ORG_DATABASE_URL=postgresql+psycopg://... \
        python scripts/verify_audit_chain.py --org org_xxx [--anchor-dir DIR]

Exit 0 when every entry re-hashes to its stored entry_hash, every prev_hash
links, seqs have no gaps, and every anchor (DB row and anchor file) matches
the chain at its seq. Exit 1 on the first bad entry, which it names.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--anchor-dir", default=None)
    args = ap.parse_args(argv)
    if args.anchor_dir:
        os.environ["QUILL_ORG_AUDIT_ANCHOR_DIR"] = args.anchor_dir
    from org_coordinator.records import service
    from org_coordinator.repo import Database
    db = Database(pool_size=1)
    try:
        out = service.verify_chain(db, args.org)
    finally:
        db.dispose()
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
