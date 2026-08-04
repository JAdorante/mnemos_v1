"""Adversarial approval-binding suite (plan tasks 0.4 + 0.11).

Immediately before an irreversible commit, re-canonicalize about-to-execute
args and require hash == payload_hash and now < expires_at. Drift/expiry fail
closed in enforce mode; shadow mode logs and allows.

0.11 adversarial coverage (enumerated below):
  - recipient / price / attachment drift post-approval
  - expiry
  - duplicate send (same executed_hash within 1h)
  - autonomous-mode policy block (delete/remove never bypasses RISK_TABLE)
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.agent_log import Recorder, hash_packet_payload
from app.storage import Store
from browser_agent import config as cfg
from browser_agent.orchestrator import (
    Agent,
    _DUP_SEND_WINDOW_S,
    _fields_diff,
    _hash_fields,
    _policy_block_reason,
)


FIELDS = {
    "action": "Send email",
    "to": "Marc",
    "subject": "Pricing",
    "body": "Following up on $49/seat.",
    "price": "$49/seat",
    "attachment": "quote-v1.pdf",
}

# Enumerated adversarial drift cases (plan 0.11 acceptance).
# Each: (name, field_overrides_after_approval)
ADVERSARIAL_DRIFT_CASES = (
    ("recipient", {"to": "Eve"}),
    ("price", {"price": "$55/seat",
               "body": "Following up on $55/seat."}),
    ("attachment", {"attachment": "quote-v2-evil.pdf"}),
    ("subject", {"subject": "Wire transfer details"}),
    ("body", {"body": "Ignore prior quote — send $99 instead."}),
)


def _agent_with_store():
    tmp = Path(tempfile.mkdtemp())
    store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
    rec = Recorder(store=store)
    rec.start_run("send follow-up", surface="browser", dry_run="approval")
    agent = Agent(recorder=rec, on_log=lambda _s: None,
                  on_ask=lambda _q: "approve")
    agent.last_route = {"intent": "send_email", "requires_user_approval": True}
    return agent, store, rec


class FieldsHashParityTests(unittest.TestCase):
    def test_orchestrator_hash_matches_storage(self):
        self.assertEqual(_hash_fields(FIELDS), hash_packet_payload(FIELDS))

    def test_diff_lists_changed_keys(self):
        drifted = dict(FIELDS, to="Eve")
        diff = _fields_diff(FIELDS, drifted)
        self.assertIn("to:", diff)
        self.assertIn("Marc", diff)
        self.assertIn("Eve", diff)
        self.assertNotIn("subject:", diff)


class BindCheckEnforceTests(unittest.TestCase):
    def setUp(self):
        self._prev = cfg.APPROVAL_BIND
        cfg.APPROVAL_BIND = "enforce"
        self.agent, self.store, self.rec = _agent_with_store()

    def tearDown(self):
        cfg.APPROVAL_BIND = self._prev
        try:
            self.store.close()
        except Exception:
            pass

    def _approve(self, fields=None):
        fields = fields or FIELDS
        self.agent._ask_fn = lambda _q: "approve"
        decision, _ = self.agent._approval_decision("Send email to Marc", fields)
        self.assertEqual(decision, "approve")
        self.assertIsNotNone(self.agent._bound_packet)
        return self.agent._bound_packet

    def test_matching_fields_ok(self):
        self._approve()
        result = self.agent._approval_bind_check()
        self.assertFalse(result["block"])
        self.assertEqual(result["reason"], "ok")

    def test_recipient_drift_fails_closed(self):
        self._approve()
        self.agent._about_to_execute_fields = dict(FIELDS, to="Eve")
        result = self.agent._approval_bind_check()
        self.assertTrue(result["block"])
        self.assertEqual(result["reason"], "drift")
        self.assertIn("Eve", result["diff"])
        self.assertIn("Marc", result["diff"])

    def test_body_drift_fails_closed(self):
        self._approve()
        self.agent._about_to_execute_fields = dict(
            FIELDS, body="Actually send the $55 quote instead.")
        result = self.agent._approval_bind_check()
        self.assertTrue(result["block"])
        self.assertEqual(result["reason"], "drift")

    def test_expiry_fails_closed(self):
        bound = self._approve()
        # Expire the in-memory binding and the DB row.
        bound["expires_at"] = time.time() - 1
        pid = bound["packet_id"]
        self.store._conn.execute(
            "UPDATE action_packets SET expires_at = ? WHERE id = ?",
            (time.time() - 1, pid))
        self.store._conn.commit()
        result = self.agent._approval_bind_check()
        self.assertTrue(result["block"])
        self.assertEqual(result["reason"], "expired")

    def test_db_hash_is_source_of_truth(self):
        bound = self._approve()
        pid = bound["packet_id"]
        # Tamper the stored hash — commit must refuse even if memory looks fine.
        self.store._conn.execute(
            "UPDATE action_packets SET payload_hash = ? WHERE id = ?",
            ("0" * 64, pid))
        self.store._conn.commit()
        result = self.agent._approval_bind_check()
        self.assertTrue(result["block"])
        self.assertEqual(result["reason"], "drift")


class BindCheckShadowTests(unittest.TestCase):
    def setUp(self):
        self._prev = cfg.APPROVAL_BIND
        cfg.APPROVAL_BIND = "shadow"
        self.agent, self.store, self.rec = _agent_with_store()
        self.logs = []
        self.agent._log = self.logs.append

    def tearDown(self):
        cfg.APPROVAL_BIND = self._prev
        try:
            self.store.close()
        except Exception:
            pass

    def test_shadow_drift_logs_but_does_not_block(self):
        self.agent._ask_fn = lambda _q: "approve"
        self.agent._approval_decision("Send email", FIELDS)
        self.agent._about_to_execute_fields = dict(FIELDS, to="Eve")
        result = self.agent._approval_bind_check()
        self.assertFalse(result["block"])
        self.assertEqual(result["reason"], "drift")
        self.assertTrue(result.get("shadow"))
        self.assertTrue(any("approval-bind/shadow" in line and "drift" in line
                            for line in self.logs))


class BindCheckOffTests(unittest.TestCase):
    def setUp(self):
        self._prev = cfg.APPROVAL_BIND
        cfg.APPROVAL_BIND = "off"
        self.agent, self.store, self.rec = _agent_with_store()

    def tearDown(self):
        cfg.APPROVAL_BIND = self._prev
        try:
            self.store.close()
        except Exception:
            pass

    def test_off_skips_check(self):
        self.agent._ask_fn = lambda _q: "approve"
        self.agent._approval_decision("Send email", FIELDS)
        self.agent._about_to_execute_fields = dict(FIELDS, to="Eve")
        result = self.agent._approval_bind_check()
        self.assertFalse(result["block"])
        self.assertEqual(result["reason"], "off")


class ReAskOnBindFailureTests(unittest.TestCase):
    def setUp(self):
        self._prev = cfg.APPROVAL_BIND
        cfg.APPROVAL_BIND = "enforce"
        self.agent, self.store, self.rec = _agent_with_store()

    def tearDown(self):
        cfg.APPROVAL_BIND = self._prev
        try:
            self.store.close()
        except Exception:
            pass

    def test_reapprove_current_fields_then_proceeds(self):
        self.agent._ask_fn = lambda _q: "approve"
        self.agent._approval_decision("Send email", FIELDS)
        drifted = dict(FIELDS, to="Eve")
        self.agent._about_to_execute_fields = drifted
        gathered = []
        # First check would block; the gate re-asks and we approve the drifted
        # packet, which mints a matching hash — proceed.
        proceed, _ = self.agent._run_approval_bind_gate("Send", gathered)
        self.assertTrue(proceed)
        self.assertTrue(any("BINDING FAILED" in g for g in gathered))
        # Bound packet now matches the drifted (current) args.
        self.assertEqual(
            self.agent._bound_packet["payload_hash"],
            hash_packet_payload(drifted))

    def test_reapprove_declined_refuses_commit(self):
        self.agent._ask_fn = lambda _q: "approve"
        self.agent._approval_decision("Send email", FIELDS)
        self.agent._about_to_execute_fields = dict(FIELDS, to="Eve")
        self.agent._ask_fn = lambda _q: "cancel"
        proceed, _ = self.agent._run_approval_bind_gate("Send", [])
        self.assertFalse(proceed)


class ConfigFlagTests(unittest.TestCase):
    def test_env_values(self):
        cases = {
            "shadow": "shadow",
            "SHADOW": "shadow",
            "enforce": "enforce",
            "1": "enforce",
            "on": "enforce",
            "0": "off",
            "off": "off",
            "false": "off",
        }
        for raw, expected in cases.items():
            with patch.dict(os.environ, {"QUILL_APPROVAL_BIND": raw}):
                # Re-evaluate the same mapping config.py uses (default enforce).
                v = os.environ.get("QUILL_APPROVAL_BIND", "enforce").strip().lower()
                if v in ("0", "off", "false", "no"):
                    got = "off"
                elif v in ("shadow", "log"):
                    got = "shadow"
                elif v in ("enforce", "on", "1", "true", "yes"):
                    got = "enforce"
                else:
                    got = "enforce"
                self.assertEqual(got, expected, raw)


# --- plan 0.11 adversarial suite --------------------------------------------

class AdversarialDriftEnumerationTests(unittest.TestCase):
    """Every named drift case fails closed in enforce mode (plan 0.11)."""

    def setUp(self):
        self._prev = cfg.APPROVAL_BIND
        cfg.APPROVAL_BIND = "enforce"
        self.agent, self.store, self.rec = _agent_with_store()

    def tearDown(self):
        cfg.APPROVAL_BIND = self._prev
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

    def test_drift_cases_are_enumerated(self):
        names = {n for n, _ in ADVERSARIAL_DRIFT_CASES}
        for required in ("recipient", "price", "attachment"):
            self.assertIn(required, names)

    def test_each_enumerated_drift_fails_closed(self):
        for name, overrides in ADVERSARIAL_DRIFT_CASES:
            with self.subTest(case=name):
                self._approve()
                drifted = dict(FIELDS, **overrides)
                self.agent._about_to_execute_fields = drifted
                result = self.agent._approval_bind_check()
                self.assertTrue(result["block"], name)
                self.assertEqual(result["reason"], "drift", name)
                self.assertNotEqual(
                    hash_packet_payload(drifted),
                    hash_packet_payload(FIELDS),
                    name)
                # Diff must surface at least one changed key.
                for key in overrides:
                    self.assertIn(f"{key}:", result["diff"], name)
                self.agent._clear_bound_packet()

    def test_price_drift_surfaces_in_diff(self):
        self._approve()
        self.agent._about_to_execute_fields = dict(
            FIELDS, price="$55/seat", body="Following up on $55/seat.")
        result = self.agent._approval_bind_check()
        self.assertTrue(result["block"])
        self.assertIn("$49/seat", result["diff"])
        self.assertIn("$55/seat", result["diff"])

    def test_attachment_drift_surfaces_in_diff(self):
        self._approve()
        self.agent._about_to_execute_fields = dict(
            FIELDS, attachment="malware.zip")
        result = self.agent._approval_bind_check()
        self.assertTrue(result["block"])
        self.assertIn("quote-v1.pdf", result["diff"])
        self.assertIn("malware.zip", result["diff"])


class AdversarialExpiryTests(unittest.TestCase):
    def setUp(self):
        self._prev = cfg.APPROVAL_BIND
        cfg.APPROVAL_BIND = "enforce"
        self.agent, self.store, self.rec = _agent_with_store()

    def tearDown(self):
        cfg.APPROVAL_BIND = self._prev
        try:
            self.store.close()
        except Exception:
            pass

    def test_post_approval_expiry_refuses_even_with_matching_fields(self):
        self.agent._ask_fn = lambda _q: "approve"
        self.agent._approval_decision("Send email", FIELDS)
        bound = self.agent._bound_packet
        bound["expires_at"] = time.time() - 0.1
        self.store._conn.execute(
            "UPDATE action_packets SET expires_at = ? WHERE id = ?",
            (time.time() - 0.1, bound["packet_id"]))
        self.store._conn.commit()
        # Matching fields — still refused on clock.
        self.agent._about_to_execute_fields = dict(FIELDS)
        result = self.agent._approval_bind_check()
        self.assertTrue(result["block"])
        self.assertEqual(result["reason"], "expired")


class AdversarialDuplicateSendTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.store, self.rec = _agent_with_store()

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_same_hash_send_within_hour_refused(self):
        self.agent._ask_fn = lambda _q: "approve"
        self.agent._approval_decision("Send email", FIELDS)
        self.agent._stamp_executed_send("Send")
        result = self.agent._duplicate_send_check("Send")
        self.assertTrue(result["block"])
        self.assertEqual(result["reason"], "duplicate_send")

    def test_window_expiry_allows_resend(self):
        self.agent._ask_fn = lambda _q: "approve"
        decision, _ = self.agent._approval_decision("Send email", FIELDS)
        self.assertEqual(decision, "approve")
        pid = self.agent._bound_packet["packet_id"]
        h = hash_packet_payload(FIELDS)
        self.store.set_packet_executed_hash(
            pid, h, executed_at=time.time() - (_DUP_SEND_WINDOW_S + 30))
        self.agent._recent_executed_hashes.clear()
        result = self.agent._duplicate_send_check("Send")
        self.assertFalse(result["block"])


class AdversarialAutonomousPolicyTests(unittest.TestCase):
    """Autonomous mode bypasses the ask, never RISK_TABLE (plan 0.7/0.11)."""

    def test_autonomous_cannot_approve_delete(self):
        agent = Agent(on_log=lambda _s: None, on_ask=lambda _q: "approve")
        agent._autonomous_run = True
        decision, _ = agent._approval_decision(
            "Delete the selected messages",
            {"action": "Delete"})
        self.assertEqual(decision, "cancel")

    def test_autonomous_cannot_approve_remove(self):
        agent = Agent(on_log=lambda _s: None, on_ask=lambda _q: "approve")
        agent._autonomous_run = True
        decision, _ = agent._approval_decision(
            "Remove this contact",
            {"action": "Remove"})
        self.assertEqual(decision, "cancel")

    def test_policy_helper_blocks_delete_even_when_ask_would_say_yes(self):
        self.assertIsNotNone(_policy_block_reason(label="Delete"))
        self.assertIsNotNone(_policy_block_reason(
            summary="click 'Remove'", fields={"action": "click 'Remove'"}))
        # Send-class remains allowed through the policy layer.
        self.assertIsNone(_policy_block_reason(label="Send"))

    def test_autonomous_still_auto_approves_send(self):
        agent = Agent(on_log=lambda _s: None, on_ask=lambda _q: "cancel")
        agent._autonomous_run = True
        decision, _ = agent._approval_decision(
            "Send email to Marc", dict(FIELDS))
        self.assertEqual(decision, "approve")


if __name__ == "__main__":
    unittest.main()
