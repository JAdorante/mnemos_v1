"""WS3 — speaker-attributed idea extraction.

The originator is bound in CODE to the turn's speaker (the model's
originator field is never trusted), topic entities attach only when
literally present in the turn, the schema/prompt ride the
QUILL_EXTRACT_IDEAS flag, and quoted/hypothetical brainstorms route to
review instead of auto-insert."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import self_profile
from app.services.consolidation import Turn
from app.services.extractor import Extractor, _schema
from app.storage import Store

NOW = 1_700_000_000.0
IDEAS_ON = {"QUILL_EXTRACT_IDEAS": "1"}


def _idea(text, span, *, originator="Justin", assertion="stated_by_user",
          topics=None, confidence=0.85):
    return {"text": text, "confidence": confidence, "source_span": span,
            "assertion": assertion, "originator": originator,
            "topic_entities": list(topics or [])}


class IdeaSchemaTests(unittest.TestCase):
    def test_schema_gated_by_flag(self):
        with patch.dict(os.environ, {"QUILL_EXTRACT_IDEAS": "0"}):
            self.assertNotIn("ideas", _schema()["properties"])
        with patch.dict(os.environ, IDEAS_ON):
            s = _schema()
            self.assertIn("ideas", s["properties"])
            self.assertIn("ideas", s["required"])
            item = s["properties"]["ideas"]["items"]
            self.assertIn("originator", item["properties"])
            self.assertIn("topic_entities", item["properties"])

    def test_prompt_carries_boundary_rules_iff_flag_on(self):
        captured = {}

        def fake(task, *, system, messages, schema, **kw):
            captured["system"] = system
            return {"tasks": [], "commitments": [], "claims": [],
                    "questions": [], "entities": [], "relations": []}
        tmp = Path(tempfile.mkdtemp())
        store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.addCleanup(store.close)
        ex = Extractor(store=store)
        turn = Turn(start=NOW, end=NOW + 5, speaker="Marc",
                    text="what if we tried a hosted cloud path",
                    event_ids=[], n_utterances=1)
        with patch("app.services.model_router.router.complete_json",
                   side_effect=fake), patch.dict(os.environ, IDEAS_ON):
            ex._extract_text(turn)
        self.assertIn("An IDEA is a proposal", captured["system"])
        self.assertIn("never both", captured["system"])
        with patch("app.services.model_router.router.complete_json",
                   side_effect=fake), \
                patch.dict(os.environ, {"QUILL_EXTRACT_IDEAS": "0"}):
            ex._extract_text(turn)
        self.assertNotIn("An IDEA is a proposal", captured["system"])


class IdeaPersistTests(unittest.TestCase):
    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ex = Extractor(store=self.store)
        self.marc = self.store.resolve_person("Marc", ts=NOW)
        for target in ("app.services.fact_gate._similar_active",
                       "app.services.memory.memory.index_fact"):
            p = patch(target, return_value=[])
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _user(self, name="Hugh"):
        return patch("app.services.identity.user_identity",
                     return_value={"name": name, "source": "profile"})

    def _persist(self, speaker, idea, text=None, meta=None):
        ev = self.store.insert(Event(
            time=NOW, modality=Modality.AUDIO,
            raw=text or "what if we tried a hosted cloud path",
            source="audio.whisper", meta=meta or {}))
        turn = Turn(start=NOW, end=NOW + 5, speaker=speaker,
                    text=text or "what if we tried a hosted cloud path",
                    event_ids=[ev], n_utterances=1)
        self.ex._persist(turn, {"ideas": [idea]}, NOW)
        row = self.store._conn.execute(
            "SELECT f.id, f.kind, f.text, f.state, f.review, i.* "
            "FROM facts f LEFT JOIN ideas i ON i.fact_id = f.id "
            "WHERE f.kind = 'idea' ORDER BY f.id DESC").fetchone()
        return dict(row) if row else None

    def test_originator_is_speaker_not_model_output(self):
        """Adversarial: model names 'Justin'; the labeled speaker is Marc —
        the persisted originator is Marc."""
        with self._user("Hugh"):
            row = self._persist("Marc", _idea(
                "try a hosted cloud path",
                "what if we tried a hosted cloud path",
                originator="Justin"))
        self.assertIsNotNone(row)
        self.assertEqual(row["originator_person_id"], self.marc)
        self.assertEqual(row["originator_label"], "Marc")

    def test_enrolled_user_binds_to_self_node(self):
        with self._user("Hugh"):
            row = self._persist("Hugh", _idea(
                "try a hosted cloud path",
                "what if we tried a hosted cloud path",
                originator="somebody else"))
            self.assertEqual(row["originator_person_id"],
                             self_profile.self_person_id(self.store))

    def test_anonymous_cluster_keeps_label(self):
        with self._user("Hugh"):
            row = self._persist("Speaker 2", _idea(
                "try a hosted cloud path",
                "what if we tried a hosted cloud path"))
        self.assertIsNone(row["originator_person_id"])
        self.assertEqual(row["originator_label"], "Speaker 2")
        self.assertEqual(row["state"], "active")  # escrow flag off: no park

    def test_hypothetical_routes_to_review(self):
        with self._user("Hugh"):
            row = self._persist("Marc", _idea(
                "maybe move the launch",
                "what if we tried a hosted cloud path",
                assertion="hypothetical"))
        self.assertIsNone(row, "hypothetical ideas need a human verdict")

    def test_topic_entities_only_literal_and_bind_only(self):
        vp = self.store.resolve_entity("VenturePulse", "project", ts=NOW)
        text = "what if we pointed VenturePulse at the hosted cloud path"
        before = len(self.store.all_entities())
        with self._user("Hugh"):
            row = self._persist("Marc", _idea(
                "point VenturePulse at the hosted cloud path",
                "pointed VenturePulse at the hosted cloud path",
                topics=["VenturePulse", "Atlantis"]), text=text)
        edges = self.store._conn.execute(
            "SELECT obj_id FROM relations WHERE subj_type='fact' "
            "AND subj_id=? AND predicate='about'",
            (row["fact_id"],)).fetchall()
        self.assertEqual([int(e["obj_id"]) for e in edges], [vp])
        self.assertEqual(len(self.store.all_entities()), before,
                         "unresolved topics are dropped, never minted")

    def test_topic_not_in_turn_is_dropped_even_if_resolvable(self):
        self.store.resolve_entity("Nexus V1", "project", ts=NOW)
        with self._user("Hugh"):
            row = self._persist("Marc", _idea(
                "try a hosted cloud path",
                "what if we tried a hosted cloud path",
                topics=["Nexus V1"]))
        edges = self.store._conn.execute(
            "SELECT 1 FROM relations WHERE subj_type='fact' AND subj_id=? "
            "AND predicate='about'", (row["fact_id"],)).fetchall()
        self.assertEqual(edges, [])

    def test_meeting_session_binding(self):
        with self._user("Hugh"):
            row = self._persist("Marc", _idea(
                "try a hosted cloud path",
                "what if we tried a hosted cloud path"),
                meta={"meeting_session_id": 42})
        self.assertEqual(row["meeting_session_id"], 42)
        fact = self.store.get_fact(row["fact_id"])
        self.assertEqual(fact["meeting_session_id"], 42)
        self.assertEqual(fact["originator"], "Marc")

    def test_candidate_rows_carry_idea_kind(self):
        from app.services.extractor import turn_hash
        ev = self.store.insert(Event(
            time=NOW, modality=Modality.AUDIO,
            raw="what if we tried a hosted cloud path",
            source="audio.whisper"))
        turn = Turn(start=NOW, end=NOW + 5, speaker="Marc",
                    text="what if we tried a hosted cloud path",
                    event_ids=[ev], n_utterances=1)
        with self._user("Hugh"):
            self.ex._persist(turn, {"ideas": [_idea(
                "try a hosted cloud path",
                "what if we tried a hosted cloud path")]}, NOW)
        rows = self.store.list_fact_candidates(turn_hash=turn_hash(turn))
        self.assertEqual([r["kind"] for r in rows], ["idea"])
        self.assertEqual(rows[0]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
