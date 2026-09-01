"""WS1 extractor wiring — the context prior is observational-first and
eval-gated: prompt contains the context block iff QUILL_EXTRACT_CONTEXT=1,
the prompt version is stamped accordingly, anchors stamp the anchor event
even with the flag off (observational invariant), and about_project
relations materialize only with transcript support."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services.activity import Activity
from app.services.consolidation import Turn
from app.services.extractor import (Extractor, effective_prompt_version,
                                    effective_schema_version, turn_hash)
from app.storage import Store

NOW = 1_700_000_000.0

CTX_ON = {"QUILL_EXTRACT_CONTEXT": "1"}
CTX_OFF = {"QUILL_EXTRACT_CONTEXT": "0"}


def _block(start, end, app, windows):
    a = Activity(start=start, end=end, app=app)
    a.windows = list(windows)
    return a


class ContextPriorTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ex = Extractor(store=self.store)
        self.nexus = self.store.resolve_entity("Nexus V1", "project", ts=NOW)
        self.store.replace_activities([
            _block(NOW - 10, NOW + 60, "Cursor",
                   ["storage.py - nexus_v1 - Cursor"]),
        ])
        self.ev = self.store.insert(Event(
            time=NOW, modality=Modality.AUDIO,
            raw="let's ship the signals thing next week",
            source="audio.whisper"))
        # Hermetic: the fact gate's semantic dedup and fact indexing hit the
        # GLOBAL LanceDB store — patch both so fixtures never touch (or get
        # deduped against) the live index.
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

    def _turn(self, text="let's ship the signals thing next week"):
        return Turn(start=NOW, end=NOW + 30, speaker="Justin", text=text,
                    event_ids=[self.ev], n_utterances=1)

    def _capture_llm(self):
        captured = {}

        def fake(task, *, system, messages, schema, **kw):
            captured["system"] = system
            captured["schema"] = schema
            return {"tasks": [], "commitments": [], "claims": [],
                    "questions": [], "entities": [], "relations": []}
        return captured, patch(
            "app.services.model_router.router.complete_json",
            side_effect=fake)

    # ---------------------------- prompt ----------------------------------
    def test_flag_off_no_context_block(self):
        captured, p = self._capture_llm()
        with patch.dict(os.environ, CTX_OFF), p:
            self.ex._extract_text(self._turn())
        self.assertNotIn("Context (may be irrelevant", captured["system"])
        preds = captured["schema"]["properties"]["relations"]["items"][
            "properties"]["predicate"]["enum"]
        self.assertNotIn("about_project", preds)

    def test_flag_on_context_block_and_about_project_schema(self):
        captured, p = self._capture_llm()
        with patch.dict(os.environ, CTX_ON), p:
            self.ex._extract_text(self._turn())
        sys_prompt = captured["system"]
        self.assertIn("Context (may be irrelevant", sys_prompt)
        self.assertIn("never invent facts from it", sys_prompt)
        self.assertIn("Cursor", sys_prompt)
        self.assertIn("Nexus V1", sys_prompt)
        preds = captured["schema"]["properties"]["relations"]["items"][
            "properties"]["predicate"]["enum"]
        self.assertIn("about_project", preds)

    # ---------------------------- versions --------------------------------
    def test_version_stamping(self):
        with patch.dict(os.environ, CTX_OFF):
            self.assertEqual(effective_prompt_version(), "extract-v1")
        with patch.dict(os.environ, CTX_ON):
            self.assertEqual(effective_prompt_version(), "extract-v2-ctx")
        with patch.dict(os.environ, {"QUILL_EXTRACT_IDEAS": "1"}):
            self.assertEqual(effective_schema_version(), "facts-schema-v4")

    def test_candidate_rows_carry_effective_version(self):
        turn = self._turn()
        facts = {"claims": [{
            "text": "the signals thing ships next week",
            "confidence": 0.9,
            "source_span": "ship the signals thing next week",
            "assertion": "stated_by_user"}]}
        with patch.dict(os.environ, CTX_ON):
            self.ex._persist(turn, facts, NOW)
        rows = self.store.list_fact_candidates(turn_hash=turn_hash(turn))
        self.assertTrue(rows)
        self.assertEqual(rows[0]["prompt_version"], "extract-v2-ctx")

    # ---------------------------- observational ---------------------------
    def test_anchors_stamped_with_flag_off(self):
        turn = self._turn()
        facts = {"claims": [{
            "text": "the signals thing ships next week",
            "confidence": 0.9,
            "source_span": "ship the signals thing next week",
            "assertion": "stated_by_user"}]}
        with patch.dict(os.environ, CTX_OFF):
            self.ex._persist(turn, facts, NOW)
        ev = self.store.get_event(self.ev)
        import json
        meta = json.loads(ev.get("meta") or "{}")
        anchor = meta.get("context_anchor")
        self.assertIsNotNone(anchor, "observational stamp must not depend "
                                     "on the prompt flag")
        self.assertEqual(anchor["apps"][0]["app"], "Cursor")
        self.assertTrue(any(e.get("entity_id") == self.nexus
                            for e in anchor["entities"]))

    def test_dominant_anchor_attaches_derived_context_edge(self):
        turn = self._turn()
        facts = {"claims": [{
            "text": "the signals thing ships next week",
            "confidence": 0.9,
            "source_span": "ship the signals thing next week",
            "assertion": "stated_by_user"}]}
        with patch.dict(os.environ, CTX_OFF):
            self.ex._persist(turn, facts, NOW)
        row = self.store._conn.execute(
            "SELECT r.origin FROM relations r JOIN facts f ON f.id=r.subj_id "
            "WHERE r.subj_type='fact' AND r.predicate='about' "
            "AND r.obj_type='entity' AND r.obj_id=?",
            (self.nexus,)).fetchone()
        self.assertIsNotNone(row, "dominant-app anchor must attach a "
                                  "derived about edge")
        self.assertEqual(row["origin"], "context")

    # ---------------------------- attribution -----------------------------
    def _persist_with_relation(self, span: str, obj: str = "nexus_v1"):
        turn = self._turn()
        facts = {
            "claims": [{
                "text": "the signals thing ships next week",
                "confidence": 0.9,
                "source_span": "ship the signals thing next week",
                "assertion": "stated_by_user"}],
            "relations": [{
                "subject": "this", "subject_kind": "entity",
                "predicate": "about_project", "object": obj,
                "object_kind": "entity", "confidence": 0.8,
                "source_span": span}],
        }
        with patch.dict(os.environ, CTX_ON):
            self.ex._persist(turn, facts, NOW)

    def _asserted_about_edges(self):
        return self.store._conn.execute(
            "SELECT * FROM relations WHERE subj_type='fact' "
            "AND predicate='about' AND obj_type='entity' "
            "AND origin='asserted'").fetchall()

    def test_about_project_attaches_asserted_edge(self):
        self._persist_with_relation("ship the signals thing next week")
        edges = self._asserted_about_edges()
        self.assertEqual(len(edges), 1)
        self.assertEqual(int(edges[0]["obj_id"]), self.nexus)

    def test_about_project_requires_transcript_span(self):
        self._persist_with_relation("something the turn never said")
        self.assertEqual(self._asserted_about_edges(), [])

    def test_about_project_never_mints(self):
        before = len(self.store.all_entities())
        self._persist_with_relation("ship the signals thing next week",
                                    obj="totally-unknown-project")
        self.assertEqual(self._asserted_about_edges(), [])
        self.assertEqual(len(self.store.all_entities()), before)


if __name__ == "__main__":
    unittest.main()
