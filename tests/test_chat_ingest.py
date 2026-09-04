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
            eid = chat_ingest.ingest("Hugh and I decided to demo Sparrow Friday")
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

    def _run(self, text, facts, queued, *, event_id=None, event_source="chat.user"):
        class _FakeWorker:
            def enqueue(self, kind, payload=None, *, unique=False):
                queued.append(kind)
                return 1

        import app.services.worker as worker_mod
        import app.storage as storage_mod
        if event_id is not None:
            from app.events import Event, Modality
            ev = Event(time=1.0, modality=Modality.TEXT, raw=text,
                       summary=f"[chat] {text[:40]}", source=event_source)
            # Force the id the job will look up (insert assigns next id).
            event_id = self.store.insert(ev)
        with patch.object(storage_mod, "get_store", return_value=self.store), \
             patch.object(worker_mod, "worker", _FakeWorker()), \
             patch("app.services.extractor.extractor._extract_text",
                   return_value=facts), \
             patch("app.services.extractor._index_fact", lambda *a, **k: None), \
             patch("app.services.fact_gate._similar_active", return_value=[]):
            chat_ingest.run_job({"event_id": event_id, "text": text})
        return event_id

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

    def test_passes_chat_user_event_source(self):
        """Regression: without chat.user, People v2 treats chat like a
        document and refuses to mint new people."""
        queued: list[str] = []
        seen = {}

        def _capture(store, facts, anchor, chunk, now, index=True, *,
                     event_source="document", window=""):
            seen["event_source"] = event_source
            return 0

        import app.services.worker as worker_mod
        import app.storage as storage_mod
        from app.events import Event, Modality
        ev = Event(time=1.0, modality=Modality.TEXT,
                   raw="meeting with Andy Karos",
                   summary="[chat] meeting", source="chat.user")
        eid = self.store.insert(ev)
        with patch.object(storage_mod, "get_store", return_value=self.store), \
             patch.object(worker_mod, "worker",
                          type("W", (), {"enqueue": lambda *a, **k: 1})()), \
             patch("app.services.extractor.extractor._extract_text",
                   return_value={"tasks": [], "commitments": [], "claims": [],
                                 "entities": [], "relations": []}), \
             patch("app.services.documents._persist_facts", side_effect=_capture):
            chat_ingest.run_job({"event_id": eid,
                                 "text": "meeting with Andy Karos"})
        self.assertEqual(seen.get("event_source"), "chat.user")

    def test_named_counterparty_mints_person_from_chat(self):
        """Chat commitment with to_person=Andy Karos should mint a people row
        under direct_message policy (the bug that kept Andy off You → People)."""
        queued: list[str] = []
        text = "I have a meeting with Andy Karos today at 8:30 pm about Sparrow"
        self._run(
            text,
            {"tasks": [], "commitments": [{
                "text": "meeting with Andy Karos today at 8:30 pm about Sparrow",
                "from_person": "me",
                "to_person": "Andy Karos",
                "source_span": "meeting with Andy Karos today at 8:30 pm",
                "confidence": 0.95,
                "due": "2026-08-26T20:30:00",
                "assertion": "asserted",
            }], "claims": [], "entities": [], "relations": []},
            queued,
            event_id=1,
            event_source="chat.user",
        )
        names = {p["name"].lower() for p in self.store.all_people()}
        self.assertTrue(
            any("andy" in n for n in names),
            f"expected Andy Karos in people, got {names}")

    def test_chat_text_harvest_mints_when_extractor_omits_party(self):
        """Even if the LLM returns no from/to, multi-word names in the chat
        turn itself must still land on People."""
        queued: list[str] = []
        text = "Remember I have a meeting with Andy Karos today at 8:30 pm about Sparrow."
        self._run(
            text,
            {"tasks": [], "commitments": [], "claims": [],
             "entities": [], "relations": []},
            queued,
            event_id=1,
            event_source="chat.user",
        )
        names = {p["name"].lower() for p in self.store.all_people()}
        self.assertTrue(
            any("andy" in n for n in names),
            f"expected Andy Karos harvested from chat text, got {names}")


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
