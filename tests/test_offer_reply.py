"""Pending offer / approval replies must not swallow real instructions."""
from __future__ import annotations

import time
import unittest
from unittest import mock

from app.services import agent_bridge as ab
from app.services.agent_bridge import AgentWorker


class VerdictTests(unittest.TestCase):
    def test_plain_yes_no(self) -> None:
        self.assertTrue(ab._is_plain_verdict("yes"))
        self.assertTrue(ab._is_plain_verdict("approve"))
        self.assertFalse(ab._is_plain_verdict("no"))
        self.assertFalse(ab._is_plain_verdict("skip"))

    def test_goal_is_not_verdict(self) -> None:
        self.assertIsNone(ab._is_plain_verdict("open Cursor"))
        self.assertIsNone(ab._is_plain_verdict("text Abby I love you"))
        self.assertIsNone(ab._is_plain_verdict("what are my open tasks?"))


class OfferReplyTests(unittest.TestCase):
    def test_ambiguous_reply_runs_new_goal(self) -> None:
        w = AgentWorker()
        w.send = mock.Mock()
        w.propose_todo(["buy milk"], "groceries")
        self.assertIsNotNone(w.pending_todo)
        out = w.handle_reply("open Cursor")
        self.assertEqual(out.get("routed"), "goal")
        self.assertTrue(out.get("superseded_offer"))
        self.assertIsNone(w.pending_todo)
        w.send.assert_called_once()
        self.assertEqual(w.send.call_args[0][0], "open Cursor")

    def test_yes_still_accepts_offer(self) -> None:
        w = AgentWorker()
        w.send = mock.Mock()
        w.propose_todo(["buy milk"], "groceries")
        out = w.handle_reply("yes")
        self.assertEqual(out.get("routed"), "todo")
        self.assertTrue(out.get("accepted"))
        w.send.assert_called()

    def test_no_declines_offer(self) -> None:
        w = AgentWorker()
        w.send = mock.Mock()
        w.propose_todo(["buy milk"], "groceries")
        out = w.handle_reply("no")
        self.assertEqual(out.get("routed"), "todo")
        self.assertFalse(out.get("accepted"))
        w.send.assert_not_called()

    def test_offer_expires(self) -> None:
        w = AgentWorker()
        w.send = mock.Mock()
        w.propose_anticipation({
            "title": "Open Cursor",
            "goal": "Open Cursor",
            "rationale": "pattern",
            "confidence": 0.9,
        })
        self.assertIsNotNone(w.pending_todo)
        w.pending_todo["created_at"] = time.time() - (ab._OFFER_TTL_S + 5)
        n = w.expire_stale_offers()
        self.assertGreaterEqual(n, 1)
        self.assertIsNone(w.pending_todo)

    def test_reasoner_timeout_marks_dismissed(self) -> None:
        w = AgentWorker()
        prop = mock.Mock()
        prop.reasoner = "scheduling"
        prop.summary = "Schedule work: Finish memo"
        prop.goal = "Propose a schedule window to finish: Finish memo"
        prop.why = ["due_in_1d"]
        prop.confidence = 0.7
        prop.fact_id = 42
        prop.person = None
        prop.deliverable_only = True
        prop.kind = "reasoner_scheduling"
        with mock.patch(
                "app.services.reasoners.base.mark_dismissed_from_offer") as md, \
             mock.patch(
                "app.services.task_offer.record_offer_outcome") as ro:
            w.propose_reasoner(prop)
            self.assertIsNotNone(w.pending_todo)
            w.pending_todo["created_at"] = time.time() - (ab._OFFER_TTL_S + 5)
            w.expire_stale_offers()
        self.assertIsNone(w.pending_todo)
        md.assert_called_once()
        ro.assert_called_once()
        self.assertFalse(ro.call_args[0][1])

    def test_cold_hydrate_skips_offer_asks(self) -> None:
        w = AgentWorker()
        w.propose_todo(["buy milk"], "groceries")
        self.assertIsNotNone(w.pending_todo)
        cold, state = w.snapshot(0)
        self.assertTrue(state.get("todo_pending"))
        self.assertTrue(any(e.get("kind") == "ask" for e in w.events))
        self.assertFalse(any(e.get("kind") == "ask" for e in cold))
        # Incremental poll still delivers the ask once.
        live, _ = w.snapshot(0)  # still cold
        self.assertFalse(any(e.get("kind") == "ask" for e in live))
        # After the client has advanced past cold start, new asks stream.
        since = state["next"]
        w._emit("ask", "APPROVAL NEEDED — launch notepad\nReply 'approve'…")
        later, _ = w.snapshot(since)
        self.assertTrue(any(
            e.get("kind") == "ask" and "APPROVAL NEEDED" in (e.get("text") or "")
            for e in later))

    def test_cold_hydrate_keeps_approval_asks(self) -> None:
        w = AgentWorker()
        w._emit("ask", "APPROVAL NEEDED — send email\nReply 'approve' to proceed.")
        cold, _ = w.snapshot(0)
        self.assertTrue(any(
            e.get("kind") == "ask" and "APPROVAL NEEDED" in (e.get("text") or "")
            for e in cold))

    def test_waiting_on_summary(self) -> None:
        w = AgentWorker()
        w.propose_todo(["a", "b"], "Trip")
        summary = w._offer_waiting_summary()
        self.assertIn("Waiting", summary or "")
        self.assertIn("Trip", summary or "")

    def test_approval_supersede(self) -> None:
        w = AgentWorker()
        w.send = mock.Mock()
        w.awaiting = True
        w.question = ("APPROVAL NEEDED — launch cursor (C:\\Cursor.exe)\n"
                      "Reply 'approve' to proceed, or anything else to cancel.")
        with mock.patch.object(w, "submit_answer") as sub:
            out = w.handle_reply("open Notepad")
        self.assertTrue(out.get("superseded_approval"))
        sub.assert_called_once_with("cancel")
        w.send.assert_called_once_with("open Notepad")


if __name__ == "__main__":
    unittest.main()
