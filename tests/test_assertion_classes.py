"""Assertion classes (plan task 1.3).

`quoted` and `hypothetical` assertions are never auto-accepted: gate_fact
routes them to a `review` verdict, and the extractor stamps the candidate
`review` without ever inserting a fact. `stated_by_user` / `stated_by_other` /
`inferred` are unaffected and follow the normal insert/dedup/drop path.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import fact_gate
from app.services.consolidation import Turn
from app.services.extractor import Extractor
from app.services.fact_gate import gate_fact
from app.storage import Store


class GateAssertionTests(unittest.TestCase):
    def setUp(self):
        patches = [patch.object(fact_gate, "_telemetry", lambda *a, **k: None),
                   patch.object(fact_gate, "_similar_active", return_value=[])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_quoted_routes_to_review(self):
        v = gate_fact("commitment", "she'll send the deck", 0.9,
                      "she told me she'd send the deck",
                      "she told me she'd send the deck",
                      assertion="quoted")
        self.assertEqual(v.action, "review")
        self.assertIn("quoted", v.reason)

    def test_hypothetical_routes_to_review(self):
        v = gate_fact("task", "book the venue", 0.9,
                      "if we go ahead I'd book the venue",
                      "if we go ahead I'd book the venue",
                      assertion="hypothetical")
        self.assertEqual(v.action, "review")
        self.assertIn("hypothetical", v.reason)

    def test_stated_by_user_inserts_normally(self):
        v = gate_fact("task", "book the venue", 0.9,
                      "we need to book the venue",
                      "we need to book the venue",
                      assertion="stated_by_user")
        self.assertEqual(v.action, "insert")

    def test_stated_by_other_inserts_normally(self):
        v = gate_fact("claim", "the demo is Monday", 0.9,
                      "the demo is Monday",
                      "the demo is Monday",
                      assertion="stated_by_other")
        self.assertEqual(v.action, "insert")

    def test_inferred_inserts_normally(self):
        v = gate_fact("claim", "budget is tight", 0.9,
                      "budget is tight", "budget is tight",
                      assertion="inferred")
        self.assertEqual(v.action, "insert")

    def test_no_assertion_still_inserts(self):
        # Legacy candidates (no assertion tag) must be unaffected.
        v = gate_fact("claim", "the demo is Monday", 0.9,
                      "the demo is Monday", "the demo is Monday")
        self.assertEqual(v.action, "insert")

    def test_quoted_never_auto_inserts_even_over_confidence(self):
        v = gate_fact("commitment", "he promised to call back", 0.99,
                      "he said he'd call me back",
                      "he said he'd call me back", assertion="quoted")
        self.assertNotEqual(v.action, "insert")
        self.assertEqual(v.action, "review")


def _turn(text: str, event_id: int = 42) -> Turn:
    return Turn(start=1000.0, end=1001.0, speaker="Hugh", text=text,
                event_ids=[event_id], n_utterances=1)


class ExtractorAssertionEndToEndTests(unittest.TestCase):
    """Adversarial fixtures: quoted/hypothetical speech must never become a
    fact through the full _persist path, and the candidate must be stamped
    'review' — not silently dropped or left pending."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ex = Extractor(store=self.store)
        self.now = time.time()

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _persist(self, facts, turn):
        from app.services.people_pipeline import ResolveResult
        with patch.object(self.ex, "_resolve_person_ref",
                          return_value=ResolveResult(None, "leave_open")), \
             patch.object(self.ex, "_persist_entities", return_value=(0, 0)), \
             patch.object(self.ex, "_record_faithfulness", return_value=None), \
             patch("app.services.extractor._index_fact", lambda *a, **k: None), \
             patch("app.services.task_offer.offer_task", lambda *a, **k: None), \
             patch("app.services.fact_gate._similar_active", return_value=[]):
            return self.ex._persist(turn, facts, self.now)

    def test_quoted_commitment_never_materializes(self):
        text = "Marc told me he'd send the pricing follow-up by Friday"
        turn = _turn(text)
        facts = {
            "tasks": [], "claims": [], "entities": [], "relations": [],
            "commitments": [{
                "text": "send the pricing follow-up",
                "from_person": "Marc", "to_person": "me",
                "due": "2026-08-08", "confidence": 0.9,
                "source_span": "Marc told me he'd send the pricing follow-up",
                "assertion": "quoted",
            }],
        }
        n = self._persist(facts, turn)
        self.assertEqual(n, 0)
        self.assertEqual(self.store.facts_since(0.0), [])
        from app.services.extractor import turn_hash
        rows = self.store.list_fact_candidates(turn_hash=turn_hash(turn))
        commit_rows = [r for r in rows if r["kind"] == "commitment"]
        self.assertEqual(len(commit_rows), 1)
        self.assertEqual(commit_rows[0]["status"], "review")

    def test_hypothetical_task_never_materializes(self):
        text = "if we go ahead with the launch I'd book the venue"
        turn = _turn(text)
        facts = {
            "commitments": [], "claims": [], "entities": [], "relations": [],
            "tasks": [{
                "text": "book the venue", "owner": "me", "due": "",
                "confidence": 0.9,
                "source_span": "if we go ahead with the launch I'd book the venue",
                "assertion": "hypothetical",
            }],
        }
        n = self._persist(facts, turn)
        self.assertEqual(n, 0)
        self.assertEqual(self.store.facts_since(0.0), [])
        from app.services.extractor import turn_hash
        rows = self.store.list_fact_candidates(turn_hash=turn_hash(turn))
        task_rows = [r for r in rows if r["kind"] == "task"]
        self.assertEqual(len(task_rows), 1)
        self.assertEqual(task_rows[0]["status"], "review")

    def test_stated_by_user_task_materializes(self):
        text = "we need to book the venue"
        turn = _turn(text)
        facts = {
            "commitments": [], "claims": [], "entities": [], "relations": [],
            "tasks": [{
                "text": "Book the venue", "owner": "me", "due": "",
                "confidence": 0.9, "source_span": "we need to book the venue",
                "assertion": "stated_by_user",
            }],
        }
        n = self._persist(facts, turn)
        self.assertEqual(n, 1)
        self.assertEqual(len(self.store.facts_since(0.0)), 1)


if __name__ == "__main__":
    unittest.main()
