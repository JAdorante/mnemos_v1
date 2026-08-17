"""Context-based project attribution — facts inherit the room they were born
in. A fact minted during a meeting titled "Nexus weekly sync" gets a
fact→entity `about` edge (origin="context") to the existing Nexus entity even
when the sentence never says "Nexus"; nightly graph rebuilds can neither wipe
nor inflate that edge; and a title can never mint a new entity."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import context_attribution as ca
from app.storage import Store

NOW = 1_000_000_000.0


class ContextAttributionTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.eid_nexus = self.store.resolve_entity("Nexus", "project", ts=NOW)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _with_meeting(self, title: str):
        return patch("app.services.meeting_session.current",
                     return_value={"title": title, "status": "active"})

    def test_meeting_title_binds_existing_entity(self):
        with self._with_meeting("Nexus weekly sync"):
            ctx = ca.context_entities_for_turn(self.store)
        self.assertEqual([h["id"] for h in ctx], [self.eid_nexus])
        self.assertEqual(ctx[0]["via_title"], "Nexus weekly sync")

    def test_title_never_mints_an_entity(self):
        before = len(self.store.all_entities())
        with self._with_meeting("Zephyr planning session"):
            ctx = ca.context_entities_for_turn(self.store)
        self.assertEqual(ctx, [])
        self.assertEqual(len(self.store.all_entities()), before)

    def test_window_title_from_anchor_event(self):
        eid = self.store.resolve_entity("nexus_v1", "project", ts=NOW)
        ev_id = self.store.insert(Event(
            time=NOW, modality=Modality.VISION, raw="",
            source="desktop.screen", confidence=0.9,
            meta={"window": "app/main.py - nexus_v1 - Cursor"}))
        ctx = ca.context_entities_for_turn(self.store, anchor_event_id=ev_id)
        self.assertIn(eid, [h["id"] for h in ctx])

    def test_browser_suffix_is_stripped(self):
        chrome = self.store.resolve_entity("Chrome", "tool", ts=NOW)
        self.assertTrue(chrome)
        ev_id = self.store.insert(Event(
            time=NOW, modality=Modality.VISION, raw="",
            source="desktop.screen", confidence=0.9,
            meta={"window": "Quarterly plan - Google Chrome"}))
        ctx = ca.context_entities_for_turn(self.store, anchor_event_id=ev_id)
        # The "- Google Chrome" suffix must not bind the Chrome tool entity
        # from every browser tab.
        self.assertNotIn(chrome, [h["id"] for h in ctx])

    def test_self_windows_are_not_context(self):
        with patch("app.services.surface_filters.is_self_window",
                   return_value=True), \
             self._with_meeting("Nexus weekly sync"):
            ctx = ca.context_entities_for_turn(self.store)
        self.assertEqual(ctx, [])

    def test_kill_switch(self):
        import os
        with patch.dict(os.environ, {"QUILL_CONTEXT_ATTRIB": "0"}), \
             self._with_meeting("Nexus weekly sync"):
            self.assertEqual(ca.context_entities_for_turn(self.store), [])

    def _edge(self, fid):
        return self.store._conn.execute(
            "SELECT origin, weight FROM relations WHERE subj_type='fact' "
            "AND subj_id=? AND predicate='about' AND obj_type='entity' "
            "AND obj_id=?", (fid, self.eid_nexus)).fetchone()

    def test_stamp_writes_context_edge(self):
        fid = self.store.add_claim("we agreed to ship the beta on Friday",
                                   extracted_at=NOW)
        with self._with_meeting("Nexus weekly sync"):
            ctx = ca.context_entities_for_turn(self.store)
        n = ca.stamp_fact(self.store, fid, ctx, now=NOW)
        self.assertEqual(n, 1)
        row = self._edge(fid)
        self.assertEqual(row["origin"], "context")
        self.assertAlmostEqual(row["weight"], 0.5)

    def test_edge_survives_rebuild_without_weight_inflation(self):
        from app.services import graph
        # The claim text deliberately does NOT contain "Nexus" — only the
        # context stamp attributes it.
        fid = self.store.add_claim("we agreed to ship the beta on Friday",
                                   extracted_at=NOW)
        with self._with_meeting("Nexus weekly sync"):
            ctx = ca.context_entities_for_turn(self.store)
        ca.stamp_fact(self.store, fid, ctx, now=NOW)

        graph.rebuild(self.store)
        graph.rebuild(self.store)
        row = self._edge(fid)
        self.assertIsNotNone(row, "rebuild wiped the context edge")
        self.assertEqual(row["origin"], "context")
        self.assertAlmostEqual(row["weight"], 0.5)

        # Cross-origin re-add (text-match derived edge on the same key) takes
        # MAX(weight), not the sum — repeated rebuilds can't inflate it.
        self.store.add_relation("fact", fid, "about", "entity",
                                self.eid_nexus, weight=1.0, origin="derived",
                                ts=NOW)
        row = self._edge(fid)
        self.assertAlmostEqual(row["weight"], 1.0)
        self.store.add_relation("fact", fid, "about", "entity",
                                self.eid_nexus, weight=1.0, origin="derived",
                                ts=NOW)
        row = self._edge(fid)
        self.assertAlmostEqual(row["weight"], 1.0)


if __name__ == "__main__":
    unittest.main()
