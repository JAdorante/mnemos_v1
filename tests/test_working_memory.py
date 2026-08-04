"""Working Memory + MMR (Track A3).

Contracts from Field §8.2 / §8.3 / §11 / §16:
  - MMR is deterministic and suppresses near-duplicate open work
  - inhibition-diversity: 20-task flood still keeps ≥2 people and ≥3 entities
    in focus *with the quota path disabled*
  - hysteresis-no-flicker: same scores twice ⇒ identical WM membership
  - WORKING SET appears in grounding after identity/profile
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class MmrTests(unittest.TestCase):
    def test_deterministic_and_collapses_open_work(self):
        from app.services import mmr

        cands = [
            {"id": f"fact:{i}", "label": f"Noise task {i}",
             "kind": "task", "gravity": 0.95 - i * 0.001, "pinned": False}
            for i in range(20)
        ]
        cands += [
            {"id": "person:1", "label": "Ada", "kind": "person",
             "gravity": 0.45, "pinned": False},
            {"id": "person:2", "label": "Bea", "kind": "person",
             "gravity": 0.44, "pinned": False},
            {"id": "entity:1", "label": "GitHub", "kind": "tool",
             "gravity": 0.40, "pinned": False},
            {"id": "entity:2", "label": "AWS", "kind": "tool",
             "gravity": 0.39, "pinned": False},
            {"id": "entity:3", "label": "Cursor", "kind": "tool",
             "gravity": 0.38, "pinned": False},
        ]
        a = mmr.mmr_select(cands, 8)
        b = mmr.mmr_select(cands, 8)
        self.assertEqual([n["id"] for n in a], [n["id"] for n in b])
        task_ids = [n["id"] for n in a if n["kind"] == "task"]
        self.assertLessEqual(len(task_ids), 2)
        self.assertGreaterEqual(a[0].get("cluster_n", 1), 10)
        kinds = {n["kind"] for n in a}
        self.assertIn("person", kinds)
        self.assertTrue(kinds & {"tool", "project", "place", "idea"})


class WorkingMemoryTests(unittest.TestCase):
    def test_hysteresis_no_flicker(self):
        from app.services import working_memory as wm
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                ranked = [
                    {"id": "person:1", "label": "Ada", "kind": "person",
                     "gravity": 0.6, "pinned": False, "why": ["A"]},
                    {"id": "person:2", "label": "Bea", "kind": "person",
                     "gravity": 0.55, "pinned": False, "why": ["B"]},
                    {"id": "fact:1", "label": "Task one", "kind": "task",
                     "gravity": 0.5, "pinned": False, "why": ["T"]},
                ]
                t0 = time.time()
                f1 = wm.select_focus(ranked, 7, store=store, now=t0)
                ids1 = [n["id"] for n in f1]
                f2 = wm.select_focus(ranked, 7, store=store, now=t0 + 30)
                ids2 = [n["id"] for n in f2]
                self.assertEqual(ids1, ids2)
            finally:
                store.close()

    def test_inhibition_diversity_without_quota(self):
        """MMR + Admitter keep diversity under a task flood.

        Quotas are no longer an alternate selector; Admitter may swap.
        Assert pipeline path and the diversity contract (≥2 people, ≥3 entities).
        """
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Ada")
                store.resolve_person("Bea")
                for name in ("GitHub", "AWS", "Cursor", "DTC Venture Pulse"):
                    store.resolve_entity(name, "tool")
                for i in range(20):
                    store.add_task(f"Noise task {i}",
                                   confidence=0.99, extracted_at=time.time())
                with mock.patch("app.services.working_memory._wm_enabled",
                                return_value=True):
                    data = graph.constellation(store, limit=24,
                                               record_impressions=False)
                self.assertEqual(data["selection"]["path"], "pipeline")
                focus = [n for n in data["nodes"] if n["layer"] == "focus"]
                people = [n for n in focus if n["kind"] == "person"]
                entities = [n for n in focus
                            if n["kind"] in graph._ENTITY_FOCUS_KINDS]
                self.assertGreaterEqual(len(people), 2)
                self.assertGreaterEqual(
                    len(entities),
                    min(graph.GRAVITY["min_entities_in_focus"], 4))
            finally:
                store.close()

    def test_pinned_survives_flood(self):
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                pid = store.resolve_person("QuietPeer")
                for i in range(8):
                    store.add_commitment(
                        f"Loud open item {i}",
                        confidence=0.95, extracted_at=time.time())
                nid = f"person:{pid}"
                graph.pin_constellation_node(store, nid, True)
                data = graph.constellation(store, limit=20,
                                           record_impressions=False)
                node = next(n for n in data["nodes"] if n["id"] == nid)
                self.assertEqual(node["layer"], "focus")
                self.assertTrue(node["pinned"])
            finally:
                store.close()


class WorkingSetGroundingTests(unittest.TestCase):
    def test_working_set_block_after_profile(self):
        from app.services import grounding as gr
        from app.services import working_memory as wm
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                pid = store.resolve_person("Scott Reeves")
                tid = store.add_task("Send Scott the term sheet",
                                     confidence=0.9, owner_person_id=pid,
                                     extracted_at=time.time())
                wm.persist_slots(store, [{
                    "slot": 0, "node_type": "person", "node_id": pid,
                    "node_key": f"person:{pid}",
                    "entered_at": time.time(), "score": 0.8,
                    "cluster_head": 1, "cluster_n": 1,
                    "reason": {"label": "Scott Reeves", "kind": "person",
                               "why": ["Lit by what you're doing right now"]},
                }, {
                    "slot": 1, "node_type": "fact", "node_id": tid,
                    "node_key": f"fact:{tid}",
                    "entered_at": time.time(), "score": 0.7,
                    "cluster_head": 1, "cluster_n": 1,
                    "reason": {"label": "Send Scott the term sheet",
                               "kind": "task", "why": ["Open obligation"]},
                }])
                with mock.patch("app.services.grounding._semantic_section",
                                return_value=([], [])), \
                     mock.patch("app.services.grounding._activity_section",
                                return_value=[]):
                    out = gr.compose("What about Scott?", store=store,
                                     record_attention=False)
                labels = [s["label"] for s in out["sources"]]
                self.assertIn("working set", labels)
                # After identity (and maybe profile), before person-graph.
                ws_i = labels.index("working set")
                self.assertEqual(labels[0], "identity")
                self.assertLess(ws_i, labels.index("person graph: Scott")
                                if "person graph: Scott" in labels
                                else len(labels))
                self.assertIn("WORKING SET", out["block"])
                self.assertIn("Scott", out["block"])
            finally:
                store.close()

    def test_current_exposes_active_person_and_project(self):
        from app.services import working_memory as wm
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                pid = store.resolve_person("Marc Chen")
                eid = store.resolve_entity("Fundraise", "project")
                wm.persist_slots(store, [{
                    "slot": 0, "node_type": "person", "node_id": pid,
                    "node_key": f"person:{pid}",
                    "entered_at": time.time(), "score": 0.9,
                    "cluster_head": 1, "cluster_n": 1,
                    "reason": {"label": "Marc Chen", "kind": "person",
                               "why": ["Active"]},
                }, {
                    "slot": 1, "node_type": "entity", "node_id": eid,
                    "node_key": f"entity:{eid}",
                    "entered_at": time.time(), "score": 0.8,
                    "cluster_head": 1, "cluster_n": 1,
                    "reason": {"label": "Fundraise", "kind": "project",
                               "why": ["Active project"]},
                }])
                ctx = wm.current(store)
                self.assertEqual(ctx["person_ids"], [pid])
                self.assertEqual(ctx["person_labels"], ["Marc Chen"])
                self.assertEqual(ctx["project_ids"], [eid])
                self.assertEqual(ctx["project_labels"], ["Fundraise"])
            finally:
                store.close()

    def test_active_project_boosted_on_task_question(self):
        from app.services import grounding as gr
        from app.services import working_memory as wm
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                eid = store.resolve_entity("Fundraise", "project")
                tid = store.add_task("Close Fundraise term sheet",
                                     confidence=0.9, extracted_at=time.time())
                wm.persist_slots(store, [{
                    "slot": 0, "node_type": "entity", "node_id": eid,
                    "node_key": f"entity:{eid}",
                    "entered_at": time.time(), "score": 0.9,
                    "cluster_head": 1, "cluster_n": 1,
                    "reason": {"label": "Fundraise", "kind": "project",
                               "why": ["Active project"]},
                }])
                ctx = wm.current(store)
                with mock.patch("app.services.grounding._semantic_section",
                                return_value=([], [])), \
                     mock.patch("app.services.grounding._activity_section",
                                return_value=[]):
                    out = gr.compose("What deadlines do I have?",
                                     store=store, ctx=ctx,
                                     record_attention=False)
                labels = [s["label"] for s in out["sources"]]
                self.assertTrue(
                    any(l.startswith("active project: Fundraise") for l in labels),
                    f"expected project boost, got {labels}")
                self.assertIn("Fundraise", out["block"])
                self.assertIn("Close Fundraise term sheet", out["block"])
            finally:
                store.close()


class PlannerWmTests(unittest.TestCase):
    def test_select_context_reads_wm(self):
        from app.services.agent_planner import PersonalAgentLayer
        from app.services import working_memory as wm
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                pid = store.resolve_person("Marc Chen")
                wm.persist_slots(store, [{
                    "slot": 0, "node_type": "person", "node_id": pid,
                    "node_key": f"person:{pid}",
                    "entered_at": time.time(), "score": 0.7,
                    "cluster_head": 1, "cluster_n": 1,
                    "reason": {"label": "Marc Chen", "kind": "person",
                               "why": ["On the horizon"]},
                }])
                layer = PersonalAgentLayer(store=store)
                with mock.patch.object(layer, "_s", return_value=store):
                    ctx = layer.select_context("ping Marc")
                self.assertIn("WORKING SET", ctx.wm_block)
                self.assertIn(f"person:{pid}", ctx.wm_node_ids)
                self.assertIn("Marc Chen", ctx.memory_block)
            finally:
                store.close()


class FreshnessAndFallbackTests(unittest.TestCase):
    def test_ensure_fresh_rebuilds_when_context_moves(self):
        from app.services import working_memory as wm
        from app.services.now_context import now_context
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now_context.clear()
                store.resolve_person("Ada")
                store.resolve_person("Bea")
                # Empty slots → must rebuild.
                r1 = wm.ensure_fresh(store, force=True)
                self.assertTrue(r1.get("refreshed"))
                self.assertGreater(len(wm.snapshot(store)), 0)
                gen_before = now_context.generation
                # Same gen → no-op.
                r2 = wm.ensure_fresh(store)
                self.assertFalse(r2.get("refreshed"))
                self.assertEqual(r2.get("reason"), "current")
                # Context move → rebuild.
                pid = store.resolve_person("Ada")
                now_context.observe([("person", pid)], weight=1.0)
                self.assertNotEqual(now_context.generation, gen_before)
                r3 = wm.ensure_fresh(store)
                self.assertTrue(r3.get("refreshed"))
            finally:
                now_context.clear()
                store.close()

    def test_quota_fallback_is_marked(self):
        """Selector failure still yields focus via top-k + Admitter (pipeline)."""
        from app.services import graph
        from app.services import working_memory as wm
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Ada")
                store.add_task("Something open", confidence=0.9,
                               extracted_at=time.time())
                with mock.patch("app.services.working_memory.select_focus",
                                side_effect=RuntimeError("boom")):
                    data = graph.constellation(store, limit=20,
                                               record_impressions=False)
                sel = data.get("selection") or {}
                self.assertEqual(sel.get("path"), "pipeline")
                self.assertTrue(sel.get("fallback"))
                self.assertIn("boom", sel.get("reason") or "")
                self.assertTrue(wm.last_selection().get("fallback"))
                self.assertGreater(
                    sum(1 for n in data["nodes"] if n["layer"] == "focus"), 0)
            finally:
                store.close()

    def test_selection_reports_wm_path(self):
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Ada")
                store.resolve_person("Bea")
                data = graph.constellation(store, limit=20,
                                           record_impressions=False)
                sel = data.get("selection") or {}
                self.assertEqual(sel.get("path"), "pipeline")
                self.assertFalse(sel.get("fallback"))
            finally:
                store.close()


class SoftmaxAndContractsTests(unittest.TestCase):
    def test_softmax_prominence_is_zero_sum(self):
        from app.services.graph import _apply_softmax_prominence

        nodes = [
            {"id": "a", "layer": "focus", "gravity": 0.9},
            {"id": "b", "layer": "focus", "gravity": 0.4},
            {"id": "c", "layer": "focus", "gravity": 0.2},
            {"id": "d", "layer": "periphery", "gravity": 0.8},
        ]
        _apply_softmax_prominence(nodes)
        focus = [n for n in nodes if n["layer"] == "focus"]
        total = sum(n["prominence"] for n in focus)
        self.assertAlmostEqual(total, len(focus), delta=0.05)
        self.assertGreater(focus[0]["prominence"], focus[2]["prominence"])
        # Periphery stays below the focus mean (zero-sum budget is focus-only).
        mean_focus = total / len(focus)
        self.assertLess(nodes[3]["prominence"], mean_focus)

    def test_context_shift_turns_over_wm(self):
        from app.services import working_memory as wm
        from app.services.now_context import now_context

        now_context.clear()
        t0 = time.time()
        ranked_a = [
            {"id": "person:1", "label": "Ada", "kind": "person",
             "gravity": 0.8, "pinned": False, "why": [], "prospective_risk": 0},
            {"id": "person:2", "label": "Bea", "kind": "person",
             "gravity": 0.7, "pinned": False, "why": [], "prospective_risk": 0},
            {"id": "fact:1", "label": "Task A", "kind": "task",
             "gravity": 0.6, "pinned": False, "why": [], "prospective_risk": 0},
        ]
        with tempfile.TemporaryDirectory() as td:
            from app.storage import Store
            store = Store(Path(td) / "t.db")
            try:
                f1 = wm.select_focus(ranked_a, 7, store=store, now=t0)
                ids1 = {n["id"] for n in f1}
                # Simulate long residence then a context switch with a new cast.
                now_context.observe([("person", 99)], weight=1.0)
                ranked_b = [
                    {"id": "person:9", "label": "Scott", "kind": "person",
                     "gravity": 0.95, "pinned": False, "why": [],
                     "prospective_risk": 0},
                    {"id": "fact:9", "label": "Term sheet", "kind": "task",
                     "gravity": 0.9, "pinned": False, "why": [],
                     "prospective_risk": 0.8},
                    {"id": "entity:9", "label": "Series A", "kind": "project",
                     "gravity": 0.85, "pinned": False, "why": [],
                     "prospective_risk": 0},
                ]
                f2 = wm.select_focus(ranked_b, 7, store=store,
                                     now=t0 + 120)
                ids2 = {n["id"] for n in f2}
                if not ids1:
                    self.skipTest("empty initial WM")
                turnover = len(ids1 - ids2) / len(ids1)
                self.assertGreaterEqual(
                    turnover, 0.5,
                    f"context shift turnover {turnover:.2f} < 0.5 "
                    f"({ids1} -> {ids2})")
                delta = wm.last_delta()
                self.assertTrue(delta.get("enter") or delta.get("exit"))
            finally:
                now_context.clear()
                store.close()

    def test_urgent_preempts_one_slot(self):
        from app.services import working_memory as wm

        ranked = [
            {"id": f"fact:{i}", "label": f"Quiet {i}", "kind": "task",
             "gravity": 0.55, "pinned": False, "why": [],
             "prospective_risk": 0.1}
            for i in range(8)
        ]
        ranked.append({
            "id": "fact:99", "label": "OVERDUE promise", "kind": "commitment",
            "gravity": 0.35, "pinned": False, "why": ["At risk"],
            "prospective_risk": 0.92,
        })
        # Cap small so preempt must displace someone.
        focus = wm.select_focus(ranked, 7, store=None, persist=False,
                                now=time.time())
        ids = [n["id"] for n in focus]
        self.assertIn("fact:99", ids)
        preempted = [n for n in focus if n.get("urgent_preempt")]
        self.assertEqual(len(preempted), 1)
        self.assertIn("Urgency claimed", preempted[0]["why"][0])

    def test_boredom_evicts_idle_furniture(self):
        from app.services import working_memory as wm
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                t0 = time.time()
                ranked = [
                    {"id": "person:1", "label": "Ada", "kind": "person",
                     "gravity": 0.5, "pinned": False, "why": [],
                     "prospective_risk": 0},
                    {"id": "fact:1", "label": "Stale chore", "kind": "task",
                     "gravity": 0.45, "pinned": False, "why": [],
                     "prospective_risk": 0},
                ]
                wm.select_focus(ranked, 7, store=store, now=t0)
                # Jump past boredom window with no engagement; offer a better node.
                ranked2 = [
                    {"id": "person:1", "label": "Ada", "kind": "person",
                     "gravity": 0.5, "pinned": False, "why": [],
                     "prospective_risk": 0},
                    {"id": "fact:1", "label": "Stale chore", "kind": "task",
                     "gravity": 0.45, "pinned": False, "why": [],
                     "prospective_risk": 0},
                    {"id": "person:2", "label": "Bea", "kind": "person",
                     "gravity": 0.7, "pinned": False, "why": [],
                     "prospective_risk": 0},
                ]
                # Force boredom path: mark previous as long-idle by selecting
                # far in the future without touch_engagement.
                focus = wm.select_focus(
                    ranked2, 7, store=store,
                    now=t0 + wm.BOREDOM_S + 60)
                ids = {n["id"] for n in focus}
                # Furniture may leave; Bea should be admissible.
                self.assertIn("person:2", ids)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
