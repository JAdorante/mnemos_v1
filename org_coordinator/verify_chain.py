"""Check the relay log's hash chain.

    python -m org_coordinator.verify_chain [--path data/org_coordinator/relay_log.jsonl]

Exit 0 when every row re-hashes to its stored hash, every prev_hash links,
and seqs have no gaps. Exit 1 on the first bad row, which it names.
"""
from __future__ import annotations

import argparse
import json
import sys

from org_coordinator import relay_log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", default=None,
                    help="relay_log.jsonl (default: the coordinator's)")
    args = ap.parse_args(argv)
    out = relay_log.verify_chain(args.path)
    print(json.dumps(out, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
