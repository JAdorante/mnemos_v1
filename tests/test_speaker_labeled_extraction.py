"""Speaker-labeled extraction + relative ownership (plan task 2.1).

`_extract_text(turn)` renders `[speaker]: text`. `owner`/`from_person`='me'
maps to the enrolled user's self node ONLY when the labeled speaker is that
user; otherwise 'me' resolves to the labeled speaker (or None if unknown).
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.consolidation import (
    Turn,
    format_turn_transcript,
    speaker_label,
)
from app.services import self_profile
from app.services.extractor import Extractor
from app.storage import Store


class FormatTurnTranscriptTests(unittest.TestCase):
    def test_known_speaker(self):
        t = Turn(start=1, end=2, speaker="Hugh", text="I'll send the deck",
                 event_ids=[1], n_utterances=1)
        self.assertEqual(speaker_label(t), "Hugh")
        self.assertEqual(format_turn_transcript(t),
                         "[Hugh]: I'll send the deck")

    def test_unknown_speaker(self):
        t = Turn(start=1, end=2, speaker="", text="hello",
                 event_ids=[1], n_utterances=1)
        self.assertEqual(speaker_label(t), "unknown speaker")
        self.assertEqual(format_turn_transcript(t),
                         "[unknown speaker]: hello")

    def test_dict_turn(self):
        self.assertEqual(
            format_turn_transcript({"speaker": "Marc", "text": "I'll do it"}),
            "[Marc]: I'll do it")


class SpeakerIsEnrolledUserTests(unittest.TestCase):
    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)

    def test_matches_identity(self):
        with patch("app.services.identity.user_identity",
                   return_value={"name": "Hugh", "source": "profile"}):
            self.assertTrue(self_profile.speaker_is_enrolled_user("Hugh"))
            self.assertTrue(self_profile.speaker_is_enrolled_user("hugh"))
            self.assertFalse(self_profile.speaker_is_enrolled_user("Marc"))
            self.assertFalse(self_profile.speaker_is_enrolled_user(""))
            self.assertFalse(
                self_profile.speaker_is_enrolled_user("unknown speaker"))


class RelativeOwnershipTests(unittest.TestCase):
    """2-speaker fixtures: ownership relative to the labeled speaker."""

    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ex = Extractor(store=self.store)
        self.now = time.time()
        # Seed people so resolve_person finds them.
        self.hugh_id = self.store.resolve_person("Hugh", ts=self.now)
        self.marc_id = self.store.resolve_person("Marc", ts=self.now)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _user(self, name="Hugh"):
        return patch("app.services.identity.user_identity",
                     return_value={"name": name, "source": "profile"})

    def test_me_from_enrolled_user_maps_to_self(self):
        with self._user("Hugh"):
            pid = self.ex._resolve_person_id(
                "me", self.now, turn_speaker="Hugh", text="I'll send it")
            self.assertEqual(pid, self_profile.self_person_id(self.store))
            self.assertEqual(pid, self.hugh_id)

    def test_me_from_other_speaker_maps_to_that_speaker(self):
        with self._user("Hugh"):
            pid = self.ex._resolve_person_id(
                "me", self.now, turn_speaker="Marc",
                text="I'll send the deck")
            self.assertEqual(pid, self.marc_id)
            self.assertNotEqual(pid, self_profile.self_person_id(self.store))

    def test_me_from_unknown_speaker_does_not_map_to_self(self):
        with self._user("Hugh"):
            pid = self.ex._resolve_person_id(
                "me", self.now, turn_speaker="", text="I'll send it")
            self.assertIsNone(pid)
            pid2 = self.ex._resolve_person_id(
                "me", self.now, turn_speaker="unknown speaker",
                text="I'll send it")
            self.assertIsNone(pid2)

    def test_named_party_unchanged(self):
        with self._user("Hugh"):
            pid = self.ex._resolve_person_id(
                "Marc", self.now, turn_speaker="Hugh", text="tell Marc")
            self.assertEqual(pid, self.marc_id)

    def test_speaker_label_does_not_mint_via_fallthrough(self):
        """People v2 reject of 'Speaker 6' must not call store.resolve_person."""
        with self._user("Hugh"), patch.dict(
                "os.environ", {"QUILL_PEOPLE_V2": "1"}):
            before = {p["name"] for p in self.store.all_people()}
            pid = self.ex._resolve_person_id(
                "Speaker 6", self.now, turn_speaker="Speaker 6",
                event_source="audio.whisper",
                text="Speaker 6 said hello")
            self.assertIsNone(pid)
            me_pid = self.ex._resolve_person_id(
                "me", self.now, turn_speaker="Speaker 6",
                event_source="audio.whisper",
                text="I'll send it")
            self.assertIsNone(me_pid)
            after = {p["name"] for p in self.store.all_people()}
            self.assertEqual(before, after)
            self.assertNotIn("Speaker 6", after)

    def test_persist_ownership_two_speaker_fixture(self):
        """Same LLM 'me' commitment: Hugh→self, Marc→Marc — not swapped."""
        facts_me = {
            "tasks": [],
            "commitments": [{
                "text": "send the pricing follow-up",
                "from_person": "me",
                "to_person": "Eve",
                "due": "2026-08-10",
                "confidence": 0.9,
                "source_span": "I'll send the pricing follow-up",
                "assertion": "stated_by_user",
            }],
            "claims": [], "entities": [], "relations": [],
        }
        eve_id = self.store.resolve_person("Eve", ts=self.now)

        def _run(speaker: str):
            turn = Turn(start=1000, end=1001, speaker=speaker,
                        text="I'll send the pricing follow-up",
                        event_ids=[], n_utterances=1)
            with patch.object(self.ex, "_persist_entities", return_value=(0, 0)), \
                 patch.object(self.ex, "_record_faithfulness",
                              return_value=None), \
                 patch("app.services.extractor._index_fact",
                       lambda *a, **k: None), \
                 patch("app.services.fact_gate._similar_active",
                       return_value=[]), \
                 patch("app.services.people_pipeline.enabled",
                       return_value=False):
                self.ex._persist(turn, facts_me, self.now)
            rows = self.store.list_facts(kind="commitment", limit=10)
            # Newest first typically — find by from_person
            return rows

        with self._user("Hugh"):
            _run("Hugh")
            _run("Marc")
            rows = [dict(r) for r in self.store._conn.execute(
                "SELECT text, from_person_id, to_person_id FROM commitments "
                "WHERE text LIKE 'send the pricing%'").fetchall()]
            # Two commitments: one from Hugh (self), one from Marc.
            from_ids = {r["from_person_id"] for r in rows}
            self.assertIn(self.hugh_id, from_ids)
            self.assertIn(self.marc_id, from_ids)
            self.assertEqual(len(from_ids), 2)
            for r in rows:
                self.assertEqual(r["to_person_id"], eve_id)

    def test_extract_text_labels_turn(self):
        turn = Turn(start=1, end=2, speaker="Marc",
                    text="I'll book the venue", event_ids=[1], n_utterances=1)
        seen = {}

        def fake_complete(task, *, system, messages, schema, max_tokens, model):
            seen["user"] = messages[0]["content"]
            seen["system"] = system
            return {"tasks": [], "commitments": [], "claims": [],
                    "entities": [], "relations": []}

        with patch("app.services.model_router.router.complete_json",
                   side_effect=fake_complete):
            self.ex._extract_text(turn)
        self.assertIn("[Marc]: I'll book the venue", seen["user"])
        self.assertIn("Ownership is relative to the labeled speaker",
                      seen["system"])

    def test_claim_link_self_only_for_enrolled_speaker(self):
        facts = {
            "tasks": [], "commitments": [],
            "claims": [{
                "text": "I prefer morning meetings",
                "confidence": 0.9,
                "source_span": "I prefer morning meetings",
                "assertion": "stated_by_user",
            }],
            "entities": [], "relations": [],
        }

        def _persist_as(speaker):
            turn = Turn(start=1, end=2, speaker=speaker,
                        text="I prefer morning meetings",
                        event_ids=[], n_utterances=1)
            with patch.object(self.ex, "_persist_entities", return_value=(0, 0)), \
                 patch.object(self.ex, "_record_faithfulness",
                              return_value=None), \
                 patch("app.services.extractor._index_fact",
                       lambda *a, **k: None), \
                 patch("app.services.fact_gate._similar_active",
                       return_value=[]):
                self.ex._persist(turn, facts, self.now)

        with self._user("Hugh"):
            self_pid = self_profile.self_person_id(self.store)
            _persist_as("Marc")
            # Marc's first-person claim must NOT attach to Hugh's self node.
            linked = self.store._conn.execute(
                "SELECT COUNT(*) AS n FROM relations "
                "WHERE predicate = 'about_self' AND subj_id = ?",
                (self_pid,)).fetchone()["n"]
            self.assertEqual(linked, 0)
            _persist_as("Hugh")
            linked = self.store._conn.execute(
                "SELECT COUNT(*) AS n FROM relations "
                "WHERE predicate = 'about_self' AND subj_id = ?",
                (self_pid,)).fetchone()["n"]
            self.assertGreaterEqual(linked, 1)


class OwnershipEvalFixtureTests(unittest.TestCase):
    """Lightweight ownership eval on 2-speaker fixtures (plan 2.1 AC)."""

    FIXTURES = (
        # (speaker, from_person, enrolled_user, expect: "self"|"speaker"|"none")
        ("Hugh", "me", "Hugh", "self"),
        ("Marc", "me", "Hugh", "speaker"),
        ("", "me", "Hugh", "none"),
        ("unknown speaker", "me", "Hugh", "none"),
        ("Hugh", "Marc", "Hugh", "named"),  # named party, not me
    )

    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ex = Extractor(store=self.store)
        self.now = time.time()
        self.hugh = self.store.resolve_person("Hugh", ts=self.now)
        self.marc = self.store.resolve_person("Marc", ts=self.now)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_ownership_accuracy_on_fixtures(self):
        errors = 0
        with patch("app.services.identity.user_identity",
                   return_value={"name": "Hugh", "source": "profile"}), \
             patch("app.services.people_pipeline.enabled", return_value=False):
            self_pid = self_profile.self_person_id(self.store)
            for speaker, party, enrolled, expect in self.FIXTURES:
                pid = self.ex._resolve_person_id(
                    party, self.now, turn_speaker=speaker, text="x")
                if expect == "self":
                    ok = pid == self_pid
                elif expect == "speaker":
                    ok = pid == self.marc
                elif expect == "none":
                    ok = pid is None
                else:  # named Marc
                    ok = pid == self.marc
                if not ok:
                    errors += 1
        # Plan AC: 2-speaker ownership errors < 5% (here: zero on the fixture set).
        self.assertEqual(errors, 0, f"ownership errors={errors}")


if __name__ == "__main__":
    unittest.main()
