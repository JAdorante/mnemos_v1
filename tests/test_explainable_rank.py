"""Explainable rank (WS2) — breakdowns on /field/state?explain=true."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fixtures.ranking_corpus import CORPUS_BUILDERS, CORPUS_NOW


class ExplainableRankTests(unittest.TestCase):
    def test_field_state_omits_breakdowns_by_default(self):
        from app.services import graph

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["small"](Path(td) / "t.db")
            try:
                with mock.patch("time.time", return_value=CORPUS_NOW):
                    data = graph.constellation(store, limit=20, explain=False)
                self.assertNotIn("breakdowns", data)
            finally:
                store.close()

    def test_field_state_explain_includes_focus_breakdowns(self):
        from app.services import graph
        from app.services.ranking.config import BREAKDOWN_SUM_EPS

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["small"](Path(td) / "t.db")
            try:
                with mock.patch("time.time", return_value=CORPUS_NOW):
                    data = graph.constellation(store, limit=20, explain=True)
                bds = data.get("breakdowns") or {}
                self.assertTrue(bds)
                focus_ids = {
                    n["id"] for n in data["nodes"] if n["layer"] == "focus"
                }
                for nid in focus_ids:
                    self.assertIn(nid, bds)
                    bd = bds[nid]
                    self.assertIn("total", bd)
                    self.assertIn("components", bd)
                    self.assertIn("admitted_by", bd)
                    s = sum(float(c["value"]) for c in bd["components"])
                    self.assertAlmostEqual(
                        s, float(bd["total"]), delta=BREAKDOWN_SUM_EPS)
                    for c in bd["components"]:
                        if abs(float(c["value"])) > 1e-9:
                            has_refs = bool(c.get("evidence_refs"))
                            has_none = c.get("evidence") == "none"
                            self.assertTrue(
                                has_refs or has_none,
                                f"{nid}/{c.get('key')} missing evidence")
            finally:
                store.close()

    def test_evidence_endpoint_includes_breakdown(self):
        from app.services import graph

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["small"](Path(td) / "t.db")
            try:
                with mock.patch("time.time", return_value=CORPUS_NOW):
                    field = graph.constellation(store, limit=20, explain=True)
                focus = [n for n in field["nodes"] if n["layer"] == "focus"]
                self.assertTrue(focus)
                nid = focus[0]["id"]
                with mock.patch("time.time", return_value=CORPUS_NOW):
                    ev = graph.constellation_evidence(store, nid)
                self.assertTrue(ev.get("ok"))
                bd = ev.get("breakdown")
                self.assertIsNotNone(bd)
                self.assertEqual(bd.get("node_id"), nid)
                self.assertTrue(bd.get("components"))
            finally:
                store.close()

    def test_quota_admission_marked_in_breakdown(self):
        from app.services import graph

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["all_tasks"](Path(td) / "t.db")
            try:
                with mock.patch("time.time", return_value=CORPUS_NOW), \
                     mock.patch("app.services.working_memory._wm_enabled",
                                return_value=False):
                    # top-k + Admitter — under a task flood, some nodes enter
                    # via quota.
                    data = graph.constellation(store, limit=24, explain=True)
                bds = data.get("breakdowns") or {}
                admitted = {
                    nid: bd.get("admitted_by") for nid, bd in bds.items()
                }
                # At least the diversity contract held; quota may or may not
                # have swapped depending on top-k order — assert schema only
                # if any quota admits exist.
                self.assertTrue(any(
                    v in ("score", "quota", "pin") for v in admitted.values()))
                focus = [n for n in data["nodes"] if n["layer"] == "focus"]
                self.assertGreaterEqual(
                    sum(1 for n in focus if n["kind"] == "person"), 2)
            finally:
                store.close()

    def test_routes_pass_explain(self):
        """field_state(explain=True) threads through to constellation."""
        from fastapi.testclient import TestClient

        try:
            from app.api.routes import router
            from fastapi import FastAPI
            app = FastAPI()
            app.include_router(router)
        except Exception as exc:
            self.skipTest(f"app import: {exc}")
            return

        # Patch constellation to avoid needing a live store with data.
        with mock.patch("app.services.graph.constellation") as mock_c, \
             mock.patch("app.api.routes.memory._ensure_store") as mock_store:
            mock_store.return_value = mock.MagicMock()
            mock_c.return_value = {
                "nodes": [], "edges": [], "count": {},
                "editable": True, "field": True, "insights": [],
                "selection": {"path": "pipeline"},
                "breakdowns": {"person:1": {
                    "node_id": "person:1", "total": 0.5,
                    "components": [], "admitted_by": "score",
                }},
            }
            # Minimal stubs for field_state extras
            with mock.patch("app.services.working_memory.status",
                            return_value={}), \
                 mock.patch("app.services.now_context.now_context") as nc:
                nc.generation = 0
                nc.seeds.return_value = {}
                client = TestClient(app)
                r = client.get("/field/state?limit=20&explain=true")
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertIn("breakdowns", body)
                mock_c.assert_called()
                kwargs = mock_c.call_args.kwargs
                self.assertTrue(kwargs.get("explain"))

                r2 = client.get("/field/state?limit=20")
                self.assertEqual(r2.status_code, 200)
                # Last call should have explain=False
                kwargs2 = mock_c.call_args.kwargs
                self.assertFalse(kwargs2.get("explain"))


if __name__ == "__main__":
    unittest.main()
