"""Data audit — what is on disk, how big, and what is reclaimable.

Read-only by default: walks the repo's data roots (data/, the data_* clones,
browser session stores) and prints per-category size, file count, and age,
plus deep-dives where bloat hides:

  * LanceDB tables (data/lance/*.lance): actual data vs. accumulated version
    manifests. Lance writes one immutable version per append and never prunes
    on its own, so a high-churn table can hold GBs of manifests for MBs of
    vectors. The audit reports the reclaimable share per table.
  * quill.db: per-table row counts.

Usage:
    python scripts/data_audit.py                 # audit, human-readable
    python scripts/data_audit.py --json          # audit, machine-readable
    python scripts/data_audit.py --compact-lance # ALSO compact + prune Lance
                                                 # versions (rewrites the table;
                                                 # vector data is preserved,
                                                 # version history is dropped)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Repo data roots worth auditing, with what each holds. Paths that don't
# exist are skipped silently, so this is safe on a fresh clone.
AUDIT_ROOTS: dict[str, str] = {
    "data": "primary data dir (db, media, logs, vector store)",
    "data_boot": "dev/test db clone",
    "data_bridge": "dev/test db clone",
    "data_test": "dev/test db clone",
    "data_ui": "dev/test db clone",
    "sessions": "browser-agent sessions + Chrome profiles",
    "desktop_agent/sessions": "desktop-agent audit trail",
}

# Classification of data/ entries: what regenerates vs. what is the user's.
DATA_CLASSES: dict[str, str] = {
    "model_prices.json": "config (ship with copies)",
    "lance": "derived (re-embeddable from quill.db)",
    "quill.db": "personal (canonical memory db)",
    "audio": "personal (voice recordings)",
    "speakers": "personal (voice prints)",
    "frames": "personal (webcam captures)",
    "desktop_frames": "personal (screen captures)",
    "cam_diag": "personal (camera diagnostics)",
    "escalate_distill.jsonl": "personal (model distill trail)",
    "cognition.jsonl": "personal (cognition telemetry)",
    "model_calls.jsonl": "telemetry (cost/latency log)",
    "onboarding_profile.json": "personal (onboarding profile)",
    "onboarding_state.json": "state (onboarding gate)",
    "calibration.json": "state (camera calibration)",
    "eval": "derived (eval outputs)",
    "bench": "derived (benchmark outputs)",
}


def walk_stats(path: Path) -> dict:
    """Total bytes / file count / mtime range under path (file itself if a file)."""
    total = files = 0
    oldest = newest = None
    if path.is_file():
        st = path.stat()
        return {"bytes": st.st_size, "files": 1,
                "oldest": st.st_mtime, "newest": st.st_mtime}
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat()
                        total += st.st_size
                        files += 1
                        if oldest is None or st.st_mtime < oldest:
                            oldest = st.st_mtime
                        if newest is None or st.st_mtime > newest:
                            newest = st.st_mtime
        except OSError:
            continue
    return {"bytes": total, "files": files, "oldest": oldest, "newest": newest}


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:,.1f} TB"


def fmt_age(ts: float | None) -> str:
    if ts is None:
        return "-"
    days = (time.time() - ts) / 86400
    return f"{days:.0f}d" if days >= 1 else f"{(time.time() - ts) / 3600:.0f}h"


def audit_lance(lance_dir: Path) -> list[dict]:
    """Per-table breakdown: real data vs. version-manifest overhead."""
    out = []
    if not lance_dir.is_dir():
        return out
    for tdir in sorted(lance_dir.glob("*.lance")):
        row = {"table": tdir.stem}
        for sub in ("data", "_versions", "_transactions", "_indices"):
            s = walk_stats(tdir / sub) if (tdir / sub).is_dir() else {"bytes": 0, "files": 0}
            row[sub] = {"bytes": s["bytes"], "files": s["files"]}
        overhead = row["_versions"]["bytes"] + row["_transactions"]["bytes"]
        payload = row["data"]["bytes"]
        row["versions"] = row["_versions"]["files"]
        row["reclaimable_bytes"] = overhead
        row["bloat_ratio"] = round(overhead / payload, 1) if payload else None
        out.append(row)
    return out


def audit_sqlite(db_path: Path) -> list[dict]:
    if not db_path.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return [{"table": n,
                     "rows": con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]}
                    for n in names]
        finally:
            con.close()
    except sqlite3.Error as exc:
        print(f"[audit] quill.db skipped ({exc}).", file=sys.stderr)
        return []


def compact_lance(lance_dir: Path) -> None:
    """Compact fragments and prune all old versions. Keeps current data only."""
    from datetime import timedelta

    import lancedb

    db = lancedb.connect(str(lance_dir))
    for name in db.list_tables():
        tbl = db.open_table(name)
        before = walk_stats(lance_dir / f"{name}.lance")["bytes"]
        print(f"[compact] {name}: optimizing (this rewrites fragments)...")
        tbl.optimize(cleanup_older_than=timedelta(seconds=0))
        after = walk_stats(lance_dir / f"{name}.lance")["bytes"]
        print(f"[compact] {name}: {fmt_bytes(before)} -> {fmt_bytes(after)} "
              f"(freed {fmt_bytes(before - after)})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit the repo's data footprint.")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    ap.add_argument("--top", type=int, default=10,
                    help="largest individual files to list (default 10)")
    ap.add_argument("--compact-lance", action="store_true",
                    help="after auditing, compact Lance tables and drop old versions")
    ap.add_argument("--check", action="store_true",
                    help="threshold check only (quiet; exit 1 if anything is "
                         "oversized) — for schedulers")
    args = ap.parse_args()

    if args.check:
        sys.path.insert(0, str(ROOT))
        from app.services.data_watch import check as watch_check

        problems = watch_check()
        for p in problems:
            print(f"WARNING: {p}")
        if not problems:
            print("data footprint ok.")
        sys.exit(1 if problems else 0)

    report: dict = {"generated_unix": int(time.time()), "roots": [], "data_entries": [],
                    "lance": [], "quill_db": [], "largest_files": []}

    for rel, desc in AUDIT_ROOTS.items():
        p = ROOT / rel
        if not p.exists():
            continue
        s = walk_stats(p)
        report["roots"].append({"root": rel, "desc": desc, **s})

    data_dir = ROOT / "data"
    if data_dir.is_dir():
        for entry in sorted(data_dir.iterdir(), key=lambda e: e.name):
            s = walk_stats(entry)
            report["data_entries"].append({
                "name": entry.name,
                "class": DATA_CLASSES.get(entry.name, "unclassified"),
                **s,
            })
        report["data_entries"].sort(key=lambda r: r["bytes"], reverse=True)

    report["lance"] = audit_lance(data_dir / "lance")
    report["quill_db"] = audit_sqlite(data_dir / "quill.db")

    # Largest individual files outside the vector store (its size is explained above).
    big: list[tuple[int, str]] = []
    for root in report["roots"]:
        base = ROOT / root["root"]
        for dirpath, dirnames, filenames in os.walk(base):
            if "lance" in Path(dirpath).parts:
                continue
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    big.append((fp.stat().st_size, str(fp.relative_to(ROOT))))
                except OSError:
                    continue
    big.sort(reverse=True)
    report["largest_files"] = [{"bytes": b, "path": p} for b, p in big[: args.top]]

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("== Data roots ==")
        for r in sorted(report["roots"], key=lambda x: x["bytes"], reverse=True):
            print(f"  {r['root']:<24} {fmt_bytes(r['bytes']):>10}  "
                  f"{r['files']:>7} files  newest {fmt_age(r['newest'])} ago")

        print("\n== data/ by entry ==")
        for e in report["data_entries"]:
            print(f"  {e['name']:<24} {fmt_bytes(e['bytes']):>10}  "
                  f"{e['files']:>7} files  {e['class']}")

        if report["lance"]:
            print("\n== LanceDB (data/lance) ==")
            for t in report["lance"]:
                print(f"  table '{t['table']}': data {fmt_bytes(t['data']['bytes'])} "
                      f"in {t['data']['files']} fragments; {t['versions']} versions "
                      f"holding {fmt_bytes(t['reclaimable_bytes'])} of manifests"
                      + (f" ({t['bloat_ratio']}x the data)" if t["bloat_ratio"] else ""))
                if t["reclaimable_bytes"] > 100 * 1024 * 1024:
                    print("    -> reclaim with: python scripts/data_audit.py --compact-lance")

        if report["quill_db"]:
            print("\n== quill.db row counts ==")
            for t in report["quill_db"]:
                print(f"  {t['table']:<28} {t['rows']:>9,}")

        if report["largest_files"]:
            print(f"\n== Largest {len(report['largest_files'])} files (excl. lance) ==")
            for f in report["largest_files"]:
                print(f"  {fmt_bytes(f['bytes']):>10}  {f['path']}")

    if args.compact_lance:
        print()
        compact_lance(data_dir / "lance")


if __name__ == "__main__":
    main()
