"""Research answers → memory writeback (testing-first path)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services import research_ingest
from app.services import source_policy as sp
from app.storage import Store


class IngestableTests(unittest.TestCase):
    def test_browser_success_passes(self):
        text = (
            "Andy Karos is founder and CEO of Boost Run, an AI infrastructure "
            "company based in Northbrook, IL."
        )
        self.assertEqual(
            research_ingest.ingestable(text, status="success"),
            text)

    def test_memory_only_status_skipped(self):
        text = (
            "From memory, you have a meeting with Andy Karos today at 8:30 PM "
            "about Sparrow."
        )
        self.assertIsNone(
            research_ingest.ingestable(text, status="answered_no_browser"))

    def test_memory_only_route_skipped(self):
        text = (
            "From memory, you have a meeting with Andy Karos today at 8:30 PM "
            "about Sparrow."
        )
        self.assertIsNone(research_ingest.ingestable(
            text, status="success",
            route={"intent": "memory_question", "surface": "none",
                   "requires_browser": False}))

    def test_short_and_stub_skipped(self):
        self.assertIsNone(research_ingest.ingestable("short", status="success"))
        self.assertIsNone(research_ingest.ingestable(
            "(no answer — error)", status="error"))
        self.assertIsNone(research_ingest.ingestable(
            "Refused: blocked by policy", status="blocked"))

    def test_disabled(self):
        text = (
            "Andy Karos is founder and CEO of Boost Run, an AI infrastructure "
            "company based in Northbrook, IL."
        )
        with patch.object(research_ingest, "enabled", return_value=False):
            self.assertIsNone(
                research_ingest.ingestable(text, status="success"))


class SourcePolicyTests(unittest.TestCase):
    def test_chat_research_classifies_as_research_answer(self):
        self.assertEqual(
            sp.classify_source(event_source="chat.research"),
            "research_answer")
        pol = sp.policy_for("research_answer")
        self.assertTrue(pol.create_claims)
        self.assertTrue(pol.create_person_candidates)
        self.assertFalse(pol.create_commitments)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_research_ingest_"))
        self.store = Store(self.tmp / "t.db")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_ingest_writes_event_and_runs_sync(self):
        text = (
            "Andy Karos is founder and CEO of Boost Run, an AI infrastructure "
            "company based in Northbrook, IL."
        )
        with patch("app.storage.get_store", return_value=self.store), \
             patch.object(research_ingest, "sync_mode", return_value=True), \
             patch.object(research_ingest, "run_job") as job:
            eid = research_ingest.ingest(text, status="success",
                                        route={"intent": "research_person",
                                               "requires_browser": True},
                                        question="Who is Andy Karos?")
        self.assertIsNotNone(eid)
        ev = self.store.get_event(int(eid))
        self.assertEqual(ev["source"], "chat.research")
        self.assertIn("Andy Karos", ev["raw"])
        job.assert_called_once()
        payload = job.call_args[0][0]
        self.assertEqual(payload["event_id"], eid)
        self.assertEqual(payload["text"], text)

    def test_ingest_enqueues_when_async(self):
        text = (
            "Andy Karos is founder and CEO of Boost Run, an AI infrastructure "
            "company based in Northbrook, IL."
        )
        fake_worker = MagicMock()
        with patch("app.storage.get_store", return_value=self.store), \
             patch.object(research_ingest, "sync_mode", return_value=False), \
             patch("app.services.worker.worker", fake_worker):
            eid = research_ingest.ingest(text, status="success")
        self.assertIsNotNone(eid)
        fake_worker.enqueue.assert_called_once()
        args, kwargs = fake_worker.enqueue.call_args
        self.assertEqual(args[0], "research_ingest")
        self.assertEqual(kwargs["payload"]["event_id"], eid)

    def test_answered_no_browser_does_not_write(self):
        text = (
            "From memory, you have a meeting with Andy Karos today at 8:30 PM "
            "about Sparrow."
        )
        with patch("app.storage.get_store", return_value=self.store) as gs:
            self.assertIsNone(
                research_ingest.ingest(text, status="answered_no_browser"))
            gs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
