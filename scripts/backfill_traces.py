"""Backfill memory traces (Track A1) — CLI wrapper.

Usage (from the repo root):
    python scripts/backfill_traces.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import traces_backfill  # noqa: E402


def main() -> int:
    out = traces_backfill.run()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
