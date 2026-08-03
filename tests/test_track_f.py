"""Track F — heuristic predictor baselines, walk-forward bench + promote
gate, and hardening (restore drill, kill-switch audit).

Discipline under test: the bench never lets a scorer peek past its decision
point; promote only activates a candidate that beats the active model on the
held-out window; rollback always works; the restore drill verifies a COPY and
leaves the live store untouched.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.storage import Store

NOW = time.time()
HOUR = 3600.0
DAY = 86400.0


def _mk_store(tmp) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio")


def _block(start: float, app: str) -> SimpleNamespace:
    return SimpleNamespace(start=start, end=start + 600, app=app,
                           windows=[], summary=f"{app} focus", event_ids=[],
                           n_screens=1, n_clicks=0, n_audio=0, n_webcam=0,
                           ctx_event_ids=[])


def _alternating_blocks(n: int, *, until: float, spacing_s: float = 4 * HOUR,
                        apps=("chrome", "code")) -> list[SimpleNamespace]:
    """n blocks ending at `until`, strictly alternating apps[0]/apps[1]."""
    out = []
    for i in range(n):
        start = until - (n - i) * spacing_s
        out.append(_block(start, apps[i % 2]))
    return out


class ScorerTests(unittest.TestCase):
    def test_transition_dominates_next_app(self):
        from app.services.predictors import score_next_app
        blocks = [vars(b) for b in _alternating_blocks(30, until=NOW)]
        ranked = score_next_app(blocks, now=NOW, prev_app="chrome")
        self.assertTrue(ranked)
        self.assertEqual(ranked[0][0], "code")   # chrome -> code, always

    def test_prev_app_and_desktop_excluded(self):
        from app.services.predictors import score_next_app
        blocks = [vars(b) for b in _alternating_blocks(20, until=NOW)]
        blocks.append(vars(_block(NOW - 100, "desktop")))
        ranked = score_next_app(blocks, now=NOW, prev_app="chrome")
        names = [n for n, _ in ranked]
        self.assertNotIn("chrome", names)
        self.assertNotIn("desktop", names)

    def test_contact_frequency_recency_and_boost(self):
        from app.services.predictors import score_next_contact
        inter = ([(NOW - 30 * DAY, 1)] * 5          # old but frequent
                 + [(NOW - 1 * HOUR, 2)])           # single, very recent
        ranked = score_next_contact(inter, now=NOW)
        self.assertEqual(ranked[0][0], 2)           # recency wins here
        boosted = score_next_contact(inter, now=NOW, boosts={3: 0.5})
        self.assertIn(3, [p for p, _ in boosted])   # calendar attendee appears

    def test_document_scorer_orders_by_freq_recency(self):
        from app.services.predictors import score_next_document
        opens = [(NOW - 2 * DAY, "a.pdf"), (NOW - 1 * DAY, "a.pdf"),
                 (NOW - 20 * DAY, "b.docx")]
        ranked = score_next_document(opens, now=NOW)
        self.assertEqual(ranked[0][0], "a.pdf")

    def test_no_history_is_empty_not_error(self):
        from app.services import predictors as P
        self.assertEqual(P.score_next_app([], now=NOW), [])
        self.assertEqual(P.score_next_contact([], now=NOW), [])
        self.assertEqual(P.score_next_document([], now=NOW), [])


class BenchTests(unittest.TestCase):
    def test_walk_forward_replay_scores_perfect_pattern(self):
        from app.services import predictor_bench as B
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                # 90 blocks over 15 days; last 7 days is the holdout.
                store.replace_activities(_alternating_blocks(90, until=NOW))
                res = B.run("next_app", store, now=NOW)
                row = res["tasks"]["next_app"]
                self.assertEqual(row["status"], "ok")
                self.assertGreaterEqual(row["n_points"], 20)
                self.assertGreaterEqual(row["hit1"], 0.99)  # pattern is exact
                self.assertGreaterEqual(row["mrr"], 0.99)
                self.assertIsNotNone(store.last_predictor_bench_run("next_app"))
                self.assertFalse(B.due_for(store))
            finally:
                store.close()

    def test_insufficient_when_thin(self):
        from app.services import predictor_bench as B
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                store.replace_activities(_alternating_blocks(6, until=NOW,
                                                             spacing_s=HOUR))
                res = B.run("next_app", store, now=NOW)
                self.assertEqual(res["tasks"]["next_app"]["status"],
                                 "insufficient")
            finally:
                store.close()

    def test_registry_seeds_heuristic_active(self):
        from app.services import predictors as P
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                P.ensure_registry(store)
                for task in P.TASKS:
                    m = store.active_predictor_model(task)
                    self.assertEqual(m["version"], P.HEURISTIC_VERSION)
                    self.assertEqual(m["kind"], "heuristic")
                # idempotent
                P.ensure_registry(store)
                self.assertEqual(
                    len([r for r in store.predictor_model_history("next_app")
                         if r["active"]]), 1)
            finally:
                store.close()

    def test_promote_holds_without_candidate_then_promotes_then_rolls_back(self):
        from app.services import predictor_bench as B, predictors as P
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                P.ensure_registry(store)
                r = B.promote("next_app", store)
                self.assertEqual(r["status"], "hold")
                self.assertEqual(r["reason"], "no_candidate")

                # An 'ok' bench for the active heuristic at hit3=0.5 ...
                store.add_predictor_bench_run({
                    "ts": NOW, "task": "next_app",
                    "model": P.HEURISTIC_VERSION, "status": "ok",
                    "n_points": 40, "hit1": 0.4, "hit3": 0.5, "mrr": 0.45})
                # ... a weak candidate holds ...
                store.save_predictor_model(
                    task="next_app", version="learned-weak", kind="learned",
                    metrics={"hit3": 0.3, "mrr": 0.2}, activate=False)
                r = B.promote("next_app", store)
                self.assertEqual(r["status"], "hold")
                self.assertEqual(r["reason"], "does_not_beat_active")
                # ... a strong one promotes ...
                store.save_predictor_model(
                    task="next_app", version="learned-strong", kind="learned",
                    metrics={"hit3": 0.8, "mrr": 0.7}, activate=False)
                r = B.promote("next_app", store)
                self.assertEqual(r["status"], "promoted")
                self.assertEqual(
                    store.active_predictor_model("next_app")["version"],
                    "learned-strong")
                # ... and rollback restores the heuristic.
                r = B.rollback("next_app", store)
                self.assertEqual(r["status"], "rolled_back")
                self.assertEqual(
                    store.active_predictor_model("next_app")["version"],
                    P.HEURISTIC_VERSION)
            finally:
                store.close()


class HardeningTests(unittest.TestCase):
    def test_restore_drill_verifies_copy_and_persists(self):
        from app.services import hardening
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                store.resolve_person("Ada")
                store.add_task("t", confidence=0.8, extracted_at=NOW)
                res = hardening.restore_drill(store, now=NOW)
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["integrity"], "ok")
                self.assertEqual(res["mismatches"], {})
                self.assertGreaterEqual(res["counts"]["facts"], 1)
                last = store.last_hardening_run(kind="restore_drill")
                self.assertTrue(last["ok"])
                self.assertFalse(hardening.due_for_drill(store))
            finally:
                store.close()

    def test_kill_switch_audit_shape(self):
        from app.services.hardening import kill_switches
        rows = kill_switches()
        envs = {r["env"] for r in rows}
        self.assertIn("QUILL_COMPACTION", envs)
        self.assertIn("QUILL_FIELD_V2", envs)
        for r in rows:
            self.assertIsInstance(r["on"], bool)
            self.assertIsInstance(r["non_default"], bool)

    def test_battery_best_effort(self):
        from app.services.hardening import battery
        b = battery()
        if b is not None:
            self.assertIn("percent", b)


class TrackFEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = _mk_store(self.tmp)
        self.addCleanup(self.store.close)
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_predictors_console_and_bench(self):
        self.store.replace_activities(_alternating_blocks(90, until=NOW))
        r = self.client.post("/console/predictors/bench?task=next_app")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tasks"]["next_app"]["status"], "ok")
        st = self.client.get("/console/predictors").json()
        self.assertTrue(st["enabled"])
        self.assertEqual(
            st["tasks"]["next_app"]["active"]["version"], "heuristic-v1")
        self.assertTrue(st["tasks"]["next_app"]["preview"])

    def test_promote_and_rollback_endpoints(self):
        r = self.client.post("/console/predictors/promote?task=next_app")
        self.assertEqual(r.json()["status"], "hold")
        r = self.client.post("/console/predictors/rollback?task=next_app")
        self.assertEqual(r.json()["status"], "hold")

    def test_hardening_console_and_drill(self):
        r = self.client.post("/console/hardening/drill")
        self.assertTrue(r.json()["ok"])
        st = self.client.get("/console/hardening").json()
        self.assertTrue(st["last_drill"]["ok"])
        self.assertFalse(st["drill_due"])
        self.assertTrue(any(s["env"] == "QUILL_WM"
                            for s in st["kill_switches"]))
        self.assertTrue(any(s["env"] == "QUILL_REASONERS"
                            for s in st["kill_switches"]))


if __name__ == "__main__":
    unittest.main()
