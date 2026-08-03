"""Bare yes/no with nothing pending must never re-route as a goal.

Observed live: four consecutive "yes" turns each got the identical echo of the
previous result ("Typed 'How is your day going?' ... and pressed Enter").
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from app.services.agent_bridge import AgentWorker


def _worker(last_result: str | None = None) -> AgentWorker:
    w = AgentWorker.__new__(AgentWorker)
    w.lock = threading.Lock()
    w.events = []
    w.next_id = 0
    w.send = mock.Mock()
    if last_result is not None:
        w.events.append({"id": 0, "kind": "result", "text": last_result})
        w.next_id = 1
    return w


class IdleVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        # _emit best-effort speaks replies aloud — keep tests silent.
        p = mock.patch("app.services.voice.maybe_speak_reply")
        self.addCleanup(p.stop)
        p.start()

    def _results(self, w) -> list[str]:
        return [e["text"] for e in w.events if e["kind"] == "result"]

    def test_non_verdict_returns_none(self) -> None:
        w = _worker("done.")
        self.assertIsNone(w.handle_idle_verdict("open my calendar"))
        w.send.assert_not_called()

    def test_yes_with_no_offer_nudges_instead_of_echoing(self) -> None:
        w = _worker("Typed 'How is your day going?' in the Phone Link message "
                    "field and pressed Enter to send it.")
        out = w.handle_idle_verdict("yes")
        self.assertEqual(out, {"ok": True, "routed": "no_pending_ack"})
        w.send.assert_not_called()
        self.assertIn("Nothing is waiting", self._results(w)[-1])
        # A second "yes" gets the same honest nudge, not an echo loop.
        out2 = w.handle_idle_verdict("yes")
        self.assertEqual(out2["routed"], "no_pending_ack")
        w.send.assert_not_called()

    def test_yes_accepts_trailing_offer(self) -> None:
        w = _worker("Here's your summary.\n\nWant me to give you a daily "
                    "digest of your calendar?")
        out = w.handle_idle_verdict("yes")
        self.assertEqual(out["routed"], "offer_accepted")
        w.send.assert_called_once()
        goal = w.send.call_args[0][0]
        self.assertIn("daily digest", goal)
        self.assertIn("accepting the offer", goal)

    def test_no_declines_trailing_offer(self) -> None:
        w = _worker("All set.\nWant me to tackle one of your open tasks?")
        out = w.handle_idle_verdict("no")
        self.assertEqual(out["routed"], "offer_declined")
        w.send.assert_not_called()
        self.assertIn("won't", self._results(w)[-1])

    def test_no_events_yet_still_safe(self) -> None:
        w = _worker()
        out = w.handle_idle_verdict("yes")
        self.assertEqual(out["routed"], "no_pending_ack")
        w.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
