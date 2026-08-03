"""Step-1 self-test for the extracted-facts schema.

    python scripts/facts_schema_check.py

Runs the (idempotent, additive) migration against the live DB, then does a full
round-trip: resolve a person, add a task/commitment/claim, read open tasks, flip
a task to done. Verifies provenance links and the `extracted_at` bookkeeping.
Writes only throwaway rows and cleans them up, so it's safe to run repeatedly.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage import get_store


def main() -> None:
    store = get_store()
    now = time.time()
    print(f"[check] DB: {store.db_path}")

    # 1) migration added extracted_at
    cols = {r["name"] for r in
            store._conn.execute("PRAGMA table_info(events)").fetchall()}
    assert "extracted_at" in cols, "migration failed: events.extracted_at missing"
    print("[check] events.extracted_at present")

    # 2) all fact tables exist
    tables = {r["name"] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ("facts", "tasks", "commitments", "people", "entities"):
        assert t in tables, f"missing table: {t}"
    print("[check] fact tables present:", ", ".join(sorted(
        t for t in tables if t in {"facts", "tasks", "commitments", "people", "entities"})))

    facts_before = store.fact_count()

    # 3) person resolution collapses duplicates (case-insensitive)
    pid1 = store.resolve_person("Chris", ts=now)
    pid2 = store.resolve_person("chris", ts=now)
    assert pid1 == pid2, "person resolution did not collapse Chris/chris"
    print(f"[check] resolve_person collapsed to id={pid1}")

    # 4) task round-trip with provenance
    fid = store.add_task(
        "Send Chris the follow-up",
        source_event_id=None, source_span="chris needs the follow-up by friday",
        confidence=0.9, owner_person_id=pid1, due="Friday", extracted_at=now,
    )
    cid = store.add_commitment(
        "Justin will send the deck", from_person_id=pid1,
        source_span="I'll send the deck", confidence=0.8, extracted_at=now,
    )
    clid = store.add_claim("The demo is on Monday", confidence=0.7, extracted_at=now)
    print(f"[check] inserted task fid={fid}, commitment fid={cid}, claim fid={clid}")

    opens = store.open_tasks()
    mine = [t for t in opens if t["fact_id"] == fid]
    assert mine and mine[0]["owner"] == "Chris" and mine[0]["due"] == "Friday", \
        "open_tasks join/owner/due wrong"
    assert mine[0]["source_span"], "provenance span missing"
    print(f"[check] open_tasks join ok: owner={mine[0]['owner']} "
          f"due={mine[0]['due']} span={mine[0]['source_span']!r}")

    # 5) lifecycle
    assert store.set_task_status(fid, "done")
    assert all(t["fact_id"] != fid for t in store.open_tasks()), \
        "task still open after done"
    print("[check] task status open -> done")

    # 6) mark_extracted bookkeeping (no events needed — just exercise the query path)
    assert isinstance(store.unextracted_events(), list)
    print("[check] unextracted_events query ok")

    # cleanup throwaway rows
    store._conn.execute("DELETE FROM tasks WHERE fact_id = ?", (fid,))
    store._conn.execute("DELETE FROM commitments WHERE fact_id = ?", (cid,))
    store._conn.execute("DELETE FROM facts WHERE id IN (?, ?, ?)", (fid, cid, clid))
    store._conn.execute("DELETE FROM people WHERE id = ?", (pid1,))
    store._conn.commit()
    assert store.fact_count() == facts_before, "cleanup left rows behind"
    print("[check] cleaned up throwaway rows")

    print("\n[check] ALL PASS — fact schema + migration are live.")


if __name__ == "__main__":
    main()
