"""Purge leaked secrets from durable stores — one-time cleanup + rerunnable sweep.

The screen-capture pipeline OCR'd an open .env and the raw API key landed in
data/escalate_distill.jsonl, in quill.db events rows, and in the saved frame
JPEGs those rows reference. The write paths are now gated (services/redact.py),
but the already-written rows need scrubbing:

  * escalate_distill.jsonl — rows containing secrets are DROPPED (they are
    OCR of credential material; worthless and dangerous as training rows).
  * quill.db (+ any data/quill*.db backups) — text cells containing secrets
    are REDACTED IN PLACE (events are the episodic substrate; other tables
    join against them, so rows must survive). VACUUM afterwards clears the
    freelist/index remnants a plain UPDATE leaves behind.
  * frame files referenced by any purged/redacted row are DELETED — the
    secret is in the pixels too.

Dry-run by default; --apply makes the changes. Every touched file gets a
timestamped .bak copy first (frames excepted — they are deletions the .bak
of the row already accounts for).

Usage:
    python scripts/purge_secret_rows.py            # report only
    python scripts/purge_secret_rows.py --apply    # backup, purge, vacuum
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import redact  # noqa: E402


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _backup(path: Path) -> Path:
    bak = path.with_name(f"{path.name}.pre-purge-{_stamp()}.bak")
    shutil.copy2(path, bak)
    return bak


def purge_jsonl(path: Path, apply: bool) -> tuple[int, list[str]]:
    """Drop secret-bearing rows. Returns (dropped_count, frame_paths)."""
    if not path.is_file():
        print(f"  {path}: not found, skipping")
        return 0, []
    kept: list[str] = []
    frames: list[str] = []
    dropped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        # Scan the DECODED row, not the raw line: line-anchored patterns
        # (env_assignment) can't see through JSON-escaped newlines.
        try:
            kinds = redact.scan_payload(json.loads(line))
        except Exception:
            kinds = redact.scan(line)
        if not kinds:
            kept.append(line)
            continue
        dropped += 1
        try:
            fp = str(json.loads(line).get("frame_path") or "")
            if fp:
                frames.append(fp)
        except Exception:
            pass
        print(f"  drop row ({', '.join(kinds)})"
              + (f" frame={frames[-1]}" if frames else ""))
    if dropped and apply:
        _backup(path)
        path.write_text("\n".join(kept) + ("\n" if kept else ""),
                        encoding="utf-8")
    print(f"  {path.name}: {dropped} row(s) dropped, {len(kept)} kept"
          + ("" if apply else " (dry-run)"))
    return dropped, frames


def purge_db(path: Path, apply: bool) -> tuple[int, list[str]]:
    """Redact secret-bearing text cells in place. Returns (cells, frames)."""
    if not path.is_file():
        print(f"  {path}: not found, skipping")
        return 0, []
    con = sqlite3.connect(path)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    hits: list[tuple[str, str, int, str]] = []   # (table, column, rowid, new)
    frames: list[str] = []
    for t in tables:
        try:
            cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")')]
            rows = con.execute(f'SELECT rowid, * FROM "{t}"').fetchall()
        except sqlite3.Error:
            continue   # virtual/shadow tables that don't expose rowid scans
        for r in rows:
            for i, v in enumerate(r[1:]):
                if not isinstance(v, str):
                    continue
                # JSON cells are scanned/redacted DECODED — line-anchored
                # patterns can't see through JSON-escaped newlines.
                decoded = None
                if v.lstrip().startswith(("{", "[")):
                    try:
                        decoded = json.loads(v)
                    except Exception:
                        decoded = None
                if decoded is not None and redact.scan_payload(decoded):
                    new = json.dumps(redact.redact_payload(decoded),
                                     ensure_ascii=False, default=str)
                elif redact.contains_secret(v):
                    new = redact.redact_text(v)
                else:
                    continue
                hits.append((t, cols[i], r[0], new))
                # The row saw the secret — its frame did too.
                if isinstance(decoded, dict):
                    fp = str(decoded.get("frame_path") or "")
                    if fp:
                        frames.append(fp)
    if hits:
        for t, c, rid, _ in hits:
            print(f"  redact {path.name}:{t}.{c} rowid={rid}")
        if apply:
            _backup(path)
            for t, c, rid, new in hits:
                con.execute(f'UPDATE "{t}" SET "{c}"=? WHERE rowid=?',
                            (new, rid))
            con.commit()
            try:
                con.execute("VACUUM")   # clear freelist/index remnants
            except sqlite3.Error as exc:
                print(f"  VACUUM skipped ({exc}) — rerun with the app stopped")
    print(f"  {path.name}: {len(hits)} cell(s) redacted"
          + ("" if apply else " (dry-run)"))
    con.close()
    return len(hits), frames


def delete_frames(frames: list[str], apply: bool) -> int:
    n = 0
    for fp in sorted(set(frames)):
        p = Path(fp)
        if not p.is_absolute():
            p = ROOT / p
        if p.is_file():
            print(f"  delete frame {p}")
            if apply:
                p.unlink()
            n += 1
    print(f"  {n} frame file(s) deleted" + ("" if apply else " (dry-run)"))
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="make the changes (default is a dry-run report)")
    args = ap.parse_args()

    frames: list[str] = []
    print("escalate distill log:")
    _, f = purge_jsonl(ROOT / "data" / "escalate_distill.jsonl", args.apply)
    frames += f
    print("databases:")
    for db in sorted((ROOT / "data").glob("quill*.db")):
        _, f = purge_db(db, args.apply)
        frames += f
    print("frames:")
    delete_frames(frames, args.apply)
    if not args.apply:
        print("\nDry-run only. Rerun with --apply to make these changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
