"""Field time dimension (WS3) — snapshots, diff, aging gravity."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fixtures.ranking_corpus import CORPUS_NOW


class FieldDiffTests(unittest.TestCase):
    def test_snapshot_and_diff_entered_left(self):
        from app.services import field_history as fh
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                field_a = {
                    "nodes": [
                        {"id": "person:1", "layer": "focus", "gravity": 0.8,
                         "kind": "person"},
                        {"id": "fact:1", "layer": "focus", "gravity": 0.6,
                         "kind": "task"},
                        {"id": "entity:1", "layer": "periphery", "gravity": 0.3,
                         "kind": "tool"},
                    ]
                }
                store.add_field_snapshot(
                    version="v1", ts=CORPUS_NOW - 86400,
                    focus_ids=["person:1", "fact:1"],
                    periphery_ids=["entity:1"],
                    per_node={
                        "person:1": {"gravity_total": 0.8, "kind": "person"},
                        "fact:1": {"gravity_total": 0.6, "kind": "task"},
                        "entity:1": {"gravity_total": 0.3, "kind": "tool"},
                    },
                )
                field_b = {
                    "nodes": [
                        {"id": "person:1", "layer": "focus", "gravity": 0.85,
                         "kind": "person"},
                        {"id": "person:2", "layer": "focus", "gravity": 0.7,
                         "kind": "person"},
                        {"id": "entity:1", "layer": "focus", "gravity": 0.55,
                         "kind": "tool"},
                    ]
                }
                # Persist "current" then diff against yesterday's snapshot.
                store.add_field_snapshot(
                    version="v2", ts=CORPUS_NOW,
                    focus_ids=["person:1", "person:2", "entity:1"],
                    periphery_ids=[],
                    per_node={
                        "person:1": {"gravity_total": 0.85, "kind": "person"},
                        "person:2": {"gravity_total": 0.7, "kind": "person"},
                        "entity:1": {"gravity_total": 0.55, "kind": "tool"},
                        "fact:1": {"gravity_total": 0.4, "kind": "task"},
                    },
                )
                d = fh.diff(store, since=CORPUS_NOW - 86400, now=CORPUS_NOW,
                            current=field_b)
                self.assertIn("person:2", d["entered_focus"])
                self.assertIn("entity:1", d["entered_focus"])
                self.assertIn("fact:1", d["left_focus"])
                self.assertTrue(d["has_prior"])
                # Rising: person:1 and entity:1 gained gravity
                rise_ids = {r["id"] for r in d["rising"]}
                self.assertIn("person:1", rise_ids)
                self.assertIn("entity:1", rise_ids)
            finally:
                store.close()

    def test_aging_list_threshold(self):
        from app.services import field_history as fh
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = CORPUS_NOW
                store.add_task("Fresh task", confidence=0.9,
                               extracted_at=now - 0.5 * 86400)
                store.add_task("Old task", confidence=0.9,
                               extracted_at=now - 5 * 86400)
                aging = fh.aging_open_work(store, now=now, threshold_days=2.0)
                ids = {a["id"] for a in aging}
                self.assertTrue(any("Old" in (a.get("text") or "")
                                    for a in aging))
                self.assertFalse(any("Fresh" in (a.get("text") or "")
                                     for a in aging))
                self.assertTrue(ids)
            finally:
                store.close()

    def test_aging_increases_gravity(self):
        """Open commitment with no activity gains gravity over time."""
        from app.services.ranking.scorer import GravityScorer
        from app.services.ranking.types import PipelineContext
        from app.services.field_history import aging_signal

        scorer = GravityScorer()
        base = {
            "id": "fact:1",
            "kind": "commitment",
            "label": "Follow up",
            "confidence": 0.9,
            "pinned": False,
            "why": [],
            "_feat_pros": 0.45,
            "_feat_rel": 0.0,
            "_feat_fut": 0.0,
            "_feat_unres": 0.7,
            "_feat_cent": 0.0,
            "_feat_sem": 0.35,
            "_feat_rep": 0.0,
            "_feat_temp": 0.2,
            "_feat_act": 0.0,
        }
        young = dict(base)
        young["_age"] = 1.0
        young["_feat_aging"] = aging_signal(1.0, kind="commitment")
        old = dict(base)
        old["_age"] = 10.0
        old["_feat_aging"] = aging_signal(10.0, kind="commitment")
        self.assertGreater(old["_feat_aging"], young["_feat_aging"])

        ctx = PipelineContext(now=CORPUS_NOW, mode=None, persist_wm=False)
        scorer.score([young], ctx)
        scorer.score([old], ctx)
        self.assertGreater(old["gravity"], young["gravity"])

    def test_aging_outranks_fresh_trivial(self):
        from app.services.ranking.pipeline import run as rank_run
        from app.services.ranking.scorer import GravityScorer
        from app.services.ranking.types import PipelineContext
        from app.services.field_history import aging_signal

        old = {
            "id": "fact:old", "kind": "commitment", "label": "Old promise",
            "confidence": 0.85, "pinned": False, "why": [],
            "_age": 14.0,
            "_feat_pros": 0.45, "_feat_rel": 0, "_feat_fut": 0,
            "_feat_unres": 0.7, "_feat_cent": 0, "_feat_sem": 0.35,
            "_feat_rep": 0, "_feat_temp": 0.15, "_feat_act": 0,
            "_feat_aging": aging_signal(14.0, kind="commitment"),
        }
        fresh = {
            "id": "fact:new", "kind": "task", "label": "Trivial new",
            "confidence": 0.99, "pinned": False, "why": [],
            "_age": 0.2,
            "_feat_pros": 0.25, "_feat_rel": 0, "_feat_fut": 0,
            "_feat_unres": 0.7, "_feat_cent": 0, "_feat_sem": 0.35,
            "_feat_rep": 0, "_feat_temp": 1.0, "_feat_act": 0,
            "_feat_aging": 0.0,
        }
        # Pad with people/entities so Admitter is happy.
        pads = []
        for i, kind in enumerate(
                ["person", "person", "tool", "project", "place"]):
            pads.append({
                "id": f"{'person' if kind == 'person' else 'entity'}:{i+1}",
                "kind": kind, "label": f"P{i}", "confidence": 0.8,
                "pinned": False, "why": [], "_age": 3.0,
                "_feat_pros": 0.1, "_feat_rel": 0.2, "_feat_fut": 0,
                "_feat_unres": 0, "_feat_cent": 0.2, "_feat_sem": 0.4,
                "_feat_rep": 0.1, "_feat_temp": 0.5, "_feat_act": 0,
                "_feat_aging": 0.0,
            })
        ctx = PipelineContext(
            now=CORPUS_NOW, focus_k=8, mode=None, persist_wm=False,
            wm_enabled=False)
        result = rank_run(
            [old, fresh] + pads, ctx=ctx, scorer=GravityScorer(),
            persist_wm=False)
        by_id = {n["id"]: n["gravity"] for n in result.ranked}
        self.assertGreater(by_id["fact:old"], by_id["fact:new"])

    def test_maybe_persist_on_version_change(self):
        from app.services import field_history as fh
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Ada Lovelace")
                field = {
                    "nodes": [
                        {"id": "person:1", "layer": "focus", "gravity": 0.5,
                         "kind": "person"},
                    ]
                }
                with mock.patch.object(store, "memory_version",
                                       return_value="ver-a"):
                    s1 = fh.maybe_persist_snapshot(store, field, now=CORPUS_NOW)
                    s2 = fh.maybe_persist_snapshot(store, field,
                                                   now=CORPUS_NOW + 10)
                self.assertIsNotNone(s1)
                self.assertIsNone(s2)  # same version — no duplicate
                with mock.patch.object(store, "memory_version",
                                       return_value="ver-b"):
                    s3 = fh.maybe_persist_snapshot(
                        store, field, now=CORPUS_NOW + 20)
                self.assertIsNotNone(s3)
            finally:
                store.close()

    def test_ambient_uses_field_diff_aging(self):
        from app.services import home_intelligence as hi
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = CORPUS_NOW
                store.add_task("Neglected thing", confidence=0.9,
                               extracted_at=now - 4 * 86400)
                with mock.patch("time.time", return_value=now):
                    data = hi.build(store)
                texts = [n["text"] for n in data.get("ambient") or []]
                self.assertTrue(
                    any("aging" in t.lower() for t in texts),
                    texts)
                aging_note = next(
                    n for n in data["ambient"] if "aging" in n["text"].lower())
                self.assertEqual(aging_note.get("source"), "field_diff.aging")
                self.assertTrue(aging_note.get("refs"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
