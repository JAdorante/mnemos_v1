"""#6 — Desktop telemetry report: measure the guarded desktop agent's reliability.

Reads the append-only audit log the driver writes for every executed and refused
action, and prints performance + safety metrics — launch/run success rates,
refusals bucketed by reason, per-task action counts + budget exhaustion,
repeated-failure loops, and the safety counters (jail escapes, unknown apps,
blocked verbs, shell attempts). Pure read-model; it launches nothing.

Usage:
    python scripts/desktop_telemetry.py                 # all-time report
    python scripts/desktop_telemetry.py --window 3600   # last hour only
    python scripts/desktop_telemetry.py --json          # raw metrics as JSON
    python scripts/desktop_telemetry.py --audit PATH     # a specific log file
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from desktop_agent import telemetry  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Desktop agent reliability report")
    ap.add_argument("--window", type=float, default=None,
                    help="only count actions from the last N seconds")
    ap.add_argument("--audit", type=Path, default=None,
                    help="path to a desktop_audit.jsonl (default: the live one)")
    ap.add_argument("--json", action="store_true", help="emit raw metrics as JSON")
    args = ap.parse_args()

    metrics = telemetry.desktop_metrics(path=args.audit, window_s=args.window)
    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        print(telemetry.format_report(metrics))


if __name__ == "__main__":
    main()
