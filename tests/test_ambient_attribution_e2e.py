"""Ambient-context attribution — the recorded end-to-end fixture from the
brief's definition of done. One store, one afternoon: desktop frames (with
OCR identifiers), an activity block, a meeting, and a spoken turn. The LLM
is mocked (its output is the fixture); everything downstream is real.

Demonstrates:
  1. unnamed reference → correct project attribution ("the signals thing"
     gains an about edge to Capital Connect via the dominant-app anchor and
     the transcript-supported about_project relation),
  2. screen identifier → graph evidence (observed_on_screen after rebuild),
  3. spoken idea → speaker-bound provenance (originator = the turn speaker,
     playable via the anchor event, meeting-bound).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.perception import identifiers as idn
from app.services import graph, self_profile
from app.services.activity import Activity
from app.services.consolidation import Turn
from app.services.extractor import Extractor
from app.storage import Store

NOW = 1_700_000_000.0
FLAGS = {"QUILL_EXTRACT_CONTEXT": "1", "QUILL_EXTRACT_IDEAS": "1"}


class AmbientAttributionE2E(unittest.TestCase):
    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ex = Extractor(store=self.store)
        for target in ("app.services.fact_gate._similar_active",
                       "app.services.memory.memory.index_fact"):
            p = patch(target, return_value=[])
            p.start()
            self.addCleanup(p.stop)

        # The world: a project entity, a known colleague.
        self.cap = self.store.resolve_entity("Capital Connect", "project",
                                             ts=NOW)
        self.sarah = self.store.resolve_person("Sarah Chen", ts=NOW)

        # Desktop: an IDE frame whose OCR carries verbatim identifiers —
        # stamped exactly the way the capture path stamps them.
        frame = Event(
            time=NOW - 60, modality=Modality.VISION,
            raw=("signals.py - capital-connect - Cursor\n"
                 "github.com/JAdorante/capital-connect"),
            source="desktop.screen",
            meta={"window": "signals.py - capital-connect - Cursor"})
        idn.stamp_event(frame)
        self.frame_id = self.store.insert(frame)

        # The rolled-up activity block (dominant app over the turn window).
        block = Activity(start=NOW - 120, end=NOW + 120, app="Cursor")
        block.windows = ["signals.py - capital-connect - Cursor"]
        self.store.replace_activities([block])

        # The spoken turn's anchor event, stamped with the live meeting.
        self.anchor = self.store.insert(Event(
            time=NOW, modality=Modality.AUDIO,
            raw="what if we shipped the signals thing as its own tier",
            source="audio.whisper",
            meta={"meeting_session_id": 5,
                  "audio_path": "audio/turn.wav"}))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_end_to_end(self):
        turn = Turn(
            start=NOW, end=NOW + 20, speaker="Sarah Chen",
            text="what if we shipped the signals thing as its own tier",
            event_ids=[self.anchor], n_utterances=1)
        # The mocked extractor output: an idea, plus the transcript-supported
        # project attribution the context hint enabled.
        facts = {
            "ideas": [{
                "text": "ship the signals thing as its own tier",
                "confidence": 0.85,
                "source_span": "shipped the signals thing as its own tier",
                "assertion": "stated_by_user",
                "originator": "somebody the model made up",
                "topic_entities": []}],
            "relations": [{
                "subject": "this", "subject_kind": "entity",
                "predicate": "about_project", "object": "capital-connect",
                "object_kind": "entity", "confidence": 0.8,
                "source_span": "shipped the signals thing as its own tier"}],
        }
        with patch.dict(os.environ, FLAGS), \
                patch("app.services.identity.user_identity",
                      return_value={"name": "Justin", "source": "profile"}):
            n = self.ex._persist(turn, facts, NOW)
        self.assertEqual(n, 1)

        idea = self.store._conn.execute(
            "SELECT f.id, f.text, i.originator_person_id, i.originator_label,"
            " i.meeting_session_id FROM facts f JOIN ideas i "
            "ON i.fact_id = f.id WHERE f.kind='idea'").fetchone()
        self.assertIsNotNone(idea)
        fid = int(idea["id"])

        # (3) speaker-bound provenance: originator is Sarah, never the
        # model's string; the meeting rode the anchor event's stamp; the
        # clip affordance has an event to play.
        self.assertEqual(idea["originator_person_id"], self.sarah)
        self.assertEqual(idea["meeting_session_id"], 5)
        fact = self.store.get_fact(fid)
        self.assertEqual(fact["originator"], "Sarah Chen")
        ev = self.store.get_event(self.anchor)
        self.assertIn("audio_path", json.loads(ev["meta"]))

        # (1) unnamed reference → project attribution, twice over: the
        # asserted about_project edge (transcript-supported) and the derived
        # context edge would both land on the same key — asserted wins.
        edge = self.store._conn.execute(
            "SELECT origin FROM relations WHERE subj_type='fact' "
            "AND subj_id=? AND predicate='about' AND obj_id=?",
            (fid, self.cap)).fetchone()
        self.assertIsNotNone(edge, "'the signals thing' must attribute "
                                   "to Capital Connect")
        self.assertEqual(edge["origin"], "asserted")

        # Observational stamp landed on the anchor event.
        anchors = json.loads(
            self.store.get_event(self.anchor)["meta"])["context_anchor"]
        self.assertEqual(anchors["apps"][0]["app"], "Cursor")

        # (2) screen identifier → graph evidence after rebuild.
        counts = graph.rebuild(self.store)
        self.assertGreaterEqual(counts.get("observed_on_screen", 0), 1)
        obs = self.store._conn.execute(
            "SELECT * FROM relations WHERE predicate='observed_on_screen' "
            "AND subj_id=? AND obj_id=?",
            (self.cap, self.frame_id)).fetchone()
        self.assertIsNotNone(obs)

        # The proposed edge + ideas section round-trip.
        ctx = graph.context_for_person("Sarah Chen", self.store)
        self.assertEqual([i["fact_id"] for i in ctx["ideas"]], [fid])

        # Erasure symmetry: forgetting the frame drops its evidence.
        self.store.erase_event(self.frame_id)
        graph.rebuild(self.store)
        obs = self.store._conn.execute(
            "SELECT 1 FROM relations WHERE predicate='observed_on_screen'"
        ).fetchone()
        self.assertIsNone(obs)


if __name__ == "__main__":
    unittest.main()
