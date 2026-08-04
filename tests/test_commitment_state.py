"""Plan 4.1 — commitment state machine: legality + status back-compat."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

NOW = 1_700_000_000.0


def _mk(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db")


class CommitmentStateUnitTests(unittest.TestCase):
    def test_status_derivation(self):
        from app.services import commitment_state as cs

        self.assertEqual(cs.status_for("detected"), "open")
        self.assertEqual(cs.status_for("active"), "open")
        self.assertEqual(cs.status_for("waiting"), "open")
        self.assertEqual(cs.status_for("completed"), "done")
        self.assertEqual(cs.status_for("cancelled"), "cancelled")
        self.assertEqual(cs.status_for("superseded"), "cancelled")

    def test_illegal_transition_raises(self):
        from app.services.commitment_state import TransitionError, require_legal

        with self.assertRaises(TransitionError):
            require_legal("completed", "waiting")
        with self.assertRaises(TransitionError):
            require_legal("cancelled", "completed")

    def test_completed_needs_evidence(self):
        from app.services import commitment_state as cs

        self.assertFalse(cs.evidence_ok_for_completed(None))
        self.assertFalse(cs.evidence_ok_for_completed({}))
        self.assertTrue(cs.evidence_ok_for_completed({"source": "user_mark_done"}))
        self.assertTrue(cs.evidence_ok_for_completed({"evidence_event_id": 12}))


class CommitmentStateStoreTests(unittest.TestCase):
    def test_new_commitment_starts_detected_open(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment(
                    "I'll send the deck", extracted_at=NOW)
                row = store.get_fact(fid)
                self.assertEqual(row["kind"], "commitment")
                self.assertEqual(row["status"], "open")
                self.assertEqual(row["commitment_state"], "detected")
            finally:
                store.close()

    def test_list_facts_status_open_back_compat(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                open_id = store.add_commitment("open one", extracted_at=NOW)
                done_id = store.add_commitment("done one", extracted_at=NOW + 1)
                store.set_fact_status(done_id, "done")
                opens = store.list_facts(kind="commitment", status="open")
                dones = store.list_facts(kind="commitment", status="done")
                open_ids = {r["fact_id"] for r in opens}
                done_ids = {r["fact_id"] for r in dones}
                self.assertIn(open_id, open_ids)
                self.assertNotIn(done_id, open_ids)
                self.assertIn(done_id, done_ids)
                self.assertEqual(
                    store.get_fact(done_id)["commitment_state"], "completed")
            finally:
                store.close()

    def test_transition_legality_and_log(self):
        from app.services.commitment_state import TransitionError

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment("ship it", extracted_at=NOW)
                store.transition_commitment(
                    fid, "active", reason="approve",
                    evidence={"source": "user_approve"}, actor="user")
                store.transition_commitment(
                    fid, "waiting", reason="awaiting_reply",
                    actor="system")
                out = store.transition_commitment(
                    fid, "completed", reason="agent_verified",
                    evidence={"evidence_event_id": 99, "source": "sent_folder"},
                    actor="agent")
                self.assertTrue(out["ok"])
                self.assertEqual(out["status"], "done")
                row = store.get_fact(fid)
                self.assertEqual(row["status"], "done")
                self.assertEqual(row["commitment_state"], "completed")
                self.assertIn("99", row["completion_evidence_json"] or "")
                txs = store.list_commitment_transitions(fid)
                self.assertGreaterEqual(len(txs), 3)
                self.assertEqual(txs[0]["to_state"], "completed")
                with self.assertRaises(TransitionError):
                    store.transition_commitment(
                        fid, "waiting", reason="illegal")
            finally:
                store.close()

    def test_completed_without_evidence_rejected(self):
        from app.services.commitment_state import TransitionError

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment("no evidence", extracted_at=NOW)
                store.transition_commitment(fid, "active", reason="approve")
                with self.assertRaises(TransitionError):
                    store.transition_commitment(fid, "completed", reason="oops")
                self.assertEqual(store.get_fact(fid)["status"], "open")
            finally:
                store.close()

    def test_set_fact_status_done_maps_through_machine(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment("mark done", extracted_at=NOW)
                self.assertTrue(store.set_fact_status(fid, "done"))
                row = store.get_fact(fid)
                self.assertEqual(row["status"], "done")
                self.assertEqual(row["commitment_state"], "completed")
                txs = store.list_commitment_transitions(fid)
                self.assertTrue(txs)
                self.assertEqual(txs[0]["to_state"], "completed")
            finally:
                store.close()

    def test_dismiss_cancels_and_leaves_open_filter(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment("noise", extracted_at=NOW)
                store.review_fact(fid, "dismissed")
                row = store.get_fact(fid)
                self.assertEqual(row["status"], "cancelled")
                self.assertEqual(row["commitment_state"], "cancelled")
                opens = store.list_facts(kind="commitment", status="open")
                self.assertNotIn(fid, {r["fact_id"] for r in opens})
            finally:
                store.close()

    def test_tasks_unaffected_by_state_machine(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                tid = store.add_task("plain task", extracted_at=NOW)
                store.set_fact_status(tid, "done")
                row = store.get_fact(tid)
                self.assertEqual(row["status"], "done")
                self.assertIsNone(row.get("commitment_state"))
                self.assertEqual(store.list_commitment_transitions(tid), [])
            finally:
                store.close()

    def test_migration_backfills_legacy_status(self):
        """Pre-4.1 commitments table (status only) gets state on Store open."""
        import sqlite3

        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.db"
            # Bootstrap a full modern schema, then strip 4.1 columns and
            # re-seed status-only rows to exercise ADD COLUMN + backfill.
            store = Store(path)
            store.close()
            conn = sqlite3.connect(str(path))
            conn.execute("DROP TABLE IF EXISTS commitment_transitions")
            conn.execute("DROP TABLE IF EXISTS commitments")
            conn.execute(
                """
                CREATE TABLE commitments (
                    fact_id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL,
                    from_person_id INTEGER,
                    to_person_id INTEGER,
                    due TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    FOREIGN KEY (fact_id) REFERENCES facts(id)
                )
                """
            )
            conn.execute(
                "INSERT INTO facts (kind, extracted_at, state) "
                "VALUES ('commitment', 1.0, 'active')")
            fid_open = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO commitments (fact_id, text, status) "
                "VALUES (?, 'legacy open', 'open')", (fid_open,))
            conn.execute(
                "INSERT INTO facts (kind, extracted_at, state) "
                "VALUES ('commitment', 2.0, 'active')")
            fid_done = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO commitments (fact_id, text, status) "
                "VALUES (?, 'legacy done', 'done')", (fid_done,))
            conn.commit()
            conn.close()

            store = Store(path)
            try:
                r1 = store.get_fact(fid_open)
                r2 = store.get_fact(fid_done)
                self.assertEqual(r1["commitment_state"], "active")
                self.assertEqual(r1["status"], "open")
                self.assertEqual(r2["commitment_state"], "completed")
                self.assertEqual(r2["status"], "done")
                opens = store.list_facts(kind="commitment", status="open")
                self.assertIn(fid_open, {r["fact_id"] for r in opens})
                self.assertNotIn(fid_done, {r["fact_id"] for r in opens})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
