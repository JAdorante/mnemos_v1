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

    def test_web_goal_stays_on_browser_q(self) -> None:
        w = self._worker()
        w.send("find the Acme careers page")
        cmd = w.cmd_q.get_nowait()
        self.assertIsNone(cmd["surface"])
        with self.assertRaises(queue.Empty):
            w.fast_q.get_nowait()

    def test_snapshot_merges_busy_flags(self) -> None:
        w = self._worker()
        w.busy = True
        w.busy_fast = False
        _, state = w.snapshot(0)
        self.assertTrue(state["busy"])
        self.assertTrue(state["busy_browser"])
        self.assertFalse(state["busy_fast"])
        w.busy = False
        w.busy_fast = True
        _, state = w.snapshot(0)
        self.assertTrue(state["busy"])
        self.assertTrue(state["busy_fast"])


if __name__ == "__main__":
    unittest.main()
