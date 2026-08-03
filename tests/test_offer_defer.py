"""Todo offers must not interrupt an in-flight agent goal."""
from __future__ import annotations

import unittest
from unittest import mock

from app.services.agent_bridge import AgentWorker


class OfferDeferTests(unittest.TestCase):
    def test_queues_while_browser_busy(self) -> None:
        w = AgentWorker()
        w.busy = True
        shown = w.propose_todo(["Email alex@example.com hi"], "To Do List")
        self.assertFalse(shown)
        self.assertIsNone(w.pending_todo)
        self.assertEqual(len(w.offer_queue), 1)

    def test_queues_while_cmd_pending(self) -> None:
        w = AgentWorker()
        w.cmd_q.put({"type": "goal", "text": "research something"})
        shown = w.propose_todo(["Find Chinese food nearby"], "To Do List")
        self.assertFalse(shown)
        self.assertEqual(len(w.offer_queue), 1)

    def test_surfaces_when_idle_again(self) -> None:
        w = AgentWorker()
        w.busy = True
        w.propose_todo(["Email alex@example.com hi"], "To Do List")
        w.busy = False
        shown = w._try_surface_queued_offer()
        self.assertTrue(shown)
        self.assertIsNotNone(w.pending_todo)
        self.assertEqual(w.offer_queue, [])

    def test_accept_does_not_immediately_surface_next(self) -> None:
        w = AgentWorker()
        # send() must leave work on a queue so the next offer stays deferred.
        w.send = mock.Mock(
            side_effect=lambda *a, **k: w.cmd_q.put({"type": "goal", "text": a[0]}))
        w.propose_todo(["Email alex@example.com a"], "To Do List")
        w.propose_todo(["Email sam@example.com b"], "To Do List")
        self.assertEqual(len(w.offer_queue), 1)
        w.resolve_todo(True)
        self.assertIsNone(w.pending_todo)
        self.assertEqual(len(w.offer_queue), 1)
        self.assertFalse(w.cmd_q.empty())


if __name__ == "__main__":
    unittest.main()
