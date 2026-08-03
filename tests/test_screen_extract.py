"""Screen → memory: batching, marking, persistence through the hygiene gate,
and the graph chain. The LLM is mocked throughout — these prove the plumbing,
not the model."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import screen_extract
from app.storage import Store

NOW = 1_000_000_000.0


def _screen_event(text, ts=NOW, source="desktop.screen"):
    return Event(time=ts, modality=Modality.VISION, raw=text,
                 summary=f"[App] {text[:40]}", source=source)


class ScreenExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")
        import app.storage as storage_mod
        p = patch.object(storage_mod, "get_store", return_value=self.store)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, facts=None, queued=None):
        class _FakeWorker:
            def enqueue(self, kind, payload=None, *, unique=False):
                (queued if queued is not None else []).append(kind)
                return 1

        import app.services.worker as worker_mod
        with patch.object(screen_extract, "_extract",
                          return_value=facts or {}), \
             patch.object(worker_mod, "worker", _FakeWorker()), \
             patch("app.services.extractor._index_fact", lambda *a, **k: None), \
             patch("app.services.fact_gate._similar_active", return_value=[]):
            return screen_extract.run_once()

    def test_disabled_is_noop(self):
        with patch.object(screen_extract, "enabled", return_value=False):
            self.assertEqual(screen_extract.run_once(), {"enabled": False})

    def test_no_screen_events_is_noop(self):
        self.store.insert(_screen_event("x" * 200, source="webcam"))
        self.assertEqual(self._run(), {"events": 0, "facts": 0})

    def test_thin_frames_marked_but_not_mined(self):
        eid = self.store.insert(_screen_event("tiny"))
        res = self._run()
        self.assertEqual(res["events"], 0)
        # marked extracted so it never blocks the queue again
        self.assertEqual(self.store.unextracted_events(modality="vision"), [])
        self.assertIsNotNone(eid)

    def test_facts_persist_with_screen_provenance_and_graph_chain(self):
        text = ("Email draft to Marchetti: I will send the revised pricing "
                "by Thursday. Foundry Capital leads the round.")
        eid = self.store.insert(_screen_event(text))
        queued: list = []
        res = self._run(facts={
            "tasks": [], "commitments": [{
                "text": "send Marchetti the revised pricing by Thursday",
                "source_span": "I will send the revised pricing by Thursday",
                "confidence": 0.85}],
            "claims": [{
                "text": "Foundry Capital leads the round",
                "source_span": "Foundry Capital leads the round.",
                "confidence": 0.8}],
            "entities": [], "relations": []}, queued=queued)
        self.assertEqual(res["facts"], 2)
        rows = self.store.facts_since(0.0)
        self.assertEqual(len(rows), 2)
        for f in rows:
            self.assertEqual(f["source_event_id"], eid)   # anchored to the frame
        self.assertIn("graph", queued)
        # frames marked: a second run has nothing to do
        self.assertEqual(self._run()["events"], 0)

    def test_unfaithful_span_dropped_by_gate(self):
        self.store.insert(_screen_event("a long enough screen text about "
                                        "nothing in particular at all here"))
        res = self._run(facts={"tasks": [], "commitments": [], "claims": [{
            "text": "the user loves skydiving",
            "source_span": "words never on the screen",
            "confidence": 0.9}], "entities": [], "relations": []})
        self.assertEqual(res["facts"], 0)
        self.assertEqual(self.store.facts_since(0.0), [])

    def test_extract_failure_leaves_frames_for_retry(self):
        self.store.insert(_screen_event("a long enough screen text to mine "
                                        "with several interesting words"))
        import app.services.worker as worker_mod

        class _FakeWorker:
            def enqueue(self, *a, **k):
                return 1

        with patch.object(screen_extract, "_extract",
                          side_effect=RuntimeError("model gone")), \
             patch.object(worker_mod, "worker", _FakeWorker()):
            res = screen_extract.run_once()
        self.assertIn("error", res)
        # NOT marked — retried on the next activity chain
        self.assertEqual(
            len(self.store.unextracted_events(modality="vision")), 1)


if __name__ == "__main__":
    unittest.main()
