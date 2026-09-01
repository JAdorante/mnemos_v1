"""WS3 graph integration — proposed edges are re-derived by every rebuild,
idea topic edges (asserted, via aliases) survive rebuilds, context_for_person
gains an ideas section, and dismissed ideas fall out of retrieval."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services import entity_alias as ea
from app.services import graph
from app.services.memory import fact_is_retrievable
from app.storage import Store

NOW = 1_700_000_000.0


class IdeaGraphTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.sarah = self.store.resolve_person("Sarah Chen", ts=NOW)
        self.cap = self.store.resolve_entity("Capital Connect", "project",
                                             ts=NOW)
        self.fid = self.store.add_idea(
            "host the cloud path ourselves",
            source_span="we could host the cloud path ourselves",
            confidence=0.8, originator_person_id=self.sarah,
            originator_label="Sarah Chen", meeting_session_id=7,
            extracted_at=NOW)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _proposed_edges(self):
        return self.store._conn.execute(
            "SELECT * FROM relations WHERE predicate='proposed' "
            "AND subj_type='person' AND obj_type='fact'").fetchall()

    def test_proposed_edge_survives_rebuild(self):
        graph.rebuild(self.store)
        edges = self._proposed_edges()
        self.assertEqual(len(edges), 1)
        self.assertEqual(int(edges[0]["subj_id"]), self.sarah)
        self.assertEqual(int(edges[0]["obj_id"]), self.fid)
        graph.rebuild(self.store)
        edges = self._proposed_edges()
        self.assertEqual(len(edges), 1, "rebuild must re-derive, not dup")

    def test_about_edge_via_alias_survives_rebuild(self):
        # Topic named by a CONFIRMED alias resolves to the real entity.
        self.store.upsert_entity_alias(
            self.cap, "capconnect", "capconnect", source="user",
            confirmed=True, ts=NOW, day="2026-08-20")
        eid = ea.resolve("capconnect", store=self.store, ts=NOW)
        self.assertEqual(eid, self.cap)
        self.store.add_relation("fact", self.fid, "about", "entity", eid,
                                origin="asserted", confidence=0.8, ts=NOW)
        graph.rebuild(self.store)
        row = self.store._conn.execute(
            "SELECT origin FROM relations WHERE subj_type='fact' "
            "AND subj_id=? AND predicate='about' AND obj_id=?",
            (self.fid, self.cap)).fetchone()
        self.assertIsNotNone(row, "asserted topic edge must survive rebuild")
        self.assertEqual(row["origin"], "asserted")

    def test_context_for_person_returns_ideas_section(self):
        graph.rebuild(self.store)
        ctx = graph.context_for_person("Sarah Chen", self.store)
        self.assertTrue(ctx["found"])
        self.assertEqual(len(ctx["ideas"]), 1)
        idea = ctx["ideas"][0]
        self.assertEqual(idea["fact_id"], self.fid)
        self.assertEqual(idea["text"], "host the cloud path ourselves")
        self.assertEqual(idea["meeting_session_id"], 7)
        # Ideas do not double-report as open items.
        self.assertFalse(any(i["fact_id"] == self.fid
                             for i in ctx["items"]))

    def test_dismissed_ideas_vanish(self):
        graph.rebuild(self.store)
        self.store.review_fact(self.fid, "dismissed")
        ctx = graph.context_for_person("Sarah Chen", self.store)
        self.assertEqual(ctx["ideas"], [])
        # Same lifecycle contract search hydration applies (vector hits for
        # dismissed facts are filtered by fact_is_retrievable).
        self.assertFalse(fact_is_retrievable(self.store.get_fact(self.fid)))

    def test_fact_person_links_carries_proposed_role(self):
        links = self.store.fact_person_links()
        self.assertIn((self.fid, self.sarah, "proposed"), links)

    def test_escrowed_idea_stays_out_of_links(self):
        track = self.store.get_or_create_speaker_track("Speaker 9", ts=NOW)
        fid2 = self.store.add_idea(
            "park this one", source_span="park this one", confidence=0.7,
            originator_person_id=self.sarah, originator_label="Speaker 9",
            originator_track_id=track, extracted_at=NOW)
        self.store.escrow_fact(fid2, track, idea_originator=True)
        links = self.store.fact_person_links()
        self.assertNotIn((fid2, self.sarah, "proposed"), links)


if __name__ == "__main__":
    unittest.main()
