"""Read the peer telemetry trail and print the three pilot metrics.

    python scripts/peer_metrics.py                 # this instance
    python scripts/peer_metrics.py --json
    QUILL_DATA_DIR=/path/to/volume python scripts/peer_metrics.py

The trail is metadata only — no question or answer text — so this is safe to
read while the pilot is running without reading anyone's memory.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _secs(v) -> str:
    return "n/a" if v is None else f"{v:.1f}s"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from app.services import peer_telemetry as tel
    roll = tel.rollup()

    if args.json:
        print(json.dumps(roll, indent=2))
        return 0

    if not roll["asks_sent"]:
        print("\nNo peer traffic recorded yet.\n")
        return 0

    print(f"\npeer round trips — {roll['asks_sent']} asks sent\n")
    print(f"  completion rate    {_pct(roll['completion_rate'])} "
          f"({roll['answered_usable']} usable answers)")
    print(f"  median time-to-answer  {_secs(roll['median_answer_s'])}")
    print(f"  repeat use         {_pct(roll['repeat_use'])} "
          f"({roll['pairs_repeat']}/{roll['pairs_asked']} pairs asked twice)")
    print()
    # The diagnostic half: WHERE it failed, so a low completion rate points at
    # consent, retrieval, or composition instead of all three at once.
    print(f"  median wait on a human  {_secs(roll['median_verdict_wait_s'])}")
    print(f"  gate actions       {roll['gate_actions'] or '{}'}")
    print(f"  terminal states    {roll['terminal_states'] or '{}'}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
