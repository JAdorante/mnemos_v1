"""fact_candidates table + writer + materialization (plan tasks 1.1/1.2).

Every LLM extract row lands as a candidate with prompt_version before the
hygiene gate runs. `add_fact_candidate` dedupes on turn_hash+kind+payload_json,
and `Extractor._persist` routes task/commitment/claim materialization through
that candidate row: gate once, stamp accepted/dropped/deduped/review, and
never re-materialize an already-gated candidate — so replaying the same turn
leaves fact counts unchanged.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.consolidation import Turn
from app.services.extractor import (
    EXTRACT_PROMPT_VERSION,
    EXTRACT_SCHEMA_VERSION,
    EXTRACTOR_MODEL,
    Extractor,
    turn_hash,
)
from app.storage import Store


FACTS = {
    "tasks": [{
        "text": "Book the venue",
        "owner": "me",
        "due": "2026-08-10",
        "confidence": 0.9,
        "source_span": "we need to book the venue",
        "assertion": "stated_by_user",
    }],
    "commitments": [{
        "text": "send Marc the pricing follow-up",
        "from_person": "me",
        "to_person": "Marc",
        "due": "2026-08-08",
        "confidence": 0.85,
        "source_span": "I'll send Marc the pricing follow-up",
        "assertion": "stated_by_user",
    }],
    "claims": [{
        "text": "pilot is $49/seat",
        "confidence": 0.8,
        "source_span": "the pilot is $49/seat",
        "assertion": "stated_by_user",
    }],
    "entities": [{
        "name": "Acme",
        "kind": "org",
        "confidence": 0.7,
        "source_span": "Acme",
    }],
    "relations": [{
        "subject": "Marc",
        "subject_kind": "person",
        "predicate": "works_at",
        "object": "Acme",
        "object_kind": "entity",
        "confidence": 0.6,
        "source_span": "Marc is at Acme",
    }],
}


def _turn(text: str = "I'll send Marc the pricing follow-up. "
                      "We need to book the venue. The pilot is $49/seat. "
                      "Marc is at Acme.") -> Turn:
    return Turn(start=1000.0, end=1001.0, speaker="Hugh", text=text,
                event_ids=[42], n_utterances=1)


class TurnHashTests(unittest.TestCase):
    def test_stable_and_speaker_sensitive(self):
        t = _turn()
        h1 = turn_hash(t)
        h2 = turn_hash(t)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)
        other = Turn(start=t.start, end=t.end, speaker="Other", text=t.text,
                     event_ids=list(t.event_ids), n_utterances=1)
        self.assertNotEqual(turn_hash(other), h1)


class FactCandidateStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_table_exists_with_required_columns(self):
        cols = {r["name"] for r in self.store._conn.execute(
            "PRAGMA table_info(fact_candidates)").fetchall()}
        for col in ("turn_hash", "kind", "payload_json", "source_span",
                    "speaker", "assertion", "confidence", "model",
                    "prompt_version", "schema_version", "status",
                    "verdict_reason", "correlation_id"):
            self.assertIn(col, cols)

    def test_add_and_list(self):
        cid = self.store.add_fact_candidate(
            turn_hash="abc", kind="claim",
            payload={"text": "x", "confidence": 0.5},
            source_span="x", speaker="Hugh",
            prompt_version="extract-v1", schema_version="facts-schema-v1",
            model="claude-haiku-4-5", confidence=0.5,
        )
        rows = self.store.list_fact_candidates(turn_hash="abc")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], cid)
        self.assertEqual(rows[0]["prompt_version"], "extract-v1")
        self.assertEqual(rows[0]["payload"]["text"], "x")
        self.assertEqual(rows[0]["status"], "pending")

    def test_add_fact_candidate_dedupes_on_turn_kind_payload(self):
        cid1 = self.store.add_fact_candidate(
            turn_hash="abc", kind="task", payload={"text": "Book the venue"},
            prompt_version="extract-v1", schema_version="facts-schema-v1")
        cid2 = self.store.add_fact_candidate(
            turn_hash="abc", kind="task", payload={"text": "Book the venue"},
            prompt_version="extract-v1", schema_version="facts-schema-v1")
        self.assertEqual(cid1, cid2)
        rows = self.store.list_fact_candidates(turn_hash="abc")
        self.assertEqual(len(rows), 1)

    def test_add_fact_candidate_distinct_payload_is_new_row(self):
        cid1 = self.store.add_fact_candidate(
            turn_hash="abc", kind="task", payload={"text": "Book the venue"},
            prompt_version="extract-v1", schema_version="facts-schema-v1")
        cid2 = self.store.add_fact_candidate(
            turn_hash="abc", kind="task", payload={"text": "Book a venue"},
            prompt_version="extract-v1", schema_version="facts-schema-v1")
        self.assertNotEqual(cid1, cid2)

    def test_find_fact_candidate_exact_match(self):
        cid = self.store.add_fact_candidate(
            turn_hash="xyz", kind="claim", payload={"text": "pilot is $49"},
            prompt_version="extract-v1", schema_version="facts-schema-v1")
        found = self.store.find_fact_candidate(
            "xyz", "claim", {"text": "pilot is $49"})
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], cid)
        self.assertIsNone(
            self.store.find_fact_candidate("xyz", "claim", {"text": "other"}))
        self.assertIsNone(
            self.store.find_fact_candidate("nope", "claim", {"text": "pilot is $49"}))


class FactCandidateWriterTests(unittest.TestCase):
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

    def _persist(self, facts=None, turn=None):
        turn = turn or _turn()
        facts = facts if facts is not None else FACTS
        # Keep fact writes quiet: no person resolve / index / offers.
        from app.services.people_pipeline import ResolveResult
        with patch.object(self.ex, "_resolve_person_ref",
                          return_value=ResolveResult(None, "leave_open")), \
             patch.object(self.ex, "_persist_entities", return_value=(0, 0)), \
             patch.object(self.ex, "_record_faithfulness", return_value=None), \
             patch("app.services.extractor._index_fact", lambda *a, **k: None), \
             patch("app.services.task_offer.offer_task", lambda *a, **k: None), \
             patch("app.services.fact_gate._similar_active", return_value=[]), \
             patch("app.services.self_profile.is_first_person",
                   return_value=False):
            return self.ex._persist(turn, facts, self.now), turn

    def test_every_llm_row_lands_with_prompt_version(self):
        _, turn = self._persist()
        rows = self.store.list_fact_candidates(turn_hash=turn_hash(turn))
        kinds = sorted(r["kind"] for r in rows)
        self.assertEqual(
            kinds, ["claim", "commitment", "entity", "relation", "task"])
        for r in rows:
            self.assertEqual(r["prompt_version"], EXTRACT_PROMPT_VERSION)
            self.assertEqual(r["schema_version"], EXTRACT_SCHEMA_VERSION)
            self.assertEqual(r["model"], EXTRACTOR_MODEL)
            self.assertEqual(r["speaker"], "Hugh")
            self.assertEqual(r["source_event_id"], 42)
            self.assertTrue(r["payload_json"])
        # task/commitment/claim materialize -> accepted; entity/relation are
        # written for provenance but not gated/stamped by this plan.
        by_kind = {r["kind"]: r for r in rows}
        for kind in ("task", "commitment", "claim"):
            self.assertEqual(by_kind[kind]["status"], "accepted")
        for kind in ("entity", "relation"):
            self.assertEqual(by_kind[kind]["status"], "pending")

    def test_dropped_by_gate_still_leaves_candidate(self):
        # Empty span ⇒ gate drop for claims, but candidate must still exist.
        facts = {
            "tasks": [], "commitments": [],
            "claims": [{
                "text": "something notable",
                "confidence": 0.9,
                "source_span": "",  # empty → drop
                "assertion": "stated_by_user",
            }],
            "entities": [], "relations": [],
        }
        n, turn = self._persist(facts=facts)
        self.assertEqual(n, 0)  # no facts materialized
        rows = self.store.list_fact_candidates(turn_hash=turn_hash(turn))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "claim")
        self.assertEqual(rows[0]["prompt_version"], EXTRACT_PROMPT_VERSION)
        self.assertEqual(rows[0]["status"], "dropped")
        self.assertEqual(rows[0]["verdict_reason"], "empty source_span")
        self.assertEqual(self.store.facts_since(0.0), [])

    def test_write_helper_returns_ids(self):
        turn = _turn()
        ids = self.ex._write_fact_candidates(turn, FACTS, self.now)
        self.assertEqual(len(ids), 5)
        self.assertTrue(all(isinstance(i, int) for i in ids))

    def test_replay_same_turn_materializes_no_new_facts(self):
        n1, turn = self._persist()
        self.assertEqual(n1, 3)  # one task + one commitment + one claim
        facts_before = self.store.facts_since(0.0)
        self.assertEqual(len(facts_before), 3)

        n2, _ = self._persist(turn=turn)  # replay: identical turn + facts
        self.assertEqual(n2, 0)
        facts_after = self.store.facts_since(0.0)
        self.assertEqual(len(facts_after), 3)
        self.assertEqual({f["fact_id"] for f in facts_before},
                         {f["fact_id"] for f in facts_after})

        # Still exactly one candidate row per kind — dedupe, not a twin row.
        rows = self.store.list_fact_candidates(turn_hash=turn_hash(turn))
        kinds = sorted(r["kind"] for r in rows)
        self.assertEqual(
            kinds, ["claim", "commitment", "entity", "relation", "task"])

    def test_replay_preserves_dropped_status(self):
        facts = {
            "tasks": [], "commitments": [],
            "claims": [{
                "text": "something notable", "confidence": 0.9,
                "source_span": "", "assertion": "stated_by_user",
            }],
            "entities": [], "relations": [],
        }
        n1, turn = self._persist(facts=facts)
        n2, _ = self._persist(facts=facts, turn=turn)
        self.assertEqual((n1, n2), (0, 0))
        rows = self.store.list_fact_candidates(turn_hash=turn_hash(turn))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "dropped")


if __name__ == "__main__":
    unittest.main()
