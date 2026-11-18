"""Entity metrics, one row per seat (`app/services/entity_metrics.py`).

Each store is opened read-only and measured on its own: the forget rate, the
kind-correction rate, the share of rows never seen twice, and candidates
pending / promoted / expired / dismissed. Pass several `--db` to compare
seats (or copies of them) side by side; no seat's numbers are pooled with
another's.

    python3 scripts/entity_metrics.py --db data/quill.db
    python3 scripts/entity_metrics.py --db a/quill.db --db b/quill.db --days 7
    python3 scripts/entity_metrics.py --db copy/quill.db --as-of latest --json

`--days 0` is all time. `--as-of latest` ends the window at the store's own
newest event (a snapshot's last week rather than this week); the default is
now. A store from before the entity-model tables reads as zeros where it has
nothing to count. The seat label is the database's directory name (its file
name when that is not quill.db).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

COLUMNS = ("seat", "visible", "created", "forgotten", "forget_rate",
           "kind_corrected", "kind_correction_rate", "never_seen_twice",
           "never_seen_twice_share", "pending", "promoted", "expired",
           "dismissed")


def _args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, action="append", default=None,
                    help="a seat's quill.db (repeat for several seats; "
                         "default: the configured one)")
    ap.add_argument("--days", type=float, default=7.0,
                    help="window length in days (0: all time)")
    ap.add_argument("--as-of", default="now",
                    help="end of the window: now, latest (the store's "
                         "newest event) or a unix time")
    ap.add_argument("--json", action="store_true",
                    help="print every seat's full result as JSON")
    return ap.parse_args(argv)


def label(db: Path) -> str:
    return db.parent.name if db.name == "quill.db" else db.stem


def measure(db: Path, *, days: float, as_of: str = "now") -> dict:
    """`entity_metrics.compute` for one store, opened read-only."""
    from app.services import entity_metrics
    from app.storage import Store
    st = Store(db_path=db, readonly=True)
    try:
        if as_of == "now":
            now = None
        elif as_of == "latest":
            now = st.latest_event_time()
        else:
            now = float(as_of)
        return entity_metrics.compute(st, days=days, now=now)
    finally:
        st.close()


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    from app.services import entity_metrics
    dbs = args.db
    if not dbs:
        from app.config import settings
        dbs = [Path(settings.storage.db_path)]
    missing = [str(d) for d in dbs if not d.is_file()]
    if missing:
        print("no database at " + ", ".join(missing), file=sys.stderr)
        return 2
    out, rows = {}, []
    for db in dbs:
        m = measure(db, days=args.days, as_of=args.as_of)
        name = label(db)
        if name in out:
            name = str(db)
        out[name] = m
        rows.append(entity_metrics.row(name, m))
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    widths = {c: max(len(c), *(len(_fmt(r[c])) for r in rows))
              for c in COLUMNS}
    print("  ".join(c.ljust(widths[c]) for c in COLUMNS))
    for r in rows:
        print("  ".join(_fmt(r[c]).ljust(widths[c]) for c in COLUMNS))
    span = f"last {args.days:g} days" if args.days else "all time"
    print(f"({span}, as of {args.as_of}; one row per seat, never pooled)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
