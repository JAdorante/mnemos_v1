"""Priors-continuity replay (Track A1 gate) — CLI wrapper.

Usage (from the repo root):
    python scripts/replay_attention.py [--days 7] [--gate 0.6]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import attention_replay  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=None)
    ap.add_argument("--gate", type=float, default=None)
    args = ap.parse_args()

    result = attention_replay.run(days=args.days, gate=args.gate)
    print(json.dumps(result, indent=2))
    if result["status"] == "insufficient":
        print("[replay] no shadow-bearing renders yet — fetch the field "
              "(or run the app) after backfill, then re-run.")
        return 0
    if result["status"] == "fail":
        print("[replay] GATE FAILED — shadow diverges from shipped gravity; "
              "do not cut over. Inspect low-tau renders' decompositions.")
        return 1
    print("[replay] gate passed — shadow tracks gravity at shipped priors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
