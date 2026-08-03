"""Spreading activation + Now-Context (Track A2).

Contracts: conductance respects edge class and PMI; propagation is damped and
conserving (a hub spreads thinner, never brighter); context seeds decay; and
under QUILL_FIELD_V2 the field responds to context — seeding a person lights
their obligations — while an EMPTY context keeps v2 rank-consistent with v1
(the continuity invariant, now for the flag itself)."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.services import activation
from app.services.now_context import NowContext


def _rel(i, s_t, s_i, pred, o_t, o_i, *, w=1.0, conf=0.8):
    return {"id": i, "subj_type": s_t, "subj_id": s_i, "predicate": pred,
            "obj_type": o_t, "obj_id": o_i, "weight": w, "confidence": conf,
            "origin": "derived", "created_at": time.time()}


class EdgeDynamicsTests(unittest.TestCase):
    def test_class_priors_order_conductance(self):
        rows = activation.compute_edge_dynamics([
            _rel(1, "person", 1, "responsible_for", "fact", 10),
            _rel(2, "fact", 10, "evidenced_by", "event", 99),
            _rel(3, "person", 1, "linked", "entity", 5),
            _rel(4, "person", 1, "mentioned_in", "fact", 11),
        ])
        by_id = {r["relation_id"]: r for r in rows}
        self.assertGreater(by_id[3]["conductance"],       # user
                           by_id[1]["conductance"])       # > obligation
        self.assertGreater(by_id[1]["conductance"],       # obligation
                           by_id[4]["conductance"])       # > aboutness
        self.assertGreater(by_id[4]["conductance"],       # aboutness
                           by_id[2]["conductance"])       # > provenance

    def test_self_loops_and_bookkeeping_never_conduct(self):
        rows = activation.compute_edge_dynamics([
            _rel(1, "person", 1, "pins", "person", 1),
            _rel(2, "entity", 3, "constellation_hidden", "entity", 3),
            _rel(3, "person", 2, "co_occurs", "person", 2),   # self loop
        ])
        self.assertEqual(rows, [])

    def test_pmi_rewards_exclusive_pairs_over_popular_hubs(self):
        rows = [
            _rel(1, "person", 1, "co_occurs", "person", 2, w=4),   # A-B
            _rel(2, "person", 2, "co_occurs", "person", 1, w=4),
        ]
        rid = 3
        for other in range(3, 9):                                   # hub H=99
            rows.append(_rel(rid, "person", 99, "co_occurs",
                             "person", other, w=4)); rid += 1
            rows.append(_rel(rid, "person", other, "co_occurs",
                             "person", 99, w=4)); rid += 1
        out = {r["relation_id"]: r for r in
               activation.compute_edge_dynamics(rows)}
        self.assertGreater(out[1]["conductance"], out[3]["conductance"])
        self.assertGreater(out[1]["pmi"], out[3]["pmi"])

    def test_edge_age_decays_conductance(self):
        now = time.time()
        fresh = _rel(1, "person", 1, "responsible_for", "fact", 10)
        fresh["created_at"] = now
        old = _rel(2, "person", 1, "responsible_for", "fact", 11)
        old["created_at"] = now - 90 * 86400  # ~2 half-lives of 45d
        out = {r["relation_id"]: r for r in
               activation.compute_edge_dynamics([fresh, old], now=now)}
        self.assertGreater(out[1]["conductance"], out[2]["conductance"])


class PropagateTests(unittest.TestCase):
    def _adj(self):
        return {
            ("person", 1): [(("fact", 10), 1.4), (("person", 2), 0.7)],
            ("fact", 10): [(("person", 1), 1.4), (("entity", 5), 0.8)],
            ("person", 2): [(("person", 1), 0.7)],
            ("entity", 5): [(("fact", 10), 0.8)],
        }

    def test_two_hops_reach_and_attenuate(self):
        a = activation.propagate({("person", 1): 1.0}, self._adj())
        self.assertAlmostEqual(a[("person", 1)], 0.6, delta=0.15)
        self.assertGreater(a[("fact", 10)], a[("entity", 5)])
        self.assertIn(("entity", 5), a)
        self.assertLessEqual(sum(a.values()), 2.0)

    def test_hub_spreads_thinner_not_brighter(self):
        hub = {("person", 9): [(("fact", i), 1.0) for i in range(8)]}
        for i in range(8):
            hub[("fact", i)] = [(("person", 9), 1.0)]
        a = activation.propagate({("person", 9): 1.0}, hub)
        per_neighbor = a.get(("fact", 0), 0.0)
        self.assertLess(per_neighbor, 0.12)

    def test_empty_seeds_empty_result(self):
        self.assertEqual(activation.propagate({}, self._adj()), {})


class NowContextTests(unittest.TestCase):
    def test_observe_decay_and_generation(self):
        ctx = NowContext()
        t0 = time.time()
        ctx.observe([("person", 1)], weight=1.0, now=t0)
        g1 = ctx.generation
        self.assertAlmostEqual(ctx.seeds(now=t0)[("person", 1)], 1.0, places=3)
        w = ctx.seeds(now=t0 + 20 * 60)[("person", 1)]
        self.assertAlmostEqual(w, 0.368, delta=0.05)
        self.assertEqual(ctx.seeds(now=t0 + 4 * 3600), {})
        ctx.observe([("fact", 2)], now=t0)
        self.assertGreater(ctx.generation, g1)

    def test_reobserve_takes_max_not_sum(self):
        ctx = NowContext()
        t0 = time.time()
        ctx.observe([("person", 1)], weight=1.0, now=t0)
        ctx.observe([("person", 1)], weight=0.3, now=t0 + 60)
        self.assertLessEqual(ctx.seeds(now=t0 + 60)[("person", 1)], 1.0)

    def test_trim_keeps_strongest(self):
        ctx = NowContext()
        t0 = time.time()
        ctx.observe([("fact", i) for i in range(80)], weight=0.5, now=t0)
        ctx.observe([("person", 1)], weight=1.0, now=t0)
        seeds = ctx.seeds(now=t0)
        self.assertLessEqual(len(seeds), 64)
        self.assertIn(("person", 1), seeds)


class ContextFeederTests(unittest.TestCase):
    def test_speech_and_calendar_seed_known_people(self):
        from app.services import context_feeder
        from app.services.now_context import now_context
        from app.storage import Store
        from app.events import Event, Modality
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                pid = store.resolve_person("Scott Reeves", ts=now)
                store.replace_turns([SimpleNamespace(
                    start=now - 60, end=now - 30, speaker="me",
                    text="I owe Scott Reeves the intro",
                    n_utterances=1, event_ids=[], audio_paths=[],
                )])
                store.insert(Event(
                    time=now, modality=Modality.SYSTEM,
                    raw="Calendar (Home): Scott Reeves — term sheet",
                    summary="[calendar] Scott Reeves",
                    source="phone.calendar",
                    meta={"start": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                 time.localtime(now + 1800)),
                          "origin": "icloud"},
                ))
                now_context.clear()
                n_speech = context_feeder.feed_from_speech(store, now=now)
                self.assertGreaterEqual(n_speech, 1)
                self.assertIn(("person", pid), now_context.seeds(now=now))
                now_context.clear()
                n_cal = context_feeder.feed_from_calendar(store, now=now)
                self.assertGreaterEqual(n_cal, 1)
                self.assertIn(("person", pid), now_context.seeds(now=now))
            finally:
                now_context.clear()
                store.close()

    def test_replay_uses_g1_when_v2_score_diverges(self):
        from app.services import attention_replay
        rows = [{
            "ts": time.time(), "context_id": 1, "score": 0.99,
            "decomposition": json.dumps({
                "g1": 0.5, "shadow": 0.52, "v2": 0.99, "act": 0.8,
            }),
        }, {
            "ts": time.time(), "context_id": 1, "score": 0.10,
            "decomposition": json.dumps({
                "g1": 0.4, "shadow": 0.41, "v2": 0.10, "act": 0.0,
            }),
        }, {
            "ts": time.time(), "context_id": 1, "score": 0.20,
            "decomposition": json.dumps({
                "g1": 0.3, "shadow": 0.31, "v2": 0.20, "act": 0.0,
            }),
        }, {
            "ts": time.time(), "context_id": 1, "score": 0.05,
            "decomposition": json.dumps({
                "g1": 0.2, "shadow": 0.22, "v2": 0.05, "act": 0.0,
            }),
        }]
        scored = attention_replay.score_renders(rows, min_nodes=4)
        self.assertEqual(scored["renders"], 1)
        self.assertGreaterEqual(scored["mean_tau"], 0.9)


class FieldV2Tests(unittest.TestCase):
    def _store(self, td):
        from app.storage import Store
        now = time.time()
        store = Store(Path(td) / "t.db")
        pid = store.resolve_person("Scott Reeves")
        store.resolve_person("Marc Chen")
        store.resolve_entity("Atlas", kind="project")
        tid = store.add_task("Send Scott Reeves the term sheet",
                             confidence=0.9, owner_person_id=pid,
                             extracted_at=now - 3 * 86400)
        for i in range(3):
            store.add_task(f"Unrelated chore {i}", confidence=0.8,
                           extracted_at=now - 600)
        return store, pid, tid

    def test_context_lights_the_neighborhood(self):
        from app.services import graph
        from app.services.attention_ledger import attention_ledger
        from app.services.now_context import now_context

        with tempfile.TemporaryDirectory() as td:
            store, pid, tid = self._store(td)
            try:
                now_context.clear()
                attention_ledger._last.clear()
                # WM off: this test asserts v2 *scoring* (activation lights
                # the neighborhood); MMR cluster-collapse would absorb the
                # task into a chore cluster and hide it from the node list.
                # Learning off too: QUILL_ATTENTION_LEARN=1 makes each render
                # redraw a Thompson β, so base-vs-lit deltas would be noise.
                with mock.patch("app.services.graph._field_v2_enabled",
                                return_value=True), \
                     mock.patch("app.services.working_memory._wm_enabled",
                                return_value=False), \
                     mock.patch("app.services.ranking_learn._learn_enabled",
                                return_value=False), \
                     mock.patch("app.services.activity.describe_recent",
                                return_value=[]):
                    graph.rebuild(store)
                    base = graph.constellation(store, limit=24)
                    base_g = {n["id"]: n["gravity"] for n in base["nodes"]}
                    now_context.observe([("person", pid)], weight=1.0)
                    lit = graph.constellation(store, limit=24)
                    lit_n = {n["id"]: n for n in lit["nodes"]}
                scott = f"person:{pid}"
                task = f"fact:{tid}"
                self.assertIn(task, lit_n)
                self.assertGreater(lit_n[scott]["gravity"], base_g[scott])
                self.assertGreater(lit_n[task]["gravity"], base_g[task])
                self.assertIn("Lit by what you're doing right now",
                              lit_n[scott]["why"])
            finally:
                now_context.clear()
                store.close()

    def test_flag_on_with_empty_context_stays_rank_consistent(self):
        from app.services import graph
        from app.services.now_context import now_context
        from app.services.traces import kendall_tau

        with tempfile.TemporaryDirectory() as td:
            store, _pid, _tid = self._store(td)
            try:
                now_context.clear()
                # WM off: rank-consistency compares raw v1 vs v2 scoring;
                # MMR/hysteresis selection would shrink and reorder the list.
                with mock.patch("app.services.activity.describe_recent",
                                return_value=[]), \
                     mock.patch("app.services.working_memory._wm_enabled",
                                return_value=False), \
                     mock.patch("app.services.ranking_learn._learn_enabled",
                                return_value=False):
                    graph.rebuild(store)
                    with mock.patch("app.services.graph._field_v2_enabled",
                                    return_value=False):
                        v1 = graph.constellation(store, limit=24)
                    with mock.patch("app.services.graph._field_v2_enabled",
                                    return_value=True):
                        v2 = graph.constellation(store, limit=24)
                g1 = {n["id"]: n["gravity"] for n in v1["nodes"]}
                g2 = {n["id"]: n["gravity"] for n in v2["nodes"]}
                shared = [i for i in g1 if i in g2]
                self.assertGreaterEqual(len(shared), 5)
                tau = kendall_tau([g1[i] for i in shared],
                                  [g2[i] for i in shared])
                self.assertGreaterEqual(
                    tau, 0.6, f"v2-at-empty-context diverged: tau={tau:.3f}")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
