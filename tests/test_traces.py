"""Memory traces (Track A1) — B/V math, access plumbing, and the
priors-continuity contract (shadow must track shipped gravity)."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.services import traces


class ShadowPriorsInvariantTests(unittest.TestCase):
    """The logged shadow is the I-5 replay anchor: it must be computed at
    shipped priors no matter what the learning loop is doing. The drawn β
    reaches the ranked score only through the explicit `w=` override."""

    _KW = dict(kind="task", confidence=0.8, age_days=3.0, pinned=False,
               prospective=0.6, relationship=0.2, future=0.4, unresolved=0.3,
               centrality=0.1, repeats=0.2, b=0.5, v=0.4)

    def test_default_ignores_learning_entirely(self):
        from app.services import ranking_learn
        at_priors = traces.shadow_score(**self._KW)
        wild = {k: 5.0 for k in ranking_learn.FEATURES}
        with mock.patch.object(ranking_learn, "_learn_enabled",
                               return_value=True), \
             mock.patch.object(ranking_learn, "current_beta",
                               return_value=wild):
            self.assertEqual(traces.shadow_score(**self._KW), at_priors)

    def test_w_override_actually_overrides(self):
        from app.services import ranking_learn
        wild = {k: 5.0 for k in ranking_learn.FEATURES}
        self.assertNotEqual(traces.shadow_score(**self._KW, w=wild),
                            traces.shadow_score(**self._KW))

    def test_constellation_passes_drawn_beta_only_to_v2(self):
        """With learning ON, every node makes exactly two shadow_score calls:
        the logged shadow with NO weight override (priors), and the ranked v2
        with the drawn β. Verified by spying on the calls — no float drift."""
        from app.services import graph, ranking_learn
        from app.storage import Store

        now = time.time()
        wild = dict(ranking_learn.prior_beta())
        wild["pros"] = 0.0
        wild["temp"] = 3.0

        calls: list[dict] = []
        real = traces.shadow_score

        def spy(**kw):
            calls.append(dict(kw))
            return real(**kw)

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                pid = store.resolve_person("Ada")
                store.add_task("Send Ada the notes", confidence=0.9,
                               owner_person_id=pid, extracted_at=now - 86400)
                with mock.patch("app.services.activity.describe_recent",
                                return_value=[]), \
                     mock.patch.object(ranking_learn, "_learn_enabled",
                                       return_value=True), \
                     mock.patch.object(ranking_learn, "refresh_thompson",
                                       return_value=wild), \
                     mock.patch.object(ranking_learn, "current_beta",
                                       return_value=wild), \
                     mock.patch.object(traces, "shadow_score",
                                       side_effect=spy):
                    graph.constellation(store, limit=24)
            finally:
                store.close()

        priors_calls = [c for c in calls if c.get("w") is None]
        drawn_calls = [c for c in calls if c.get("w") == wild]
        # Every node scores the anchor shadow at priors AND ranks on the draw.
        self.assertGreaterEqual(len(priors_calls), 2)
        self.assertGreaterEqual(len(drawn_calls), 2)
        self.assertEqual(len(priors_calls), len(drawn_calls))
        # Nothing slips through with some third weight set.
        self.assertEqual(len(calls), len(priors_calls) + len(drawn_calls))


class BaseLevelMathTests(unittest.TestCase):
    def test_more_and_fresher_accesses_are_stronger(self):
        now = time.time()
        day = 86400.0
        one_old = traces.base_level([now - 30 * day], 0, None, now)
        one_fresh = traces.base_level([now - day], 0, None, now)
        many_fresh = traces.base_level(
            [now - 3 * day, now - 2 * day, now - day], 0, None, now)
        self.assertGreater(one_fresh, one_old)
        self.assertGreater(many_fresh, one_fresh)
        # The compressed tail adds strength too.
        with_tail = traces.base_level([now - day], 5, now - 40 * day, now)
        self.assertGreater(with_tail, one_fresh)

    def test_b_hat_bounds_and_empty_history(self):
        now = time.time()
        self.assertLess(traces.b_hat(traces.base_level([], 0, None, now)), 0.01)
        b = traces.b_hat(traces.base_level([now - 3600], 0, None, now))
        self.assertTrue(0.0 < b < 1.0)

    def test_fold_access_ring(self):
        now = time.time()
        recent, n_older, t_older = [], 0, None
        stamps = [now - i * 1000 for i in range(10, 0, -1)]  # 10 accesses
        for ts in stamps:
            recent, n_older, t_older = traces.fold_access(
                recent, n_older, t_older, ts)
        self.assertEqual(len(recent), traces.RECENT_K)
        self.assertEqual(n_older, 2)
        self.assertAlmostEqual(t_older, (stamps[0] + stamps[1]) / 2, places=3)
        self.assertEqual(recent, sorted(recent))  # newest kept, ordered

    def test_v_seed_and_bump(self):
        self.assertEqual(traces.v_seed("person"), 0.55)   # the old sem prior
        self.assertGreaterEqual(traces.v_seed("idea", pinned=True), 0.80)
        self.assertGreaterEqual(traces.v_seed("tool", profiled=True), 0.60)
        v = traces.v_bump(0.95, "pin")
        self.assertLessEqual(v, traces.V_MAX)
        v = traces.v_bump(0.1, "hide")
        self.assertGreaterEqual(v, traces.V_MIN)

    def test_kendall_tau(self):
        self.assertEqual(traces.kendall_tau([1, 2, 3], [10, 20, 30]), 1.0)
        self.assertEqual(traces.kendall_tau([1, 2, 3], [30, 20, 10]), -1.0)
        self.assertIsNone(traces.kendall_tau([1], [2]))


class TraceStoreTests(unittest.TestCase):
    def test_access_recording_and_value_bump(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                for i in range(10):
                    store.record_node_access("person", 7, time.time() - i)
                row = store.node_dynamics_map([("person", 7)])[("person", 7)]
                self.assertEqual(len(json.loads(row["access_recent"])), 8)
                self.assertEqual(row["access_n_older"], 2)
                store.bump_node_value("person", 7, "pin")
                row = store.node_dynamics_map([("person", 7)])[("person", 7)]
                self.assertAlmostEqual(row["V"], traces.V_DEFAULT + 0.15,
                                       places=4)
            finally:
                store.close()

    def test_touch_fact_records_access(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                fid = store.add_task("Ship the traces layer", confidence=0.9,
                                     extracted_at=time.time())
                store.touch_fact(fid, time.time())
                dyn = store.node_dynamics_map([("fact", fid)])
                self.assertIn(("fact", fid), dyn)
            finally:
                store.close()

    def test_seed_never_overwrites_live_rows(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.bump_node_value("entity", 3, "pin")   # live row, V=0.50
                self.assertFalse(store.seed_node_dynamics(
                    "entity", 3, v=0.2, access=[time.time()]))
                row = store.node_dynamics_map([("entity", 3)])[("entity", 3)]
                self.assertAlmostEqual(row["V"], 0.50, places=4)
            finally:
                store.close()

    def test_verdict_join_bumps_value(self):
        from app.services.attention_ledger import AttentionLedger
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = AttentionLedger()
                led.record_grounding([4], [], store)
                with mock.patch(
                        "app.services.escalate_log.escalate_log.row_by_id",
                        return_value={"id": "r1", "time": time.time()}):
                    led.close_grounding_for_row("r1", "accepted", store=store)
                row = store.node_dynamics_map([("person", 4)])[("person", 4)]
                self.assertGreater(row["V"], traces.V_DEFAULT)
            finally:
                store.close()


class PriorsContinuityTests(unittest.TestCase):
    """Invariant I-5: at shipped priors the shadow must rank like gravity."""

    def test_shadow_tracks_gravity_on_a_mixed_field(self):
        from app.services import graph
        from app.services.attention_ledger import attention_ledger
        from app.storage import Store

        now = time.time()
        day = 86400.0
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                for name, age_d in (("Justin Adorante", 0.5), ("Marc Chen", 5),
                                    ("Abby Nangle", 40)):
                    pid = store.resolve_person(name)
                    store.touch_person(pid, now - age_d * day)
                store.resolve_entity("Atlas", kind="project")
                store.resolve_entity("GitHub", kind="tool")
                for i, (text, conf, age_d) in enumerate((
                        ("Send Marc the term sheet", 0.9, 0.2),
                        ("Book flights for the offsite", 0.7, 3),
                        ("Refactor the extractor", 0.5, 12),
                        ("Water the plants", 0.4, 30))):
                    store.add_task(text, confidence=conf,
                                   extracted_at=now - age_d * day)
                attention_ledger._last.clear()
                attention_ledger._snapshot_id = None
                with mock.patch("app.services.activity.describe_recent",
                                return_value=[]):
                    graph.constellation(store, limit=24,
                                        record_impressions=True)
                with store._lock:
                    rows = store._conn.execute(
                        "SELECT score, decomposition FROM attention_impressions "
                        "WHERE surface = 'field'").fetchall()
                pairs = []
                for r in rows:
                    d = json.loads(r["decomposition"])
                    self.assertIsNotNone(d.get("shadow"),
                                         "every impression carries the shadow")
                    pairs.append((float(r["score"]), float(d["shadow"])))
                self.assertGreaterEqual(len(pairs), 6)
                tau = traces.kendall_tau([p[0] for p in pairs],
                                         [p[1] for p in pairs])
                self.assertIsNotNone(tau)
                self.assertGreaterEqual(
                    tau, 0.6,
                    f"priors-continuity gate failed: tau={tau:.3f}")
            finally:
                store.close()


class AttentionReplayServiceTests(unittest.TestCase):
    def test_run_persists_and_status(self):
        from app.services import attention_replay, graph, traces_backfill
        from app.services.attention_ledger import attention_ledger
        from app.storage import Store

        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                for name in ("Ada", "Bea", "Cara", "Dan"):
                    store.resolve_person(name)
                    store.touch_person(store.find_person_exact(name), now)
                for i in range(4):
                    store.add_task(f"Task {i}", confidence=0.8,
                                   extracted_at=now - i * 100)
                traces_backfill.run(store=store)
                counts = store.node_dynamics_counts()
                self.assertGreaterEqual(counts["total"], 8)
                attention_ledger._last.clear()
                attention_ledger._snapshot_id = None
                # Pin the v2 flag off so the observe-only assertions don't
                # depend on the developer's live .env (QUILL_FIELD_V2).
                with mock.patch("app.services.activity.describe_recent",
                                return_value=[]), \
                     mock.patch("app.services.graph._field_v2_enabled",
                                return_value=False):
                    graph.constellation(store, limit=24,
                                        record_impressions=True)
                    result = attention_replay.run(days=1, gate=0.6, store=store)
                    self.assertIn(result["status"],
                                  ("pass", "fail", "insufficient"))
                    if result["renders"] >= 1:
                        self.assertIsNotNone(result["mean_tau"])
                    last = store.last_attention_replay_run()
                    self.assertIsNotNone(last)
                    self.assertEqual(last["status"], result["status"])
                    st = attention_replay.status(store=store)
                self.assertTrue(st["observe_only"])
                self.assertFalse(st["field_v2"])
                self.assertGreaterEqual(st["traces"]["total"], 8)
                self.assertFalse(attention_replay.due_for(store=store))
            finally:
                store.close()

    def test_insufficient_when_ledger_empty(self):
        from app.services import attention_replay
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                result = attention_replay.run(days=1, store=store)
                self.assertEqual(result["status"], "insufficient")
                self.assertIsNone(result["passed"])
            finally:
                store.close()

    def test_add_task_records_creation_access(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                fid = store.add_task("Seeded on create", confidence=0.9,
                                     extracted_at=time.time())
                dyn = store.node_dynamics_map([("fact", fid)])
                self.assertIn(("fact", fid), dyn)
                recent = json.loads(dyn[("fact", fid)]["access_recent"] or "[]")
                self.assertGreaterEqual(len(recent), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
