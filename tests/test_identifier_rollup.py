"""WS2 rollup — stamped identifiers become derived observed_on_screen graph
evidence: per-block aggregation, alias-path resolution, rebuild idempotence,
and erasure symmetry (erasing the frame drops the evidence)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.events import Event, Modality
from app.services import graph, identifier_rollup
from app.storage import Store

NOW = 1_700_000_000.0


def _frame(ts: float, window: str, idents: list[dict]) -> Event:
    return Event(time=ts, modality=Modality.VISION, raw="ocr text",
                 source="desktop.screen",
                 meta={"window": window, "identifiers": idents})


class RollupTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        # Canonical differs from the on-screen slug → exercises the
        # normalized alias path, not exact match.
        self.nexus = self.store.resolve_entity("Nexus V1", "project", ts=NOW)
        self.ev1 = self.store.insert(_frame(
            NOW, "storage.py - nexus_v1 - Cursor",
            [{"kind": "title_segment", "value": "nexus_v1",
              "norm": "nexus_v1"},
             {"kind": "repo", "value": "JAdorante/nexus_v1",
              "norm": "nexus_v1"}]))
        self.ev2 = self.store.insert(_frame(
            NOW + 60, "README.md - unknown_proj - Cursor",
            [{"kind": "title_segment", "value": "unknown_proj",
              "norm": "unknown_proj"}]))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _edges(self):
        return self.store._conn.execute(
            "SELECT subj_id, obj_id, weight, origin FROM relations "
            "WHERE predicate = 'observed_on_screen'").fetchall()

    def test_aggregate_identifiers_per_block(self):
        rows = self.store.events_with_identifiers()
        agg = identifier_rollup.aggregate_identifiers(
            rows, t0=NOW - 1, t1=NOW + 1)
        self.assertEqual(agg.get("nexus_v1"), 2)
        self.assertNotIn("unknown_proj", agg)  # ev2 is outside the block
        agg_all = identifier_rollup.aggregate_identifiers(rows)
        self.assertEqual(agg_all.get("unknown_proj"), 1)

    def test_derive_edges_resolves_via_alias_and_counts_evidence(self):
        n = identifier_rollup.derive_edges(self.store, now=NOW)
        self.assertEqual(n, 1)
        edges = self._edges()
        self.assertEqual(len(edges), 1)
        self.assertEqual(int(edges[0]["subj_id"]), self.nexus)
        self.assertEqual(int(edges[0]["obj_id"]), self.ev1)
        self.assertAlmostEqual(float(edges[0]["weight"]), 2.0)  # two idents
        self.assertEqual(edges[0]["origin"], "derived")

    def test_unknown_identifier_never_mints(self):
        before = len(self.store.all_entities())
        identifier_rollup.derive_edges(self.store, now=NOW)
        self.assertEqual(len(self.store.all_entities()), before)

    def test_rebuild_rederives_idempotently(self):
        counts1 = graph.rebuild(self.store)
        self.assertEqual(counts1.get("observed_on_screen"), 1)
        counts2 = graph.rebuild(self.store)
        self.assertEqual(counts2.get("observed_on_screen"), 1)
        edges = self._edges()
        self.assertEqual(len(edges), 1)
        self.assertAlmostEqual(float(edges[0]["weight"]), 2.0,
                               msg="rebuild must not inflate evidence")

    def test_erase_event_drops_evidence(self):
        graph.rebuild(self.store)
        self.assertEqual(len(self._edges()), 1)
        res = self.store.erase_event(self.ev1)
        self.assertTrue(res["ok"])
        self.assertEqual(len(self._edges()), 0,
                         "citing relations must drop with the event")
        graph.rebuild(self.store)
        self.assertEqual(len(self._edges()), 0,
                         "an erased frame must not re-derive evidence")

    def test_rollup_does_not_accumulate_alias_recurrence(self):
        for _ in range(5):
            identifier_rollup.derive_edges(self.store, now=NOW)
        rows = self.store.list_entity_aliases(entity_id=self.nexus)
        # record=False: rollup re-scans must not leave recurrence trails.
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
