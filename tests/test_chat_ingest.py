"""Typed chat → memory: the pre-filter, the event write + job queue, the
extraction job (through the hygiene gate), and the memory_version change token
the live constellation polls."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import chat_ingest
from app.storage import Store


class IngestableTests(unittest.TestCase):
    def test_substantive_statement_passes(self):
        self.assertEqual(
            chat_ingest.ingestable("I met Sarah Chen, she runs platform at Foundry"),
            "I met Sarah Chen, she runs platform at Foundry")

    def test_short_approvals_skipped(self):
        for msg in ("yes", "go", "approve", "cancel", "ok", ""):
            self.assertIsNone(chat_ingest.ingestable(msg))

    def test_slash_prefix_stripped_then_judged(self):
        self.assertEqual(
            chat_ingest.ingestable("/plan draft the pricing follow-up email"),
            "draft the pricing follow-up email")
        self.assertIsNone(chat_ingest.ingestable("/plan"))

    def test_questions_are_not_skipped(self):
        # a question can carry a fact — the extractor decides, not the filter
        self.assertIsNotNone(
            chat_ingest.ingestable("can you note the demo moved to Friday?"))

    def test_whitespace_trimmed(self):
        self.assertIsNone(chat_ingest.ingestable("   hi   "))


class IngestTests(unittest.TestCase):
    def test_disabled_env_skips(self):
        with patch.object(chat_ingest, "enabled", return_value=False):
            self.assertIsNone(chat_ingest.ingest("a perfectly good statement"))

    def test_stores_text_event_and_enqueues_job(self):
        stored, queued = {}, {}

        class _FakeStore:
            def insert(self, ev):
                stored["ev"] = ev
                return 41

        class _FakeWorker:
            def enqueue(self, kind, payload=None, *, unique=False):
                queued.update(kind=kind, payload=payload)
                return 1

        import app.services.worker as worker_mod
        import app.storage as storage_mod
        with patch.object(storage_mod, "get_store", return_value=_FakeStore()), \
             patch.object(worker_mod, "worker", _FakeWorker()), \
             patch("app.services.attachments._index_event", lambda *a: None):
            eid = chat_ingest.ingest("Hugh and I decided to demo Mnemos Friday")
        self.assertEqual(eid, 41)
        ev = stored["ev"]
        self.assertEqual(ev.modality.value, "text")
        self.assertEqual(ev.source, "chat.user")
        self.assertIn("Hugh", ev.raw)
        self.assertEqual(queued["kind"], "chat_ingest")
        self.assertEqual(queued["payload"]["event_id"], 41)

    def test_failure_never_raises(self):
        import app.storage as storage_mod
        with patch.object(storage_mod, "get_store",
                          side_effect=RuntimeError("db locked")):
            self.assertIsNone(chat_ingest.ingest("a perfectly good statement"))


class RunJobTests(unittest.TestCase):
    """The job persists extracted facts into a REAL (temp) store through
    _persist_facts, and chains a graph rebuild only when facts landed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def _run(self, text, facts, queued):
        class _FakeWorker:
            def enqueue(self, kind, payload=None, *, unique=False):
                queued.append(kind)
                return 1

        import app.services.worker as worker_mod
        import app.storage as storage_mod
        with patch.object(storage_mod, "get_store", return_value=self.store), \
             patch.object(worker_mod, "worker", _FakeWorker()), \
             patch("app.services.extractor.extractor._extract_text",
                   return_value=facts), \
             patch("app.services.extractor._index_fact", lambda *a, **k: None), \
             patch("app.services.fact_gate._similar_active", return_value=[]):
            chat_ingest.run_job({"event_id": None, "text": text})

    def test_facts_persist_and_graph_chains(self):
        queued: list[str] = []
        self._run(
            "I told Marc we ship the pilot next week",
            {"tasks": [], "commitments": [{
                "text": "ship the pilot next week",
                "source_span": "we ship the pilot next week",
                "confidence": 0.9}], "claims": [], "entities": [],
             "relations": []},
            queued)
        rows = self.store.facts_since(0.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "commitment")
        self.assertIn("graph", queued)

    def test_no_facts_no_graph_rebuild(self):
        queued: list[str] = []
        self._run("what a lovely day we are having",
                  {"tasks": [], "commitments": [], "claims": [],
                   "entities": [], "relations": []}, queued)
        self.assertEqual(self.store.facts_since(0.0), [])
        self.assertNotIn("graph", queued)

    def test_empty_payload_is_noop(self):
        chat_ingest.run_job({})
        chat_ingest.run_job(None)


class MemoryVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_version_changes_on_fact_add_and_touch(self):
        v0 = self.store.memory_version()
        fid = self.store.add_task("call the venue", extracted_at=100.0)
        v1 = self.store.memory_version()
        self.assertNotEqual(v0, v1)
        self.store.touch_fact(fid, 200.0, 0.9)
        v2 = self.store.memory_version()
        self.assertNotEqual(v1, v2)
        self.assertEqual(v2, self.store.memory_version())  # stable when idle


if __name__ == "__main__":
    unittest.main()
