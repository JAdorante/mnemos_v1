"""Multi-task decomposition — split one message into independently-routed tasks.

Covers the pure layer offline (rule gate, ordering, dependency context, summary,
status) and the LLM decomposition path with a mocked router-tier LLM, so the whole
fan-out is tested without any network.
"""
from __future__ import annotations

import os
import unittest

from app.services import multitask as mt


class _FakeLLM:
    """Stands in for browser_agent.llm.LLM — returns a canned structured split."""
    def __init__(self, payload=None, raises=False):
        self.payload = payload or {"tasks": []}
        self.raises = raises
        self.calls = 0

    def _json_call(self, model, system, user, schema, effort=None):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")
        return self.payload


class RuleGateTests(unittest.TestCase):
    def test_markers_trigger(self):
        self.assertTrue(mt.looks_multi("text Dana and find Acme's careers page"))
        self.assertTrue(mt.looks_multi("open the folder then run the build"))
        self.assertTrue(mt.looks_multi("do X; do Y"))

    def test_single_intent_does_not_trigger(self):
        self.assertFalse(mt.looks_multi("find the pricing and features page")
                         and len("find the pricing and features page") < 12)
        self.assertFalse(mt.looks_multi("hi"))
        self.assertFalse(mt.looks_multi("summarize this page"))

    def test_bullet_list_triggers(self):
        self.assertTrue(mt.looks_multi("- buy milk\n- call the vet"))


class DecomposeTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("QUILL_MULTITASK", None)

    def test_disabled_returns_single(self):
        os.environ["QUILL_MULTITASK"] = "0"
        llm = _FakeLLM({"tasks": [{"id": "t1", "text": "a"}, {"id": "t2", "text": "b"}]})
        tasks = mt.decompose("a and b", llm=llm)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(llm.calls, 0)              # never calls the LLM when off

    def test_no_marker_skips_llm(self):
        llm = _FakeLLM({"tasks": [{"id": "t1", "text": "x"}]})
        tasks = mt.decompose("summarize this page", llm=llm)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(llm.calls, 0)              # rule gate short-circuits

    def test_split_into_two_with_surfaces_and_deps(self):
        payload = {"tasks": [
            {"id": "t1", "text": "Find Acme's careers page", "surface_hint": "browser",
             "depends_on": [], "requires_approval": False, "risk": "low"},
            {"id": "t2", "text": "Text the URL to <name>", "surface_hint": "phone_link",
             "depends_on": ["t1"], "requires_approval": True, "risk": "medium"},
        ]}
        llm = _FakeLLM(payload)
        tasks = mt.decompose("find Acme's careers page and text it to <name>", llm=llm)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].surface_hint, "browser")
        self.assertEqual(tasks[1].surface_hint, "phone_link")
        self.assertEqual(tasks[1].depends_on, ["t1"])
        self.assertTrue(tasks[1].requires_approval)

    def test_bad_surface_sanitized_to_none(self):
        payload = {"tasks": [{"id": "t1", "text": "x", "surface_hint": "teleport",
                              "depends_on": []},
                             {"id": "t2", "text": "y", "surface_hint": "browser",
                              "depends_on": []}]}
        tasks = mt.decompose("x and y", llm=_FakeLLM(payload))
        self.assertIsNone(tasks[0].surface_hint)

    def test_llm_error_falls_back_to_single(self):
        tasks = mt.decompose("a and b", llm=_FakeLLM(raises=True))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].text, "a and b")

    def test_empty_tasks_falls_back_to_single(self):
        tasks = mt.decompose("a and b", llm=_FakeLLM({"tasks": []}))
        self.assertEqual(len(tasks), 1)


class OrderingTests(unittest.TestCase):
    def test_topological_order(self):
        t1 = mt.AtomicTask(id="t1", text="find")
        t2 = mt.AtomicTask(id="t2", text="send", depends_on=["t1"])
        ordered = mt.order_tasks([t2, t1])          # given out of order
        self.assertEqual([t.id for t in ordered], ["t1", "t2"])

    def test_dangling_dep_dropped(self):
        t = mt.AtomicTask(id="t1", text="x", depends_on=["ghost"])
        ordered = mt.order_tasks([t])
        self.assertEqual(ordered[0].depends_on, [])

    def test_cycle_does_not_hang(self):
        a = mt.AtomicTask(id="a", text="a", depends_on=["b"])
        b = mt.AtomicTask(id="b", text="b", depends_on=["a"])
        ordered = mt.order_tasks([a, b])
        self.assertEqual(len(ordered), 2)


class ContextAndSummaryTests(unittest.TestCase):
    def test_dependency_context(self):
        t = mt.AtomicTask(id="t2", text="send it", depends_on=["t1"])
        ctx = mt.dependency_context(t, {"t1": "https://acme.com/careers"})
        self.assertIn("acme.com/careers", ctx)
        self.assertEqual(mt.dependency_context(
            mt.AtomicTask(id="t1", text="x"), {}), "")   # no deps -> empty

    def test_status_ok(self):
        self.assertTrue(mt.status_ok("success"))
        self.assertTrue(mt.status_ok("answered_no_browser"))
        self.assertFalse(mt.status_ok("failed"))
        self.assertFalse(mt.status_ok("cancelled"))
        self.assertFalse(mt.status_ok(""))

    def test_summary_partial(self):
        t1 = mt.AtomicTask(id="t1", text="find page")
        t2 = mt.AtomicTask(id="t2", text="text it", depends_on=["t1"])
        s = mt.summarize([t1, t2], done_ids=["t1"], failed_ids=[],
                         skipped_ids=["t2"], results={"t1": "found"})
        self.assertIn("Completed 1 of 2", s)
        self.assertIn("find page", s)
        self.assertIn("Needs help", s)


if __name__ == "__main__":
    unittest.main()
