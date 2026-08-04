"""Meeting Layer P2 — notepad jots, extract anchors, ranking adjacency."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

NOW = 1_700_000_000.0


class MeetingNotesIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_mn_"))
        from app.storage import Store
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.env = patch.dict(os.environ, {"QUILL_MEETING_NOTES": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.store.close()

    def test_ingest_short_jot_as_meeting_note(self):
        from app.services import meeting_notes as mn
        eid = mn.ingest("pricing — pushback", store=self.store, now=NOW)
        self.assertIsNotNone(eid)
        rows = self.store.recent_events(source_substr="meeting.note", limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "meeting.note")
        self.assertEqual(rows[0]["raw"], "pricing — pushback")
        self.assertEqual(rows[0]["meta"].get("section"), "notepad")

    def test_jots_near_window(self):
        from app.services import meeting_notes as mn
        mn.ingest("early", store=self.store, now=NOW - 200)
        mn.ingest("in window", store=self.store, now=NOW + 10)
        mn.ingest("late", store=self.store, now=NOW + 200)
        near = mn.jot_texts_near(self.store, NOW, window_s=90)
        self.assertEqual(near, ["in window"])

    def test_format_anchor_block_warns_span_source(self):
        from app.services import meeting_notes as mn
        block = mn.format_anchor_block(["pricing — pushback"])
        self.assertIn("USER'S LIVE NOTE AT THIS MOMENT", block)
        self.assertIn("NOT a source of quotes", block)
        self.assertIn("pricing — pushback", block)

    def test_note_adjacent_score(self):
        from app.services import meeting_notes as mn
        times = [NOW - 30, NOW + 400]
        self.assertEqual(mn.note_adjacent_score(NOW, times), 1.0)
        self.assertEqual(mn.note_adjacent_score(NOW + 200, times), 0.0)

    def test_events_in_window_store(self):
        from app.services import meeting_notes as mn
        mn.ingest("note a", store=self.store, now=NOW)
        mn.ingest("note b", store=self.store, now=NOW + 30)
        rows = self.store.events_in_window(
            NOW - 1, NOW + 60, source=mn.SOURCE, modality="text")
        self.assertEqual([r["raw"] for r in rows], ["note a", "note b"])


class ExtractAnchorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_ex_"))
        from app.storage import Store
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def tearDown(self):
        self.store.close()

    def test_extract_text_includes_co_timed_jot(self):
        from app.services import meeting_notes as mn
        from app.services.consolidation import Turn
        from app.services.extractor import Extractor

        mn.ingest("pricing — pushback", store=self.store, now=NOW + 5)
        turn = Turn(
            start=NOW, end=NOW + 20, speaker="user",
            text="I'll send the revised pricing by Thursday",
            event_ids=[1], n_utterances=1,
        )
        captured = {}

        class FakeRouter:
            def complete_json(self, task, *, system, messages, **kwargs):
                captured["system"] = system
                captured["content"] = messages[0]["content"]
                return {
                    "tasks": [], "commitments": [], "claims": [],
                    "questions": [], "entities": [], "relations": [],
                }

        ex = Extractor(store=self.store)
        with patch("app.services.model_router.router", FakeRouter()):
            ex._extract_text(turn)
        self.assertIn("USER'S LIVE NOTE AT THIS MOMENT", captured["content"])
        self.assertIn("pricing — pushback", captured["content"])
        self.assertIn("I'll send the revised pricing", captured["content"])
        self.assertIn("verbatim substring of the spoken transcript",
                      captured["system"])


class RankingNoteAdjacentTests(unittest.TestCase):
    def test_note_adjacent_lifts_commitment_vs_baseline(self):
        from app.services.ranking.scorer import GravityScorer
        from app.services.ranking.types import PipelineContext

        def cand(note_adj: float) -> dict:
            return {
                "id": "fact:1",
                "kind": "commitment",
                "label": "send pricing",
                "confidence": 0.85,
                "_age": 0.1,
                "ts": NOW,
                "pinned": False,
                "_feat_pros": 0.45,
                "_feat_rel": 0.0,
                "_feat_fut": 0.0,
                "_feat_unres": 0.7,
                "_feat_cent": 0.1,
                "_feat_sem": 0.35,
                "_feat_rep": 0.0,
                "_feat_temp": 0.9,
                "_feat_act": 0.0,
                "_feat_aging": 0.0,
                "_feat_note_adjacent": note_adj,
            }

        scorer = GravityScorer()
        ctx = PipelineContext(now=NOW)
        base = cand(0.0)
        boosted = cand(1.0)
        scorer.score([base], ctx)
        scorer.score([boosted], ctx)
        self.assertGreater(boosted["gravity"], base["gravity"])
        keys = {c.key for c in boosted["_breakdown"].components}
        self.assertIn("note_adjacent", keys)


if __name__ == "__main__":
    unittest.main()
