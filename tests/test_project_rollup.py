"""Project rollup — entities inherit a single home project from the facts
they share with it, or none at all when the evidence is split. Edges carry
origin="rollup" so graph rebuilds can't wipe them and reruns can't double
them; the write-time gate keeps known people from minting as projects."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import project_rollup as pr
from app.storage import Store

NOW = 1_000_000_000.0


class RollupBase(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.mnemos = self.store.resolve_entity("Mnemos Build", "project", ts=NOW)
        self.pulse = self.store.resolve_entity("VenturePulse", "project", ts=NOW)
        self.lance = self.store.resolve_entity("LanceDB", "tool", ts=NOW)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _fact_about(self, *entity_origins: tuple[int, str]) -> int:
        fid = self.store.add_claim("shared work item", extracted_at=NOW)
        for eid, origin in entity_origins:
            self.store.add_relation("fact", fid, "about", "entity", eid,
                                    origin=origin, ts=NOW)
        return fid


class ComputeTests(RollupBase):
    def test_dominant_project_wins(self):
        for _ in range(3):
            self._fact_about((self.lance, "derived"), (self.mnemos, "derived"))
        self._fact_about((self.lance, "derived"), (self.pulse, "derived"))
        out = pr.compute(self.store)
        self.assertEqual(len(out), 1)
        a = out[0]
        self.assertEqual(a["entity_id"], self.lance)
        self.assertEqual(a["project_id"], self.mnemos)
        self.assertEqual(a["facts"], 3)
        self.assertAlmostEqual(a["share"], 0.75)

    def test_split_evidence_means_no_home(self):
        # 3 facts with each project — 0.5 share is ambiguity, not a home.
        for _ in range(3):
            self._fact_about((self.lance, "derived"), (self.mnemos, "derived"))
            self._fact_about((self.lance, "derived"), (self.pulse, "derived"))
        self.assertEqual(pr.compute(self.store), [])

    def test_min_facts_floor(self):
        # 100% share but only 2 facts — below the floor, stays loose.
        for _ in range(2):
            self._fact_about((self.lance, "derived"), (self.mnemos, "derived"))
        self.assertEqual(pr.compute(self.store), [])

    def test_context_edges_count_double(self):
        # 3 room-born facts with Mnemos Build (weight 2 each = 6) vs 4 plain
        # text matches with VenturePulse (4): context wins the dominance vote
        # despite fewer facts.
        for _ in range(3):
            self._fact_about((self.lance, "derived"), (self.mnemos, "context"))
        for _ in range(4):
            self._fact_about((self.lance, "derived"), (self.pulse, "derived"))
        out = pr.compute(self.store)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["project_id"], self.mnemos)

    def test_dismissed_facts_do_not_vote(self):
        fids = [self._fact_about((self.lance, "derived"),
                                 (self.mnemos, "derived")) for _ in range(3)]
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE facts SET review='dismissed' WHERE id=?", (fids[0],))
            self.store._conn.commit()
        self.assertEqual(pr.compute(self.store), [])  # 2 live facts < floor


class RunTests(RollupBase):
    def _seed_dominant(self):
        for _ in range(3):
            self._fact_about((self.lance, "derived"), (self.mnemos, "derived"))

    def test_run_writes_current_and_rerun_is_stable(self):
        self._seed_dominant()
        res = pr.run(self.store, now=NOW)
        self.assertEqual(res["associated"], 1)
        homes = pr.current(self.store)
        self.assertEqual(homes[self.lance]["id"], self.mnemos)
        share = homes[self.lance]["share"]
        pr.run(self.store, now=NOW)  # rerun: cleared + re-minted, not doubled
        self.assertEqual(pr.current(self.store)[self.lance]["share"], share)

    def test_survives_derived_edge_wipe(self):
        # graph.rebuild clears origin="derived" — rollup edges must outlive it.
        self._seed_dominant()
        pr.run(self.store, now=NOW)
        self.store.clear_relations(origin="derived")
        self.assertIn(self.lance, pr.current(self.store))

    def test_kill_switch(self):
        self._seed_dominant()
        with patch.dict(os.environ, {"QUILL_PROJECT_ROLLUP": "0"}):
            res = pr.run(self.store, now=NOW)
        self.assertFalse(res["enabled"])
        self.assertEqual(pr.current(self.store), {})

    def test_hidden_entities_stay_out(self):
        self._seed_dominant()
        self.store.set_entity_hidden(self.lance, hidden=True)
        self.assertEqual(pr.compute(self.store), [])


class PersonNameMintGateTests(unittest.TestCase):
    """A name already living in the people table can't fork into a
    project/idea entity — the write-time gate behind "Justin"[project]."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.store.resolve_person("Marc", ts=NOW)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _resolver(self):
        from app.services.resolution import Resolver
        return Resolver(store=self.store)

    def test_known_person_cannot_mint_as_project(self):
        before = len(self.store.all_entities(include_hidden=True))
        self.assertEqual(self._resolver().resolve_entity(
            "Marc", "project", ts=NOW), 0)
        self.assertEqual(
            len(self.store.all_entities(include_hidden=True)), before)

    def test_org_with_a_persons_name_still_mints(self):
        # A company can share a founder's name — only project/idea are gated.
        eid = self._resolver().resolve_entity("Marc", "org", ts=NOW)
        self.assertGreater(eid, 0)

    def test_unknown_name_still_mints_as_project(self):
        eid = self._resolver().resolve_entity("Zephyr", "project", ts=NOW)
        self.assertGreater(eid, 0)


class CleanupPlanTests(unittest.TestCase):
    """ambient_cleanup flags existing project/idea rows that wear a known
    person's name (the rows the write-gate now prevents)."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_known_person_project_is_flagged_org_is_kept(self):
        from app.services import ambient_cleanup as ac
        self.store.resolve_person("Marc", ts=NOW)
        self.store.resolve_person("Sasha Grey", ts=NOW)
        bad = self.store.resolve_entity("Marc", "project", ts=NOW)
        org = self.store.resolve_entity("Sasha Grey Consulting", "org", ts=NOW)
        plan = ac.plan_entities(self.store)
        flagged = {p["id"]: p for p in plan}
        self.assertIn(bad, flagged)
        self.assertEqual(flagged[bad]["reason"], "known_person_name")
        self.assertEqual(flagged[bad]["action"], "hide")
        self.assertNotIn(org, flagged)


if __name__ == "__main__":
    unittest.main()
