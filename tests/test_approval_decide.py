"""Bound Approve/Cancel/Edit (plan task 0.6).

Buttons and typed "approve" resolve to the pending packet id + payload_hash.
Stale/drifted hashes are refused; negation still wins; edit mints a new packet.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.services.agent_log import Recorder, hash_packet_payload
from app.storage import Store
from browser_agent.orchestrator import Agent


FIELDS = {
    "action": "Send email",
    "to": "Marc",
    "subject": "Pricing",
    "body": "Following up on $49/seat.",
}


class _FakeWorker:
    """Minimal stand-in for AgentWorker.decide_approval tests."""

    def __init__(self, agent, store):
        self.agent = agent
        self.fast_agent = None
        self.lock = threading.Lock()
        self.awaiting = True
        self.awaiting_fast = False
        self._answers = []
        self._store = store
        # Bind decide_approval from the real bridge mixin-style by importing.
        from app.services.agent_bridge import AgentWorker
        self.decide_approval = AgentWorker.decide_approval.__get__(self, _FakeWorker)
        self._pending_approval_packet_unlocked = (
            AgentWorker._pending_approval_packet_unlocked.__get__(self, _FakeWorker))
        self.pending_approval_packet = (
            AgentWorker.pending_approval_packet.__get__(self, _FakeWorker))

    def submit_answer(self, text: str) -> None:
        self._answers.append(text)
        self.awaiting = False
        # Unblock the ask if the agent is mid-_approval_decision.
        if hasattr(self, "_answer_ev"):
            self._answer = text
            self._answer_ev.set()


class PendingPacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.rec = Recorder(store=self.store)
        self.rec.start_run("send follow-up", surface="browser")
        self.agent = Agent(recorder=self.rec, on_log=lambda _s: None,
                           on_ask=lambda _q: "approve")
        self.agent.last_route = {"intent": "send_email",
                                 "requires_user_approval": True}

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_approval_decision_sets_pending_before_ask(self):
        seen = {}

        def ask(prompt):
            pending = self.agent._pending_approval_packet
            seen["pending"] = dict(pending) if pending else None
            return "approve"

        self.agent._ask_fn = ask
        decision, _ = self.agent._approval_decision("Send email to Marc", FIELDS)
        self.assertEqual(decision, "approve")
        self.assertIsNotNone(seen["pending"])
        self.assertEqual(seen["pending"]["payload_hash"],
                         hash_packet_payload(FIELDS))
        self.assertIsNotNone(seen["pending"]["packet_id"])
        # After approve, pending is cleared and bound is set.
        self.assertIsNone(self.agent._pending_approval_packet)
        self.assertIsNotNone(self.agent._bound_packet)

    def test_edit_mints_new_packet_with_new_hash(self):
        self.agent._ask_fn = lambda _q: "change the subject to Q3 pricing"
        decision, feedback = self.agent._approval_decision(
            "Send email to Marc", FIELDS)
        self.assertEqual(decision, "edit")
        self.assertIn("Q3", feedback)
        runs = self.store.recent_agent_runs(limit=1)
        self.assertTrue(runs)
        run = self.store.agent_run(runs[0]["id"])
        packets = run["packets"]
        self.assertGreaterEqual(len(packets), 2)
        old, new = packets[0], packets[1]
        self.assertEqual(old["decision"], "edit")
        self.assertIsNone(new["decision"])
        self.assertNotEqual(old["payload_hash"], new["payload_hash"])
        new_fields = __import__("json").loads(new["fields_json"])
        self.assertIn("edit_request", new_fields)
        self.assertIn("Q3", new_fields["edit_request"])

    def test_approve_stamps_approved_via_typed(self):
        self.agent._last_approved_via = "typed"
        self.agent._ask_fn = lambda _q: "approve"
        self.agent._approval_decision("Send email", FIELDS)
        row = self.store.latest_pending_packet()
        # Pending is cleared; fetch the decided packet.
        pid = self.agent._bound_packet["packet_id"]
        row = self.store.get_action_packet(pid)
        self.assertEqual(row["decision"], "approve")
        self.assertEqual(row["approved_via"], "typed")
        self.assertIsNotNone(row["approved_at"])


class DecideApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.rec = Recorder(store=self.store)
        self.rec.start_run("send", surface="browser")
        self.agent = Agent(recorder=self.rec, on_log=lambda _s: None,
                           on_ask=lambda _q: "cancel")  # won't be used by decide
        self.agent.last_route = {"intent": "send_email"}
        # Pre-stage a pending packet as if ask is in flight.
        pid = self.rec.record_packet(summary="Send email", fields=FIELDS,
                                     execution_surface="browser")
        self.agent._set_pending_approval_packet(pid, FIELDS)
        self.pid = pid
        self.phash = self.agent._pending_approval_packet["payload_hash"]
        self.worker = _FakeWorker(self.agent, self.store)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_button_approve_ok(self):
        result = self.worker.decide_approval(
            self.pid, self.phash, "approve", approved_via="button")
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(self.worker._answers, ["approve"])
        self.assertEqual(self.agent._last_approved_via, "button")

    def test_stale_hash_refused(self):
        result = self.worker.decide_approval(
            self.pid, "0" * 64, "approve", approved_via="button")
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("reason"), "drift")
        self.assertEqual(self.worker._answers, [])

    def test_wrong_packet_id_refused(self):
        result = self.worker.decide_approval(
            self.pid + 99, self.phash, "approve", approved_via="button")
        self.assertFalse(result["ok"])
        self.assertIn("packet_id", result.get("error", ""))
        self.assertEqual(self.worker._answers, [])

    def test_expired_refused(self):
        self.store._conn.execute(
            "UPDATE action_packets SET expires_at = ? WHERE id = ?",
            (time.time() - 1, self.pid))
        self.store._conn.commit()
        result = self.worker.decide_approval(
            self.pid, self.phash, "approve", approved_via="button")
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("reason"), "expired")

    def test_cancel_still_wins(self):
        result = self.worker.decide_approval(
            self.pid, self.phash, "cancel", approved_via="button")
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "cancel")
        self.assertEqual(self.worker._answers, ["cancel"])

    def test_edit_submits_revision_text(self):
        result = self.worker.decide_approval(
            self.pid, self.phash, "edit",
            user_edit="change the subject to Q3",
            fields={"subject": "Q3 pricing"},
            approved_via="button")
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "edit")
        self.assertEqual(self.worker._answers, ["change the subject to Q3"])
        # Pending fields were updated for the mint path.
        self.assertEqual(
            self.agent._pending_approval_packet["fields"]["subject"],
            "Q3 pricing")


class TypedApproveResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.rec = Recorder(store=self.store)
        self.rec.start_run("send", surface="browser")
        self.agent = Agent(recorder=self.rec, on_log=lambda _s: None,
                           on_ask=lambda _q: "approve")
        pid = self.rec.record_packet(summary="Send", fields=FIELDS)
        self.agent._set_pending_approval_packet(pid, FIELDS)
        self.worker = _FakeWorker(self.agent, self.store)
        # Wire handle_reply from real AgentWorker.
        from app.services.agent_bridge import AgentWorker
        self.worker.handle_reply = AgentWorker.handle_reply.__get__(
            self.worker, _FakeWorker)
        self.worker.expire_stale_offers = lambda: None
        self.worker.pending_todo = None
        self.worker.question = "APPROVAL NEEDED — Send email\n\nReply 'approve'"
        self.worker.question_fast = None
        self.worker.send = lambda *_a, **_k: None
        self.worker._emit = lambda *_a, **_k: None
        self.worker._dismiss_offer = lambda **_k: None

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_typed_approve_resolves_pending_packet(self):
        result = self.worker.handle_reply("approve")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("decision"), "approve")
        self.assertEqual(result.get("approved_via"), "typed")
        self.assertEqual(self.worker._answers, ["approve"])

    def test_typed_no_cancels_without_hash(self):
        result = self.worker.handle_reply("cancel")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("decision"), "cancel")
        self.assertEqual(self.worker._answers, ["cancel"])

    def test_typed_approve_refused_when_hash_tampered(self):
        self.agent._pending_approval_packet["payload_hash"] = "0" * 64
        # DB still has the real hash — decide checks pending first for mismatch.
        result = self.worker.handle_reply("yes")
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("routed"), "agent_answer_refused")
        self.assertEqual(self.worker._answers, [])


class ApprovalPartialDecideTests(unittest.TestCase):
    def test_decide_helper_forwards(self):
        from app.api.approval_partial import decide

        class W:
            def decide_approval(self, *a, **k):
                return {"ok": True, "args": a, "kwargs": k}

        out = decide(W(), 7, "abc", "approve", approved_via="button")
        self.assertTrue(out["ok"])
        self.assertEqual(out["args"][0], 7)
        self.assertEqual(out["args"][1], "abc")


if __name__ == "__main__":
    unittest.main()
