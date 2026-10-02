"""Hash-chained org audit log.

entry_hash = sha256(prev_hash || seq || actor || action || object_ref ||
payload_hash || at) — app.services.records.canonical.audit_entry_hash, the
same function the node log uses. `append` runs inside the caller's
transaction and locks the org's chain head (orgs.audit_seq/audit_head FOR
UPDATE), so an entry commits with the change it describes or not at all, and
concurrent writers serialize per org without gaps.

`verify` is pure over an iterable of rows, so scripts/verify_audit_chain.py
and the tests check the same thing whether the rows come from Postgres or a
synthetic generator.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from app.services.records.canonical import (GENESIS_HASH, audit_entry_hash,
                                            canonical_hash)


def append(repo, actor: str, action: str, object_ref: str,
           payload: dict[str, Any] | None = None, *,
           payload_hash: str | None = None,
           at: float | None = None) -> dict[str, Any]:
    at = float(at if at is not None else time.time())
    p_hash = payload_hash or canonical_hash(payload or {})
    seq, prev = repo.lock_audit_head()
    seq += 1
    entry = {"seq": seq, "actor": actor, "action": action,
             "object_ref": object_ref, "payload_hash": p_hash,
             "prev_hash": prev,
             "entry_hash": audit_entry_hash(prev, seq, actor, action,
                                            object_ref, p_hash, at),
             "at": at}
    repo.append_audit(entry)
    return entry


def verify(rows: Iterable[dict[str, Any]], *,
           anchors: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    """Walk the chain. Returns the first bad seq and why, or ok + head."""
    by_seq = {int(a["seq"]): a for a in anchors}
    prev, n, expect = GENESIS_HASH, 0, 1
    for r in rows:
        seq = int(r["seq"])
        if seq != expect:
            return {"ok": False, "bad_seq": seq, "reason": "gap_or_reorder",
                    "checked": n}
        if r["prev_hash"] != prev:
            return {"ok": False, "bad_seq": seq, "reason": "prev_hash",
                    "checked": n}
        want = audit_entry_hash(prev, seq, r["actor"], r["action"],
                                r["object_ref"], r["payload_hash"], r["at"])
        if want != r["entry_hash"]:
            return {"ok": False, "bad_seq": seq, "reason": "entry_hash",
                    "checked": n}
        anchor = by_seq.get(seq)
        if anchor is not None and anchor["entry_hash"] != r["entry_hash"]:
            return {"ok": False, "bad_seq": seq, "reason": "anchor_mismatch",
                    "checked": n}
        prev, n, expect = r["entry_hash"], n + 1, expect + 1
    missing = [s for s in by_seq if s >= expect]
    if missing:
        return {"ok": False, "bad_seq": min(missing),
                "reason": "anchor_beyond_chain", "checked": n}
    return {"ok": True, "checked": n, "head": prev}


def anchor_dir() -> Path | None:
    import os
    raw = (os.environ.get("QUILL_ORG_AUDIT_ANCHOR_DIR") or "").strip()
    return Path(raw) if raw else None


def anchor(repo, *, day: str, now: float | None = None) -> dict[str, Any] | None:
    """Publish the chain head as of now to the anchor location and record it.

    Phase 1 location is a directory the org controls (mount an object-locked
    bucket there); the file is write-once — an existing anchor file for the
    day is never overwritten."""
    now = float(now if now is not None else time.time())
    seq, head = repo.lock_audit_head()
    if seq == 0:
        return None
    location = "db-only"
    root = anchor_dir()
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{repo.org_id}-{day}.json"
        body = json.dumps({"org_id": repo.org_id, "day": day, "seq": seq,
                           "entry_hash": head, "anchored_at": now},
                          sort_keys=True)
        with open(path, "x", encoding="utf-8") as fh:   # never overwrite
            fh.write(body)
        location = str(path)
    repo.insert_anchor(day=day, seq=seq, entry_hash=head, location=location,
                       anchored_at=now)
    return {"day": day, "seq": seq, "entry_hash": head, "location": location}


def read_anchor_files(org_id: str) -> list[dict[str, Any]]:
    root = anchor_dir()
    if root is None or not root.is_dir():
        return []
    out = []
    for p in sorted(root.glob(f"{org_id}-*.json")):
        try:
            out.append(json.loads(p.read_text("utf-8")))
        except (OSError, ValueError):
            out.append({"seq": -1, "entry_hash": "", "file": str(p)})
    return out
