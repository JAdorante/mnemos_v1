#!/usr/bin/env python
"""Print the weekly distillation number for one seat (see
app/services/distill_metrics.py for how each part is computed).

    python scripts/distill_weekly.py                 # last complete week
    python scripts/distill_weekly.py --week 2026-W44 --json
    python scripts/distill_weekly.py --weeks 6       # a trend, oldest first
    python scripts/distill_weekly.py --save          # also write
                                                     # data/bench/distill_weekly/<week>.json

On the gb10 pilot, run it inside a seat:
    docker exec gb10-sparrow-user1-1 python scripts/distill_weekly.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import distill_metrics as dm  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=os.environ.get("QUILL_DATA_DIR",
                                                         "data"))
    ap.add_argument("--week", help="ISO week, e.g. 2026-W44")
    ap.add_argument("--weeks", type=int, default=1,
                    help="report this many weeks ending at --week")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args(argv)

    last = a.week or dm.week_of(
        (datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    t0, _ = dm.week_bounds(last)
    weeks = [dm.week_of(t0 - 7 * 86400 * i + 1)
             for i in range(max(1, a.weeks) - 1, -1, -1)]
    reports = [dm.report(a.data_dir, w) for w in weeks]
    if a.save:
        out = Path(a.data_dir) / "bench" / "distill_weekly"
        out.mkdir(parents=True, exist_ok=True)
        for r in reports:
            (out / f"{r['week']}.json").write_text(json.dumps(r, indent=2),
                                                   encoding="utf-8")
    if a.json:
        print(json.dumps(reports if len(reports) > 1 else reports[0],
                         indent=2))
    else:
        for r in reports:
            print(dm.headline(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
