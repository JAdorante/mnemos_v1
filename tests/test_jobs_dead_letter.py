"""Jobs dead-letter (plan task 0.10).

max_attempts=5, exponential backoff via available_at, terminal status=dead,
and a console-facing dead_jobs listing so poisoned work is visible, not looping.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.worker import JobWorker
from app.storage import Store, job_backoff_s


class BackoffHelperTests(unittest.TestCase):
    def test_exponential_capped(self):
        self.assertEqual(job_backoff_s(1, base_s=2.0, cap_s=60.0), 2.0)
        self.assertEqual(job_backoff_s(2, base_s=2.0, cap_s=60.0), 4.0)
        self.assertEqual(job_backoff_s(3, base_s=2.0, cap_s=60.0), 8.0)
        self.assertEqual(job_backoff_s(10, base_s=2.0, cap_s=60.0), 60.0)


class JobsMigrationTests(unittest.TestCase):
    def test_legacy_error_becomes_dead_and_available_at_added(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                payload TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """)
        conn.execute(
            "INSERT INTO jobs (kind, status, attempts, error, created_at, updated_at) "
            "VALUES ('extract', 'error', 3, 'boom', 1.0, 2.0)")
        conn.commit()
        conn.close()

        store = Store(db_path=db, audio_dir=tmp / "audio")
        cols = {r["name"] for r in store._conn.execute(
            "PRAGMA table_info(jobs)").fetchall()}
        self.assertIn("available_at", cols)
        row = dict(store._conn.execute(
            "SELECT status FROM jobs").fetchone())
        self.assertEqual(row["status"], "dead")
        store.close()


class FailJobDeadLetterTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_retry_sets_backoff_and_skips_claim(self):
        jid = self.store.enqueue_job("extract")
        claimed = self.store.claim_job()
        self.assertEqual(claimed["id"], jid)
        self.assertEqual(claimed["attempts"], 1)
        status = self.store.fail_job(jid, "transient", max_attempts=5)
        self.assertEqual(status, "pending")
        row = dict(self.store._conn.execute(
            "SELECT status, available_at, attempts FROM jobs WHERE id = ?",
            (jid,)).fetchone())
        self.assertEqual(row["status"], "pending")
        self.assertIsNotNone(row["available_at"])
        self.assertGreater(row["available_at"], time.time())
        # Still in backoff ⇒ not claimable.
        self.assertIsNone(self.store.claim_job())

    def test_backoff_elapsed_allows_reclaim(self):
        jid = self.store.enqueue_job("extract")
        self.store.claim_job()
        self.store.fail_job(jid, "transient", max_attempts=5)
        self.store._conn.execute(
            "UPDATE jobs SET available_at = ? WHERE id = ?",
            (time.time() - 1, jid))
        self.store._conn.commit()
        claimed = self.store.claim_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], jid)
        self.assertEqual(claimed["attempts"], 2)

    def test_max_attempts_parks_dead(self):
        jid = self.store.enqueue_job("poison")
        for i in range(5):
            claimed = self.store.claim_job()
            self.assertIsNotNone(claimed, f"claim {i + 1}")
            # Clear backoff so the next claim can proceed in the loop.
            status = self.store.fail_job(jid, f"fail-{i + 1}", max_attempts=5)
            if i < 4:
                self.assertEqual(status, "pending")
                self.store._conn.execute(
                    "UPDATE jobs SET available_at = NULL WHERE id = ?", (jid,))
                self.store._conn.commit()
            else:
                self.assertEqual(status, "dead")
        row = dict(self.store._conn.execute(
            "SELECT status, attempts, error FROM jobs WHERE id = ?",
            (jid,)).fetchone())
        self.assertEqual(row["status"], "dead")
        self.assertEqual(row["attempts"], 5)
        self.assertIn("fail-5", row["error"])
        self.assertIsNone(self.store.claim_job())
        dead = self.store.dead_jobs()
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0]["id"], jid)
        stats = self.store.job_stats()
        self.assertEqual(stats["dead"], 1)

    def test_success_clears_error(self):
        jid = self.store.enqueue_job("ok")
        self.store.claim_job()
        self.store.finish_job(jid)
        row = dict(self.store._conn.execute(
            "SELECT status, error, available_at FROM jobs WHERE id = ?",
            (jid,)).fetchone())
        self.assertEqual(row["status"], "done")
        self.assertIsNone(row["error"])
        self.assertIsNone(row["available_at"])


class WorkerDeadLetterTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.worker = JobWorker(store=self.store, max_attempts=3,
                                poll_interval_s=0.05)
        self.calls = 0

        def _boom(_payload):
            self.calls += 1
            raise RuntimeError("always fails")

        self.worker.register("boom", _boom)

    def tearDown(self):
        self.worker.stop()
        try:
            self.store.close()
        except Exception:
            pass

    def test_worker_dead_letters_after_max(self):
        jid = self.worker.enqueue("boom")
        # Drive the loop synchronously: claim+dispatch until dead.
        for _ in range(5):
            job = self.store.claim_job()
            if job is None:
                # Clear backoff between manual dispatches.
                self.store._conn.execute(
                    "UPDATE jobs SET available_at = NULL "
                    "WHERE id = ? AND status = 'pending'", (jid,))
                self.store._conn.commit()
                job = self.store.claim_job()
            if job is None:
                break
            self.worker._dispatch(job)
        row = dict(self.store._conn.execute(
            "SELECT status, attempts FROM jobs WHERE id = ?", (jid,)).fetchone())
        self.assertEqual(row["status"], "dead")
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(self.calls, 3)
        self.assertIn("boom", self.worker.last_error or "")

    def test_claim_lock_error_does_not_kill_loop(self):
        """A transient SQLite lock must not tear down the job-worker thread."""
        self.worker.register("ok", lambda _p: None)
        self.worker.enqueue("ok")
        claims = {"n": 0}
        real_claim = self.store.claim_job

        def flaky_claim():
            claims["n"] += 1
            if claims["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_claim()

        with patch.object(self.store, "claim_job", side_effect=flaky_claim):
            self.worker.start()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                row = self.store._conn.execute(
                    "SELECT status FROM jobs LIMIT 1").fetchone()
                if row and row["status"] == "done":
                    break
                time.sleep(0.05)
            self.worker.stop()
            self.worker._thread.join(timeout=1.0)

        self.assertGreaterEqual(claims["n"], 2)
        self.assertIn("database is locked", self.worker.last_error or "")
        row = dict(self.store._conn.execute(
            "SELECT status FROM jobs LIMIT 1").fetchone())
        self.assertEqual(row["status"], "done")


class ConsoleJobsApiShapeTests(unittest.TestCase):
    """Smoke: /console/jobs payload includes dead-letter fields."""

    def test_endpoint_includes_dead(self):
        from app.api import routes as routes_mod
        import app.services.worker as worker_mod

        tmp = Path(tempfile.mkdtemp())
        store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        jid = store.enqueue_job("x")
        store.claim_job()
        store.fail_job(jid, "gone", max_attempts=1)

        class _Mem:
            def _ensure_store(self):
                return store

        with patch.object(routes_mod, "memory", _Mem()), \
             patch.object(worker_mod, "worker",
                          JobWorker(store=store, max_attempts=5)):
            body = routes_mod.console_jobs(limit=20)
        self.assertIn("dead", body)
        self.assertEqual(body["stats"]["dead"], 1)
        self.assertEqual(body["dead"][0]["id"], jid)
        self.assertEqual(body["max_attempts"], 5)
        store.close()


if __name__ == "__main__":
    unittest.main()
