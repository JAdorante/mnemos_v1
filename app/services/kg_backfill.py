"""KG v2 — Phase M1 backfill (architecture §20).

Walks every asserted/user row in legacy `relations` and records it into the
belief store (`kg_predicates` + one `kg_evidence` from source_event_id when
present), exactly as if it had been dual-written at the time. Idempotent:
`upsert_kg_predicate` finds existing open beliefs and the evidence dedupe
index swallows repeats, so re-running is safe.

Rows are replayed in created_at order so Change-3 conflict classification
sees history in the order it happened (a 2024 job change splits the 2022
belief, not the other way around).
"""
from __future__ import annotations

import time
from typing import Any


def run(store) -> dict[str, Any]:
    now = time.time()
    with store._lock:
        rows = [dict(r) for r in store._conn.execute(
            "SELECT * FROM relations WHERE origin IN ('asserted','user') "
            "ORDER BY COALESCE(created_at, 0), id").fetchall()]
    from app.services import kg_beliefs
    done = skipped = errors = 0
    for r in rows:
        try:
            ts = float(r.get("created_at") or now)
            out = kg_beliefs.record_from_relation(
                store,
                subj_type=r["subj_type"], subj_id=int(r["subj_id"]),
                predicate=r["predicate"],
                obj_type=r["obj_type"], obj_id=int(r["obj_id"]),
                origin=r.get("origin") or "asserted",
                source_event_id=r.get("source_event_id"),
                confidence=r.get("confidence"), ts=ts)
            if out.get("ok"):
                done += 1
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            print(f"[kg_backfill] relation {r.get('id')} failed ({exc}).")
    res = {"scanned": len(rows), "recorded": done, "skipped": skipped,
           "errors": errors}
    print(f"[kg_backfill] {res}")
    return res
