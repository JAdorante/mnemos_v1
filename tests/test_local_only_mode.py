"""Local-only mode: no ANTHROPIC_API_KEY must not silently kill chat.

Aug 2026 live repro: with no key, the worker thread exited at startup, every
/chat message was enqueued into a dead queue (silently swallowed — no user
bubble, no reply), and the chat UI appended a fresh "Issue" card every poll
second because `state.error` never clears. Fixes under test:

- `LLM` no longer needs credentials to construct; cloud-only calls raise
  `CloudModelUnavailable` (friendly prose), and `route()` degrades to the
  direct-answer surface so chat rides the local text tier.
- A lane that cannot start consumes its queue and visibly refuses each goal
  (`_reject_loop`) instead of letting messages vanish.
- With local chat available, the worker starts key-less in local-only mode.
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from app.services import agent_bridge
from browser_agent.llm import LLM, CloudModelUnavailable


def _keyless_llm() -> LLM:
    """LLM without __init__ — no client construction, cloud flagged off."""
    llm = LLM.__new__(LLM)
    llm.usage = {}
    llm.cloud_ok = False
    llm.client = SimpleNamespace(
        messages=SimpleNamespace(create=mock.Mock(
            side_effect=AssertionError("cloud client must not be called"))))
    return llm


def _wait_for(worker, pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with worker.lock:
            evs = list(worker.events)
        if pred(evs):
            return evs
        time.sleep(0.02)
    with worker.lock:
        return list(worker.events)


class CloudGateTests(unittest.TestCase):
    def test_require_cloud_raises_without_credentials(self) -> None:
        llm = _keyless_llm()
        with self.assertRaises(CloudModelUnavailable):
            llm._require_cloud()

    def test_require_cloud_defaults_open_for_handbuilt_llms(self) -> None:
        # Tests / callers that hand-assemble an LLM with a mock client never
        # set cloud_ok; they must keep working unchanged.
        llm = LLM.__new__(LLM)
        llm._require_cloud()   # no raise

    def test_route_degrades_to_direct_answer(self) -> None:
        out = _keyless_llm().route("what's on my plate today?")
        self.assertEqual(out["surface"], "none")
        self.assertFalse(out["requires_browser"])
        self.assertEqual(out["tool"], "direct_answer")

    def test_executor_calls_raise_friendly_error(self) -> None:
        llm = _keyless_llm()
        for call in (lambda: llm.choose_action("content"),
                     lambda: llm.choose_desktop_action("content"),
                     lambda: llm.verify("a", {}, {})):
            with self.assertRaises(CloudModelUnavailable):
                call()

    def test_friendly_error_passes_prose_through(self) -> None:
        msg = agent_bridge._friendly_error(CloudModelUnavailable("add a key"))
        self.assertEqual(msg, "add a key")


class DeadLaneRejectTests(unittest.TestCase):
    """Neither cloud nor local: a sent goal is refused visibly, never dropped."""

    def test_goal_into_unstartable_lane_gets_user_bubble_and_error(self) -> None:
        w = agent_bridge.AgentWorker()
        with mock.patch.object(agent_bridge, "_cloud_auth_ok",
                               return_value=False), \
             mock.patch.object(agent_bridge, "_local_chat_ready",
                               return_value=False):
            w.start()
            w.cmd_q.put({"type": "goal", "text": "hello?", "display": "hello?"})
            evs = _wait_for(
                w, lambda evs: any(e["kind"] == "user" for e in evs)
                and sum(e["kind"] == "error" for e in evs) >= 2)
        kinds = [e["kind"] for e in evs]
        self.assertIn("user", kinds)                       # the bubble survives
        self.assertGreaterEqual(kinds.count("error"), 2)   # startup + per-goal
        self.assertTrue(any("hello?" == e["text"] for e in evs
                            if e["kind"] == "user"))
        with w.lock:
            self.assertIsNotNone(w.error)

    def test_fast_lane_refuses_desktop_goals_without_key(self) -> None:
        w = agent_bridge.AgentWorker()
        with mock.patch.object(agent_bridge, "_cloud_auth_ok",
                               return_value=False), \
             mock.patch.object(agent_bridge, "_local_chat_ready",
                               return_value=False):
            w.start()
            w.fast_q.put({"type": "goal", "text": "open notepad",
                          "display": "open notepad", "surface": "desktop"})
            evs = _wait_for(
                w, lambda evs: any(e["kind"] == "user"
                                   and e["text"] == "open notepad"
                                   for e in evs))
        errs = [e["text"] for e in evs if e["kind"] == "error"]
        # Assert the MEANING, not the env-var name. The copy deliberately says
        # "connect an Anthropic key in Setup" rather than naming a variable —
        # telling a tester to set an environment variable is not a fix they can
        # act on — and the old literal-string assertion failed on that
        # improvement while the behaviour it guards was still correct.
        self.assertTrue(
            any("anthropic" in t.lower() and "setup" in t.lower() for t in errs),
            f"expected an error pointing at Anthropic credentials; got {errs}")


class LocalOnlyStartTests(unittest.TestCase):
    """Cloud off + local tier ready: the worker starts and says so once."""

    def test_worker_starts_local_only(self) -> None:
        w = agent_bridge.AgentWorker()
        stub = SimpleNamespace(current_url=lambda: None, cost=lambda: 0.0,
                               transcript=[])
        with mock.patch.object(agent_bridge, "_cloud_auth_ok",
                               return_value=False), \
             mock.patch.object(agent_bridge, "_local_chat_ready",
                               return_value=True), \
             mock.patch.object(agent_bridge.AgentWorker, "_build_agent",
                               return_value=stub):
            w.start()
            evs = _wait_for(w, lambda evs: any(e["kind"] == "system"
                                               for e in evs))
        with w.lock:
            self.assertTrue(w.ready)
            self.assertIsNone(w.error)
        sys_msgs = [e["text"] for e in evs if e["kind"] == "system"]
        self.assertTrue(any("Local-only mode" in t for t in sys_msgs))


if __name__ == "__main__":
    unittest.main()
