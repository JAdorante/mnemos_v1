"""Desktop/phone goals must enqueue on the fast lane, not behind the browser."""
from __future__ import annotations

import queue
import unittest
from unittest import mock

from app.services.agent_bridge import AgentWorker


class FastLaneEnqueueTests(unittest.TestCase):
    def _worker(self) -> AgentWorker:
        w = AgentWorker()
        # Don't spawn real agent threads.
        w.start = mock.Mock(side_effect=lambda: setattr(w, "_started", True))
        return w

    def test_open_app_goes_to_fast_q(self) -> None:
        w = self._worker()
        w.send("open Cursor")
        cmd = w.fast_q.get_nowait()
        self.assertEqual(cmd["surface"], "desktop")
        self.assertEqual(cmd["text"], "open Cursor")
        with self.assertRaises(queue.Empty):
            w.cmd_q.get_nowait()

    def test_explicit_phone_goes_to_fast_q(self) -> None:
        w = self._worker()
        w.send("text Abby I'm late", surface="phone_link")
        cmd = w.fast_q.get_nowait()
        self.assertEqual(cmd["surface"], "phone_link")
        with self.assertRaises(queue.Empty):
            w.cmd_q.get_nowait()

    def test_heuristic_text_goes_to_fast_q(self) -> None:
        w = self._worker()
        w.send("text Mom that I'll call soon")
        cmd = w.fast_q.get_nowait()
        self.assertEqual(cmd["surface"], "phone_link")
        with self.assertRaises(queue.Empty):
            w.cmd_q.get_nowait()

    def test_plain_goal_enters_the_chat_lane(self) -> None:
        w = self._worker()
        w.send("find the Acme careers page")
        cmd = w.chat_q.get_nowait()
        self.assertEqual(cmd["lane"], "chat")
        self.assertIsNone(cmd["surface"])
        with self.assertRaises(queue.Empty):
            w.cmd_q.get_nowait()
        with self.assertRaises(queue.Empty):
            w.fast_q.get_nowait()

    def test_browser_route_moves_to_the_browser_lane(self) -> None:
        w = self._worker()

        class _Agent:
            def run_goal(self, *_a, **_k):
                self._browser_handoff = {
                    "route": {"surface": "browser", "requires_browser": True},
                    "ctx": "",
                    "level": "approval",
                    "study_guidance": "",
                    "surface": "browser",
                }
                return "", "needs_browser"

        w.chat_agent = _Agent()
        cmd = {"type": "goal", "text": "open the careers page",
               "display": "open the careers page", "lane": "chat"}
        self.assertTrue(w._handoff_chat_goal(cmd))
        moved = w.cmd_q.get_nowait()
        self.assertEqual(moved["lane"], "browser")
        self.assertEqual(moved["handoff"]["route"]["surface"], "browser")
        self.assertTrue(cmd["handed_off"])
        with self.assertRaises(queue.Empty):
            w.fast_q.get_nowait()

    def test_direct_answer_stays_off_the_browser_lane(self) -> None:
        w = self._worker()

        class _Agent:
            def run_goal(self, *_a, **_k):
                return "Forty-nine a month.", "answered_no_browser"

        w.chat_agent = _Agent()
        cmd = {"type": "goal", "text": "what did we promise?",
               "display": "what did we promise?", "lane": "chat"}
        self.assertFalse(w._handoff_chat_goal(cmd))
        self.assertEqual(cmd["_result"], "Forty-nine a month.")
        with self.assertRaises(queue.Empty):
            w.cmd_q.get_nowait()

    def test_snapshot_merges_busy_flags(self) -> None:
        w = self._worker()
        w.busy = True
        w.busy_fast = False
        w.busy_chat = False
        _, state = w.snapshot(0)
        self.assertFalse(state["busy"])
        self.assertTrue(state["busy_browser"])
        self.assertFalse(state["busy_fast"])
        w.busy = False
        w.busy_fast = True
        _, state = w.snapshot(0)
        self.assertTrue(state["busy"])
        self.assertTrue(state["busy_fast"])
        w.busy_fast = False
        w.busy_chat = True
        _, state = w.snapshot(0)
        self.assertTrue(state["busy"])
        self.assertTrue(state["busy_chat"])


if __name__ == "__main__":
    unittest.main()
