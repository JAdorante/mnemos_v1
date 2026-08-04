"""Autonomous bypasses the ask, never the policy (plan task 0.7).

RISK_TABLE is the single source: delete/remove stay blocked in every mode,
including autonomous auto-approve on browser commits and desktop gates.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.agent_planner import (
    RISK_TABLE,
    classify_risk,
    execution_allowed,
    is_policy_blocked,
    kind_for_shell_verb,
    policy_block_reason,
    risk_of,
)
from browser_agent.orchestrator import Agent, _policy_block_reason
from desktop_agent import config as dcfg
from desktop_agent.driver import DesktopDriver
from desktop_agent.guards import Tier, classify_command, policy_blocks, scan_danger


class RiskTablePolicyTests(unittest.TestCase):
    def test_delete_remove_are_blocked(self):
        self.assertEqual(RISK_TABLE["delete"], "blocked")
        self.assertEqual(RISK_TABLE["remove"], "blocked")
        self.assertEqual(classify_risk("delete")[0], "blocked")
        self.assertEqual(classify_risk("remove")[0], "blocked")

    def test_execution_allowed_false_for_blocked(self):
        self.assertFalse(execution_allowed("blocked"))
        self.assertTrue(execution_allowed("high"))
        self.assertTrue(execution_allowed("low"))

    def test_risk_of_delete_goal(self):
        risk, approval = risk_of("delete the old draft email")
        self.assertEqual(risk, "blocked")
        self.assertTrue(approval)

    def test_is_policy_blocked_on_kind_and_label(self):
        self.assertTrue(is_policy_blocked(kind="delete"))
        self.assertTrue(is_policy_blocked(label="Delete forever"))
        self.assertTrue(is_policy_blocked(summary="Remove this contact"))
        self.assertFalse(is_policy_blocked(kind="send", goal="send the follow-up"))
        self.assertFalse(is_policy_blocked(label="Send"))

    def test_shell_verbs_map_to_delete(self):
        for verb in ("rm", "del", "erase", "rmdir", "rd"):
            self.assertEqual(kind_for_shell_verb(verb), "delete", verb)
        self.assertIsNone(kind_for_shell_verb("npm"))


class DesktopPolicyGateTests(unittest.TestCase):
    def setUp(self):
        self._prev_req = dcfg.REQUIRE_APPROVAL
        dcfg.REQUIRE_APPROVAL = True
        self.tmp = Path(tempfile.mkdtemp())
        self.asks = []

    def tearDown(self):
        dcfg.REQUIRE_APPROVAL = self._prev_req

    def test_scan_danger_uses_risk_table_for_rm(self):
        reason = scan_danger(["rm", "-rf", "x"])
        self.assertIsNotNone(reason)
        self.assertIn("blocked by policy", reason)

    def test_classify_command_blocks_del(self):
        tier, reason = classify_command(["del", "notes.txt"])
        self.assertEqual(tier, Tier.BLOCKED)
        self.assertIn("policy", reason)

    def test_gate_refuses_delete_even_when_ask_would_approve(self):
        d = DesktopDriver(
            on_approve=lambda *a, **k: True,  # would auto-approve
            jail_root=self.tmp / "jail",
        )
        ok = d._gate(
            Tier.MUTATING,
            summary="activate Delete in the 'Files' window",
            verb="ui_invoke",
            fields={"action": "ui_invoke", "label": "button: Delete"},
        )
        self.assertFalse(ok)
        self.assertIn("policy", d._last_gate_reason or "")

    def test_gate_allows_send_class_to_reach_ask(self):
        def ask(summary, details="", action=None):
            self.asks.append(summary)
            return True

        d = DesktopDriver(on_approve=ask, jail_root=self.tmp / "jail")
        ok = d._gate(
            Tier.MUTATING, summary="write notes.txt",
            verb="write_file",
            fields={"action": "write_file", "path": "notes.txt", "content": "hi"},
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.asks), 1)

    def test_autonomous_ask_callback_cannot_unlock_delete(self):
        # Simulate orchestrator auto-approve callback + REQUIRE_APPROVAL still on:
        # policy runs inside _gate before the ask.
        d = DesktopDriver(
            on_approve=lambda *a, **k: True,
            jail_root=self.tmp / "jail",
        )
        reason = policy_blocks(
            verb="ui_invoke",
            summary="activate Remove in the window",
            label="Remove",
            fields={"action": "ui_invoke", "label": "Remove"},
        )
        self.assertIsNotNone(reason)
        ok = d._gate(Tier.MUTATING, "activate Remove", verb="ui_invoke",
                     fields={"action": "ui_invoke", "label": "Remove"})
        self.assertFalse(ok)

    def test_require_approval_off_still_blocks_policy(self):
        dcfg.REQUIRE_APPROVAL = False
        d = DesktopDriver(on_approve=lambda *a, **k: True,
                          jail_root=self.tmp / "jail")
        ok = d._gate(
            Tier.MUTATING, "delete the folder",
            verb="ui_invoke",
            fields={"action": "delete", "label": "Delete"},
        )
        self.assertFalse(ok)


class BrowserAutonomousPolicyTests(unittest.TestCase):
    def test_policy_helper_blocks_delete_label(self):
        self.assertIsNotNone(_policy_block_reason(label="Delete"))
        self.assertIsNotNone(_policy_block_reason(
            summary="click 'Remove'", fields={"action": "click 'Remove'"}))
        self.assertIsNone(_policy_block_reason(label="Send"))

    def test_autonomous_approval_decision_cancels_delete(self):
        agent = Agent(on_log=lambda _s: None, on_ask=lambda _q: "approve")
        agent._autonomous_run = True
        decision, _ = agent._approval_decision(
            "Delete the selected messages",
            {"action": "Delete"})
        self.assertEqual(decision, "cancel")

    def test_autonomous_approval_decision_allows_send(self):
        agent = Agent(on_log=lambda _s: None, on_ask=lambda _q: "cancel")
        agent._autonomous_run = True
        decision, _ = agent._approval_decision(
            "Send email to Marc",
            {"action": "Send email", "to": "Marc"})
        self.assertEqual(decision, "approve")  # ask bypassed, policy allows

    def test_desktop_approve_callback_refuses_delete(self):
        agent = Agent(on_log=lambda _s: None, on_ask=lambda _q: "approve")
        agent._autonomous_run = True
        # Build the wired desktop approve without launching a real driver.
        with patch.dict("os.environ", {"QUILL_DESKTOP": "1"}):
            d = agent._desktop()
        if d is None:
            self.skipTest("desktop agent unavailable")
        # The on_approve closure refuses policy-blocked summaries.
        ok = d._ask("activate Delete in Files", "", action="ui_invoke")
        self.assertFalse(ok)


class PlannerBlockedDispatchTests(unittest.TestCase):
    def test_policy_block_reason_message(self):
        msg = policy_block_reason(kind="delete", goal="delete old files")
        self.assertIn("blocked by policy", msg)
        self.assertIn("autonomous", msg)


if __name__ == "__main__":
    unittest.main()
