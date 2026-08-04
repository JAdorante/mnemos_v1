"""Duplicate-send refusal (plan task 0.8).

Before a send-class commit, refuse if a verified same-executed_hash send
exists in the last hour. Non-send irreversible clicks (Delete/Buy) are
unaffected.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.services.agent_log import Recorder, hash_packet_payload
from app.storage import Store
from browser_agent.orchestrator import (
    Agent,
    _looks_send_class,
    _DUP_SEND_WINDOW_S,
)


FIELDS = {
    "action": "Send email",
    "to": "Marc",
    "subject": "Pricing",
    "body": "Following up on $49/seat.",
}


def _agent_with_store():
    tmp = Path(tempfile.mkdtemp())
    store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
    rec = Recorder(store=store)
    rec.start_run("send follow-up", surface="browser", dry_run="approval")
    agent = Agent(recorder=rec, on_log=lambda _s: None,
                  on_ask=lambda _q: "approve")
    agent.last_route = {"intent": "send_email", "requires_user_approval": True}
    return agent, store, rec


class SendClassHeuristicTests(unittest.TestCase):
    def test_send_submit_publish_post(self):
        for name in ("Send", "Send message", "Submit", "Publish", "Post"):
            self.assertTrue(_looks_send_class(name), name)

    def test_non_send_irreversible_excluded(self):
        for name in ("Delete", "Remove", "Buy now", "Pay", "Checkout"):
            self.assertFalse(_looks_send_class(name), name)

    def test_compose_placeholder_excluded(self):
        self.assertFalse(_looks_send_class("Send a chat"))
        self.assertFalse(_looks_send_class("Send a message"))


class ExecutedHashStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_stamp_and_find_within_window(self):
        pid = self.store.record_action_packet(
            summary="Send email", fields=FIELDS, execution_surface="browser")
        h = hash_packet_payload(FIELDS)
        self.assertIsNone(self.store.find_recent_executed_hash(h))
        self.store.set_packet_executed_hash(pid, h)
        found = self.store.find_recent_executed_hash(h, within_s=3600)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], pid)
        self.assertEqual(found["executed_hash"], h)
        self.assertIsNotNone(found["executed_at"])

    def test_outside_window_not_found(self):
        pid = self.store.record_action_packet(fields=FIELDS)
        h = hash_packet_payload(FIELDS)
        self.store.set_packet_executed_hash(
            pid, h, executed_at=time.time() - 7200)
        self.assertIsNone(
            self.store.find_recent_executed_hash(h, within_s=3600))

    def test_different_hash_not_found(self):
        pid = self.store.record_action_packet(fields=FIELDS)
        h = hash_packet_payload(FIELDS)
        self.store.set_packet_executed_hash(pid, h)
        other = hash_packet_payload(dict(FIELDS, to="Eve"))
        self.assertIsNone(self.store.find_recent_executed_hash(other))


class DuplicateSendGateTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.store, self.rec = _agent_with_store()

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _approve(self, fields=None):
        fields = fields or FIELDS
        self.agent._ask_fn = lambda _q: "approve"
        decision, _ = self.agent._approval_decision("Send email to Marc", fields)
        self.assertEqual(decision, "approve")
        return self.agent._bound_packet

    def test_first_send_ok(self):
        self._approve()
        result = self.agent._duplicate_send_check("Send")
        self.assertFalse(result["block"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["hash"], hash_packet_payload(FIELDS))

    def test_same_hash_within_hour_refused(self):
        bound = self._approve()
        self.agent._stamp_executed_send("Send")
        row = self.store.get_action_packet(bound["packet_id"])
        self.assertEqual(row["executed_hash"], hash_packet_payload(FIELDS))

        result = self.agent._duplicate_send_check("Send")
        self.assertTrue(result["block"])
        self.assertEqual(result["reason"], "duplicate_send")
        self.assertEqual(result["prior"]["id"], bound["packet_id"])

    def test_different_payload_allowed(self):
        self._approve()
        self.agent._stamp_executed_send("Send")
        # Fresh approve with different recipient ⇒ different hash.
        drifted = dict(FIELDS, to="Eve")
        self._approve(drifted)
        result = self.agent._duplicate_send_check("Send")
        self.assertFalse(result["block"])
        self.assertEqual(result["hash"], hash_packet_payload(drifted))

    def test_window_expiry_allows_resend(self):
        bound = self._approve()
        h = hash_packet_payload(FIELDS)
        self.store.set_packet_executed_hash(
            bound["packet_id"], h,
            executed_at=time.time() - (_DUP_SEND_WINDOW_S + 10))
        # Clear session-local stamp so only DB window applies.
        self.agent._recent_executed_hashes.clear()
        result = self.agent._duplicate_send_check("Send")
        self.assertFalse(result["block"])

    def test_delete_not_checked(self):
        self._approve()
        self.agent._stamp_executed_send("Send")
        result = self.agent._duplicate_send_check("Delete")
        self.assertFalse(result["block"])
        self.assertEqual(result["reason"], "n/a")

    def test_stamp_skips_non_send(self):
        bound = self._approve()
        self.agent._stamp_executed_send("Delete")
        row = self.store.get_action_packet(bound["packet_id"])
        self.assertIsNone(row["executed_hash"])

    def test_recorder_wrappers(self):
        pid = self.rec.record_packet(summary="Send", fields=FIELDS)
        h = hash_packet_payload(FIELDS)
        self.rec.set_executed_hash(pid, h)
        found = self.rec.find_recent_executed(h, within_s=3600)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], pid)


if __name__ == "__main__":
    unittest.main()
