"""Append-only, hash-chained relay log (fleet federation, Phase 4).

One JSON object per line at <coordinator data dir>/relay_log.jsonl:

    {"seq": 7, "ts": ..., "kind": "forward" | "refused" | "delivery_failed",
     "sender": "node-a", "recipients": [...], "signal": {...},
     "reason": "...", "prev_hash": "<hash of row 6>", "hash": "<sha256>"}

`hash` is SHA-256 over the row's canonical JSON without `hash`, and each
row's `prev_hash` is the previous row's `hash` (64 zeros for the first). Any
edit, deletion, or reordering breaks `verify_chain`. Rows are only ever
appended; nothing here rewrites the file.

The rest of the coordinator still writes whole JSON files, which is fine for
directories and topics but not for retention. This log is the record
compliance reads. What retention period and storage that record needs is a
question for the firm's compliance team.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from org_coordinator import store

GENESIS = "0" * 64
_lock = threading.Lock()


def log_path() -> Path:
    return Path(os.environ.get("QUILL_RELAY_LOG",
                               str(store.data_dir() / "relay_log.jsonl")))


def _canonical(row: dict) -> bytes:
    body = {k: v for k, v in row.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def row_hash(row: dict) -> str:
    return hashlib.sha256(_canonical(row)).hexdigest()


def _tail(p: Path) -> tuple[int, str]:
    """(last seq, last hash) — reads backwards from the end of the file."""
    if not p.is_file() or p.stat().st_size == 0:
        return 0, GENESIS
    with p.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0:
            step = min(4096, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = [ln for ln in buf.splitlines() if ln.strip()]
            if len(lines) >= 2 or (pos == 0 and lines):
                last = json.loads(lines[-1].decode("utf-8"))
                return int(last["seq"]), str(last["hash"])
    return 0, GENESIS


def append(kind: str, **fields: Any) -> dict:
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        seq, prev = _tail(p)
        row = {"seq": seq + 1, "ts": time.time(), "kind": kind, **fields,
               "prev_hash": prev}
        row["hash"] = row_hash(row)
        line = json.dumps(row, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    return row


def iter_rows(path: Path | None = None) -> Iterator[dict]:
    p = path or log_path()
    if not p.is_file():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def read(since: int = 0, limit: int = 500) -> list[dict]:
    out = []
    for row in iter_rows():
        if int(row.get("seq", 0)) > since:
            out.append(row)
            if len(out) >= limit:
                break
    return out


def verify_chain(path: Path | str | None = None) -> dict:
    """{"ok": bool, "entries": n, "bad_line": k | None, "error": str | None}."""
    p = Path(path) if path else log_path()
    prev = GENESIS
    n = 0
    if not p.is_file():
        return {"ok": True, "entries": 0, "bad_line": None, "error": None,
                "path": str(p)}
    with p.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                return _bad(p, n, lineno, "not JSON")
            if row.get("prev_hash") != prev:
                return _bad(p, n, lineno, "prev_hash does not link")
            if row.get("seq") != n + 1:
                return _bad(p, n, lineno, f"seq {row.get('seq')} != {n + 1}")
            if row_hash(row) != row.get("hash"):
                return _bad(p, n, lineno, "row does not match its hash")
            prev = row["hash"]
            n += 1
    return {"ok": True, "entries": n, "bad_line": None, "error": None,
            "path": str(p), "head": prev}


def _bad(p: Path, n: int, lineno: int, why: str) -> dict:
    return {"ok": False, "entries": n, "bad_line": lineno, "error": why,
            "path": str(p)}
