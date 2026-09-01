"""Desktop mutating-gate approval binding (plan task 0.5).

Same contract as the browser commit gate: after approve, re-hash about-to-
execute args; require hash == payload_hash and now < expires_at. Enforce
fails closed; shadow logs and allows.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.agent_log import Recorder, hash_packet_payload
from app.storage import Store
from desktop_agent import config as dcfg
from desktop_agent.driver import DesktopDriver, _fields_diff, _hash_fields
from desktop_agent.guards import Tier


FIELDS = {
    "action": "write_file",
    "path": "/jail/notes.txt",
    "content": "hello Marc",
    "bytes": 10,
}


class HashParityTests(unittest.TestCase):
    def test_desktop_hash_matches_storage(self):
        self.assertEqual(_hash_fields(FIELDS), hash_packet_payload(FIELDS))

    def test_diff_lists_changed_keys(self):
        drifted = dict(FIELDS, content="hello Eve")
        diff = _fields_diff(FIELDS, drifted)
        self.assertIn("content:", diff)
        self.assertIn("Marc", diff)
        self.assertIn("Eve", diff)


class DesktopBindCheckTests(unittest.TestCase):
    def setUp(self):
        self._prev_bind = dcfg.APPROVAL_BIND
        self._prev_req = dcfg.REQUIRE_APPROVAL
        dcfg.APPROVAL_BIND = "enforce"
        dcfg.REQUIRE_APPROVAL = True
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.rec = Recorder(store=self.store)
        self.rec.start_run("desktop write", surface="desktop")
        self.logs = []
        self.driver = DesktopDriver(
            on_log=self.logs.append,
            on_approve=lambda *a, **k: True,
            jail_root=self.tmp / "jail",
            on_record_packet=lambda fields, summary: self.rec.record_packet(
                summary=summary, fields=fields, execution_surface="desktop"),
            on_get_packet=self.store.get_action_packet,
        )

    def tearDown(self):
        dcfg.APPROVAL_BIND = self._prev_bind
        dcfg.REQUIRE_APPROVAL = self._prev_req
        try:
            self.store.close()
        except Exception:
            pass

    def test_matching_fields_ok(self):
        self.driver._bind_approved_packet(None, FIELDS)
        result = self.driver._approval_bind_check()
        self.assertFalse(result["block"])
        self.assertEqual(result["reason"], "ok")

    def test_content_drift_fails_closed(self):
        self.driver._bind_approved_packet(None, FIELDS)
        self.driver._about_to_execute_fields = dict(FIELDS, content="pwned")
        result = self.driver._approval_bind_check()
        self.assertTrue(result["block"])
        self.assertEqual(result["reason"], "drift")
        self.assertIn("pwned", result["diff"])

    def test_expiry_fails_closed(self):
        self.driver._bind_approved_packet(None, FIELDS)
        self.driver._bound_packet["expires_at"] = time.time() - 1
        result = self.driver._approval_bind_check()
        self.assertTrue(result["block"])
        self.assertEqual(result["reason"], "expired")

    def test_persisted_packet_is_source_of_truth(self):
        pid = self.rec.record_packet(summary="write", fields=FIELDS,
                                     execution_surface="desktop")
        self.driver._bind_approved_packet(pid, FIELDS)
        self.store._conn.execute(
            "UPDATE action_packets SET payload_hash = ? WHERE id = ?",
            ("0" * 64, pid))
        self.store._conn.commit()
        result = self.driver._approval_bind_check()
        self.assertTrue(result["block"])
        self.assertEqual(result["reason"], "drift")


class DesktopShadowAndOffTests(unittest.TestCase):
    def setUp(self):
        self._prev_bind = dcfg.APPROVAL_BIND
        self._prev_req = dcfg.REQUIRE_APPROVAL
        dcfg.REQUIRE_APPROVAL = True
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        dcfg.APPROVAL_BIND = self._prev_bind
        dcfg.REQUIRE_APPROVAL = self._prev_req

    def test_shadow_logs_drift_without_blocking(self):
        dcfg.APPROVAL_BIND = "shadow"
        logs = []
        d = DesktopDriver(on_log=logs.append, on_approve=lambda *a, **k: True,
                          jail_root=self.tmp / "jail")
        d._bind_approved_packet(None, FIELDS)
        d._about_to_execute_fields = dict(FIELDS, content="pwned")
        result = d._approval_bind_check()
        self.assertFalse(result["block"])
        self.assertEqual(result["reason"], "drift")
        self.assertTrue(any("approval-bind/shadow" in line for line in logs))

    def test_off_skips_check(self):
        dcfg.APPROVAL_BIND = "off"
        d = DesktopDriver(on_approve=lambda *a, **k: True,
                          jail_root=self.tmp / "jail")
        d._bind_approved_packet(None, FIELDS)
        d._about_to_execute_fields = dict(FIELDS, content="pwned")
        result = d._approval_bind_check()
        self.assertFalse(result["block"])
        self.assertEqual(result["reason"], "off")


class DesktopGateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._prev_bind = dcfg.APPROVAL_BIND
        self._prev_req = dcfg.REQUIRE_APPROVAL
        dcfg.APPROVAL_BIND = "enforce"
        dcfg.REQUIRE_APPROVAL = True
        self.tmp = Path(tempfile.mkdtemp())
        self.jail = self.tmp / "jail"
        self.asks = []

    def tearDown(self):
        dcfg.APPROVAL_BIND = self._prev_bind
        dcfg.REQUIRE_APPROVAL = self._prev_req

    def test_gate_records_and_binds_on_approve(self):
        store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        rec = Recorder(store=store)
        rec.start_run("desktop", surface="desktop")
        d = DesktopDriver(
            on_approve=lambda *a, **k: True,
            jail_root=self.jail,
            on_record_packet=lambda fields, summary: rec.record_packet(
                summary=summary, fields=fields, execution_surface="desktop"),
            on_get_packet=store.get_action_packet,
        )
        ok = d._gate(Tier.MUTATING, "create folder x", verb="make_dir",
                     fields={"action": "make_dir", "path": str(self.jail / "x")})
        self.assertTrue(ok)
        self.assertIsNotNone(d._bound_packet)
        self.assertEqual(d._bound_packet["payload_hash"],
                         _hash_fields(d._bound_packet["fields"]))
        # Packet landed in the store.
        run = store.agent_run(rec.current_run_id)
        self.assertEqual(len(run["packets"]), 1)
        self.assertEqual(run["packets"][0]["execution_surface"], "desktop")
        store.close()

    def test_gate_refuses_drift_when_reapprove_cancelled(self):
        answers = iter([True, False])  # approve once, cancel re-ask

        def ask(summary, details="", action=None):
            self.asks.append(summary)
            return next(answers)

        d = DesktopDriver(on_approve=ask, jail_root=self.jail)
        orig_bind = d._bind_approved_packet

        def bind_then_drift(pid, fields):
            orig_bind(pid, fields)
            d._about_to_execute_fields = dict(fields, path="/evil/path")

        d._bind_approved_packet = bind_then_drift
        ok = d._gate(Tier.MUTATING, "create folder x", verb="make_dir",
                     fields={"action": "make_dir", "path": str(self.jail / "x")})
        self.assertFalse(ok)
        self.assertEqual(len(self.asks), 2)
        self.assertIn("approval binding failed", self.asks[1])

    def test_gate_reapprove_current_fields_then_proceeds(self):
        answers = iter([True, True])  # approve, then re-approve after drift

        def ask(summary, details="", action=None):
            return next(answers)

        d = DesktopDriver(on_approve=ask, jail_root=self.jail)
        orig_bind = d._bind_approved_packet
        drifted_once = {"n": 0}

        def bind_then_drift(pid, fields):
            orig_bind(pid, fields)
            # Only inject drift on the first bind; the re-approve bind must stick.
            if drifted_once["n"] == 0:
                drifted_once["n"] += 1
                d._about_to_execute_fields = dict(fields, path="/evil/path")

        d._bind_approved_packet = bind_then_drift
        ok = d._gate(Tier.MUTATING, "create folder x", verb="make_dir",
                     fields={"action": "make_dir", "path": str(self.jail / "x")})
        self.assertTrue(ok)
        self.assertEqual(d._bound_packet["fields"]["path"], "/evil/path")

    def test_write_file_denied_on_bind_failure_does_not_write(self):
        answers = iter([True, False])

        def ask(summary, details="", action=None):
            return next(answers)

        d = DesktopDriver(on_approve=ask, jail_root=self.jail)
        orig_bind = d._bind_approved_packet

        def bind_then_drift(pid, fields):
            orig_bind(pid, fields)
            d._about_to_execute_fields = dict(fields, content="pwned")

        d._bind_approved_packet = bind_then_drift
        res = d.write_file("notes.txt", "hello Marc")
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("detail"), "denied")
        self.assertFalse((self.jail / "notes.txt").exists())


class DesktopMakeDirHappyPathTests(unittest.TestCase):
    def setUp(self):
        self._prev_bind = dcfg.APPROVAL_BIND
        self._prev_req = dcfg.REQUIRE_APPROVAL
        dcfg.APPROVAL_BIND = "shadow"
        dcfg.REQUIRE_APPROVAL = True
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        dcfg.APPROVAL_BIND = self._prev_bind
        dcfg.REQUIRE_APPROVAL = self._prev_req

    def test_approved_make_dir_still_works(self):
        d = DesktopDriver(on_approve=lambda *a, **k: True,
                          jail_root=self.tmp / "jail")
        res = d.make_dir("project_a")
        self.assertTrue(res["ok"])
        self.assertTrue((self.tmp / "jail" / "project_a").is_dir())
        self.assertIsNotNone(d._bound_packet)
        self.assertEqual(d._bound_packet["fields"]["action"], "make_dir")


class DesktopAskMintsPendingTests(unittest.TestCase):
    """Wired desktop `_approve` must mint a pending packet before the ask.

    Plan 0.6 refuses Hold-to-seal / Yes without `{packet_id, payload_hash}`.
    The old prompt-only path left write_file approvals stuck on screen.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.rec = Recorder(store=self.store)
        self.rec.start_run("write solitaire", surface="desktop")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_wired_approve_sets_pending_before_ask(self):
        from browser_agent.orchestrator import Agent

        seen = {}

        def ask(prompt):
            seen["pending"] = dict(agent._pending_approval_packet or {})
            seen["prompt"] = prompt
            return "approve"

        agent = Agent(recorder=self.rec, on_log=lambda _s: None, on_ask=ask)
        d = agent._desktop()
        if d is None:
            self.skipTest("desktop agent unavailable")
        fields = {
            "action": "write_file",
            "path": str(self.tmp / "index.html"),
            "content": "<h1>hi</h1>",
            "bytes": 10,
        }
        ok = d._ask(
            "write 10 bytes to index.html (new)",
            "first line: <h1>hi</h1>",
            action="write_file",
            fields=fields,
        )
        self.assertTrue(ok)
        self.assertTrue(seen.get("pending"))
        self.assertIsNotNone(seen["pending"].get("packet_id"))
        self.assertTrue(seen["pending"].get("payload_hash"))
        self.assertEqual(seen["pending"]["fields"]["path"], fields["path"])
        self.assertIn("index.html", seen.get("prompt") or "")

    def test_force_ask_still_prompts_when_autonomous_ceiling_exceeded(self):
        from browser_agent.orchestrator import Agent
        from desktop_agent import config as dcfg

        prev = dcfg.AGENT_AUTONOMY_DESKTOP
        dcfg.AGENT_AUTONOMY_DESKTOP = "launch_only"
        asked = {"n": 0}

        def ask(_prompt):
            asked["n"] += 1
            return "approve"

        try:
            agent = Agent(recorder=self.rec, on_log=lambda _s: None, on_ask=ask)
            agent._autonomous_run = True
            d = agent._desktop()
            if d is None:
                self.skipTest("desktop agent unavailable")
            ok = d._ask(
                "write 10 bytes to index.html (new)",
                action="write_file",
                fields={"action": "write_file", "path": "index.html",
                        "content": "x", "bytes": 1},
            )
            self.assertTrue(ok)
            self.assertEqual(asked["n"], 1)
        finally:
            dcfg.AGENT_AUTONOMY_DESKTOP = prev


if __name__ == "__main__":
    unittest.main()
