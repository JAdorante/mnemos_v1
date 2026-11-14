"""`QUILL_BROWSER=0` turns off the agent browser (web search and browsing).

A routed web goal is answered from memory with a note, the chat lane never
hands it to the browser lane, and anything that still reaches Playwright is
refused.
"""
from __future__ import annotations

import os
import types
import unittest
from unittest import mock


def _agent(route, answers):
    from browser_agent.orchestrator import Agent
    a = Agent.__new__(Agent)
    a.dry_run = "draft"
    a.logs = []
    a._log = a.logs.append
    a.session_id = "s"
    a.transcript = []
    a.last_route = None
    a.last_distill_id = None
    a._recorder = types.SimpleNamespace(annotate_run=lambda **kw: None)
    a.mem = types.SimpleNamespace(log_event=lambda *args: None)

    def direct_answer(goal, ctx, mode_guidance=""):
        answers.append(ctx)
        return "From memory."

    a.llm = types.SimpleNamespace(route=lambda g, c: dict(route),
                                  direct_answer=direct_answer, last_distill_id=None)
    a._desktop = lambda: None
    a._ensure_browser = lambda: (_ for _ in ()).throw(AssertionError("browser started"))
    return a


def _run(a, goal):
    from browser_agent import orchestrator as orch
    from browser_agent.orchestrator import Agent
    with mock.patch.object(Agent, "_study_block", return_value=""), \
         mock.patch.object(Agent, "_build_ctx", return_value="CTX\n"), \
         mock.patch.object(orch, "_connector_lane", return_value=None):
        return a._run_goal_inner(goal)


_WEB = {"intent": "research_company", "site": "web", "surface": "browser",
        "requires_browser": True, "tool": "browser_agent", "rationale": ""}


class BrowserOffTests(unittest.TestCase):
    def test_web_goal_answers_directly(self) -> None:
        answers = []
        a = _agent(_WEB, answers)
        with mock.patch.dict(os.environ, {"QUILL_BROWSER": "0"}):
            ans, status = _run(a, "find Acme's careers page")
        self.assertEqual((ans, status), ("From memory.", "answered_no_browser"))
        self.assertIn("web search are turned off", answers[0])
        self.assertTrue(a.last_route["browser_disabled"])
        self.assertFalse(a.last_route["requires_browser"])

    def test_chat_lane_never_hands_off(self) -> None:
        a = _agent(_WEB, [])
        a.defer_browser = True
        with mock.patch.dict(os.environ, {"QUILL_BROWSER": "0"}):
            _, status = _run(a, "search the web for Acme")
        self.assertEqual(status, "answered_no_browser")

    def test_live_feed_is_not_upgraded_to_browser(self) -> None:
        route = {"intent": "chat", "site": "", "surface": "none",
                 "requires_browser": False, "tool": "none", "rationale": ""}
        a = _agent(route, [])
        with mock.patch.dict(os.environ, {"QUILL_BROWSER": "0"}):
            _, status = _run(a, "what's on my X feed")
        self.assertEqual(status, "answered_no_browser")

    def test_ensure_browser_refuses(self) -> None:
        from browser_agent.orchestrator import Agent
        a = Agent.__new__(Agent)
        a._browser_started = False
        a.driver = types.SimpleNamespace(
            start=lambda: (_ for _ in ()).throw(AssertionError("started")))
        with mock.patch.dict(os.environ, {"QUILL_BROWSER": "0"}):
            with self.assertRaisesRegex(RuntimeError, "QUILL_BROWSER=0"):
                a._ensure_browser()

    def test_default_still_browses(self) -> None:
        a = _agent(_WEB, [])
        a.defer_browser = True
        a._remember_handoff = lambda *args: None
        env = {k: v for k, v in os.environ.items() if k != "QUILL_BROWSER"}
        with mock.patch.dict(os.environ, env, clear=True):
            _, status = _run(a, "find Acme's careers page")
        self.assertEqual(status, "needs_browser")


if __name__ == "__main__":
    unittest.main()
