"""Extract retry cap (plan task 0.9).

3 LLM failures on a turn ⇒ park extract_status='failed' (mark extracted) so
consolidate / settle-nudge cannot spin forever on a poisoned transcript.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services.extractor import EXTRACT_MAX_ATTEMPTS, Extractor
from app.storage import Store


def _audio(text: str, t: float) -> Event:
    return Event(time=t, modality=Modality.AUDIO, raw=text,
                 source="audio.whisper", confidence=0.9)


class ExtractColumnMigrationTests(unittest.TestCase):
    def test_pre_fix_db_gains_attempt_columns(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time REAL NOT NULL,
                modality TEXT NOT NULL,
                raw TEXT NOT NULL,
                summary TEXT,
                source TEXT,
                confidence REAL,
                people TEXT,
                tasks TEXT,
                entities TEXT,
                meta TEXT,
                audio_path TEXT,
                extracted_at REAL
            )
            """)
        conn.execute(
            "INSERT INTO events (time, modality, raw, extracted_at) "
            "VALUES (1.0, 'audio', 'hi', NULL)")
        conn.commit()
        conn.close()

        store = Store(db_path=db, audio_dir=tmp / "audio")
        cols = {r["name"] for r in store._conn.execute(
            "PRAGMA table_info(events)").fetchall()}
        self.assertIn("extract_attempts", cols)
        self.assertIn("extract_status", cols)
        row = dict(store._conn.execute(
            "SELECT extract_attempts, extract_status FROM events").fetchone())
        self.assertEqual(row["extract_attempts"], 0)
        self.assertIsNone(row["extract_status"])
        store.close()


class ExtractAttemptStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_bump_and_park(self):
        eid = self.store.insert(_audio("I'll send Marc the quote", 1000.0))
        self.assertEqual(self.store.bump_extract_attempts([eid]), 1)
        self.assertEqual(self.store.bump_extract_attempts([eid]), 2)
        self.store.park_extract_failed([eid], time.time())
        row = dict(self.store._conn.execute(
            "SELECT extract_attempts, extract_status, extracted_at "
            "FROM events WHERE id = ?", (eid,)).fetchone())
        self.assertEqual(row["extract_attempts"], 2)
        self.assertEqual(row["extract_status"], "failed")
        self.assertIsNotNone(row["extracted_at"])
        # Parked ⇒ no longer unextracted.
        self.assertEqual(
            self.store.unextracted_events(modality=Modality.AUDIO.value), [])


class ExtractRetryCapTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ex = Extractor(store=self.store)
        # Settled: event older than consolidation gap (default 8s).
        self.eid = self.store.insert(
            _audio("I'll send Marc the pricing follow-up by Friday",
                   time.time() - 60.0))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _run(self, side_effect=None, return_value=None):
        kw = {}
        if side_effect is not None:
            kw["side_effect"] = side_effect
        else:
            kw["return_value"] = return_value or {
                "tasks": [], "commitments": [], "claims": [],
                "entities": [], "relations": []}
        with patch.object(self.ex, "_extract_text", **kw), \
             patch("app.services.intent.enabled", return_value=False), \
             patch("app.services.utterance_router.route_enabled",
                   return_value=False), \
             patch("app.services.extractor._index_fact", lambda *a, **k: None):
            return self.ex.run_once()

    def _meta(self):
        return dict(self.store._conn.execute(
            "SELECT extract_attempts, extract_status, extracted_at "
            "FROM events WHERE id = ?", (self.eid,)).fetchone())

    def test_retries_under_cap_leave_unmarked(self):
        for i in range(EXTRACT_MAX_ATTEMPTS - 1):
            res = self._run(side_effect=RuntimeError("llm down"))
            self.assertEqual(res["failed"], 0)
            self.assertEqual(res["events_marked"], 0)
            self.assertEqual(self._meta()["extract_attempts"], i + 1)
            self.assertIsNone(self._meta()["extracted_at"])
        # Still eligible for retry.
        self.assertEqual(
            len(self.store.unextracted_events(modality=Modality.AUDIO.value)), 1)

    def test_third_failure_parks_failed(self):
        for _ in range(EXTRACT_MAX_ATTEMPTS - 1):
            self._run(side_effect=RuntimeError("llm down"))
        res = self._run(side_effect=RuntimeError("llm down"))
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["events_marked"], 1)
        meta = self._meta()
        self.assertEqual(meta["extract_attempts"], EXTRACT_MAX_ATTEMPTS)
        self.assertEqual(meta["extract_status"], "failed")
        self.assertIsNotNone(meta["extracted_at"])
        # No longer in the extract queue — nudge/consolidate can't spin.
        self.assertEqual(
            self.store.unextracted_events(modality=Modality.AUDIO.value), [])
        # A further pass is a no-op for this turn.
        res2 = self._run(side_effect=RuntimeError("llm down"))
        self.assertEqual(res2["turns"], 0)
        self.assertEqual(res2["failed"], 0)
        self.assertEqual(self._meta()["extract_attempts"], EXTRACT_MAX_ATTEMPTS)

    def test_success_after_failures_marks_ok(self):
        self._run(side_effect=RuntimeError("transient"))
        res = self._run(return_value={
            "tasks": [], "commitments": [], "claims": [],
            "entities": [], "relations": []})
        self.assertEqual(res["failed"], 0)
        self.assertEqual(res["events_marked"], 1)
        meta = self._meta()
        self.assertEqual(meta["extract_attempts"], 1)
        self.assertEqual(meta["extract_status"], "ok")
        self.assertIsNotNone(meta["extracted_at"])


if __name__ == "__main__":
    unittest.main()
