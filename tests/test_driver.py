"""Unit tests for desktop_agent.driver.DesktopDriver — the executor discipline.

Complements test_guards.py: where those test the pure decisions, these test that
the driver *honors* them — the approval gate stops denied actions, the per-task
budget is enforced, jail escapes are refused before any side effect, and both
executed and refused actions land in the audit log.

Every test is hermetic: a throwaway jail, a patched sessions dir, and no real
external process is ever spawned (only refusal/denial paths touch launch/run).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from desktop_agent import config as cfg  # noqa: E402
from desktop_agent.driver import DesktopDriver, _wants_action  # noqa: E402
from desktop_agent.guards import Tier  # noqa: E402


class DriverTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.jail = Path(tempfile.mkdtemp(prefix="jail_")).resolve()
        self._sessions = Path(tempfile.mkdtemp(prefix="sessions_")).resolve()
        # Save + patch module-level policy the driver reads at call time.
        self._saved = {
            "SESSIONS_ROOT": cfg.SESSIONS_ROOT,
            "REQUIRE_APPROVAL": cfg.REQUIRE_APPROVAL,
            "MAX_ACTIONS_PER_TASK": cfg.MAX_ACTIONS_PER_TASK,
        }
        cfg.SESSIONS_ROOT = self._sessions
        cfg.REQUIRE_APPROVAL = True
        self.approve = True           # what the approval hook returns
        self.approve_calls = 0
        self.logs: list[str] = []
        self.driver = DesktopDriver(
            on_log=self.logs.append,
            on_approve=self._on_approve,
            jail_root=self.jail,
        )

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(cfg, k, v)

    def _on_approve(self, summary: str, details: str = "") -> bool:
        self.approve_calls += 1
        return self.approve

    def audit_records(self) -> list[dict]:
        path = self._sessions / "desktop_audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class ApprovalGateTests(DriverTestBase):
    def test_mutating_action_asks_and_proceeds_when_approved(self) -> None:
        self.approve = True
        r = self.driver.make_dir("proj")
        self.assertTrue(r["ok"])
        self.assertEqual(self.approve_calls, 1)
        self.assertTrue((self.jail / "proj").is_dir())

    def test_mutating_action_denied_does_not_execute(self) -> None:
        self.approve = False
        r = self.driver.make_dir("proj")
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "denied")
        self.assertFalse((self.jail / "proj").exists())

    def test_read_only_action_needs_no_approval(self) -> None:
        r = self.driver.list_dir("")
        self.assertTrue(r["ok"])
        self.assertEqual(r["tier"], Tier.READ_ONLY.value)
        self.assertEqual(self.approve_calls, 0)

    def test_approval_disabled_auto_proceeds(self) -> None:
        cfg.REQUIRE_APPROVAL = False
        r = self.driver.make_dir("proj")
        self.assertTrue(r["ok"])
        self.assertEqual(self.approve_calls, 0)


class WriteFileTests(DriverTestBase):
    def test_write_and_read_back(self) -> None:
        r = self.driver.write_file("notes/readme.txt", "hello world")
        self.assertTrue(r["ok"])
        target = self.jail / "notes" / "readme.txt"
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world")

    def test_escape_refused(self) -> None:
        r = self.driver.write_file(r"..\..\escape.txt", "x")
        self.assertFalse(r["ok"])
        self.assertEqual(r["tier"], Tier.BLOCKED.value)

    def test_reserved_device_name_refused(self) -> None:
        r = self.driver.write_file("NUL", "x")
        self.assertFalse(r["ok"])

    def test_secret_marker_refused(self) -> None:
        r = self.driver.write_file("config/.env", "SECRET=1")
        self.assertFalse(r["ok"])
        self.assertFalse((self.jail / "config" / ".env").exists())

    def test_size_cap_enforced(self) -> None:
        saved = cfg.MAX_FILE_BYTES
        cfg.MAX_FILE_BYTES = 10
        try:
            r = self.driver.write_file("big.txt", "x" * 50)
            self.assertFalse(r["ok"])
            self.assertIn("too large", r["detail"])
        finally:
            cfg.MAX_FILE_BYTES = saved


class BudgetTests(DriverTestBase):
    def test_budget_exhausts_then_resets(self) -> None:
        cfg.MAX_ACTIONS_PER_TASK = 2
        self.assertTrue(self.driver.make_dir("a")["ok"])
        self.assertTrue(self.driver.make_dir("b")["ok"])
        blocked = self.driver.make_dir("c")
        self.assertFalse(blocked["ok"])
        self.assertIn("budget", blocked["detail"])
        # new_task() resets the per-task counter.
        self.driver.new_task()
        self.assertTrue(self.driver.make_dir("d")["ok"])


class LaunchAndRunRefusalTests(DriverTestBase):
    """Only refusal/denial paths — no real process is ever spawned."""

    def test_unknown_app_refused(self) -> None:
        # Not in the registry and not installed: discovery finds nothing.
        r = self.driver.launch_app("definitely_not_installed_xyz")
        self.assertFalse(r["ok"])
        self.assertIn("not found on this machine", r["detail"])

    @unittest.skipUnless(os.name == "nt", "notepad allowlist entry is Windows-only")
    def test_launch_with_escaping_arg_refused(self) -> None:
        # resolves a real app, then refuses the out-of-jail argument before Popen.
        r = self.driver.launch_app("notepad", [r"C:\Windows\win.ini"])
        self.assertFalse(r["ok"])
        self.assertIn("outside jail", r["detail"])

    def test_run_blocked_verb_refused(self) -> None:
        r = self.driver.run_command(["rm", "-rf", "x"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["tier"], Tier.BLOCKED.value)

    def test_run_cwd_outside_jail_refused(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="outside_")).resolve()
        r = self.driver.run_command(["dir"], cwd=str(outside))
        self.assertFalse(r["ok"])
        self.assertIn("outside jail", r["detail"])

    def test_run_denied_does_not_execute(self) -> None:
        self.approve = False
        r = self.driver.run_command(["npm", "install"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "denied")


class CapabilityEnforcementTests(DriverTestBase):
    """launch_app only points an app at targets its capability entry declares.

    The allow paths are checked through the fs-classifying helper (which never
    spawns); the refuse path is checked end-to-end through launch_app, which
    refuses before Popen.
    """

    def test_editor_allowed_on_folder(self) -> None:
        (self.jail / "proj").mkdir()
        self.assertIsNone(
            self.driver._open_targets_ok("cursor", [str(self.jail / "proj")]))

    def test_editor_allowed_on_new_unmade_folder(self) -> None:
        # make_dir -> launch flow: a not-yet-existing, extensionless path reads
        # as a folder-open intent.
        self.assertIsNone(
            self.driver._open_targets_ok("cursor", [str(self.jail / "fresh")]))

    def test_flstudio_allowed_on_project_file(self) -> None:
        (self.jail / "song.flp").write_text("x")
        self.assertIsNone(
            self.driver._open_targets_ok("flstudio", [str(self.jail / "song.flp")]))

    def test_flstudio_refuses_text_file(self) -> None:
        (self.jail / "notes.txt").write_text("x")
        r = self.driver._open_targets_ok("flstudio", [str(self.jail / "notes.txt")])
        self.assertIsNotNone(r)
        self.assertIn("cannot open", r)

    def test_terminal_refuses_any_target(self) -> None:
        (self.jail / "proj").mkdir()
        self.assertIsNotNone(
            self.driver._open_targets_ok("terminal", [str(self.jail / "proj")]))

    def test_flags_are_exempt(self) -> None:
        self.assertIsNone(self.driver._open_targets_ok("chrome", ["--incognito"]))

    @unittest.skipUnless(os.name == "nt", "notepad allowlist entry is Windows-only")
    def test_launch_app_refuses_folder_for_notepad_end_to_end(self) -> None:
        # notepad resolves on Windows, so we reach the capability gate; it
        # refuses a folder target before any process is spawned.
        (self.jail / "proj").mkdir()
        r = self.driver.launch_app("notepad", [str(self.jail / "proj")])
        self.assertFalse(r["ok"])
        self.assertIn("does not open folders", r["detail"])


class ApprovalVerbThreadingTests(DriverTestBase):
    """The gate hands the action verb to callbacks that opt in (for granular
    autonomy), while older two-arg callbacks keep working unchanged."""

    def test_wants_action_detection(self) -> None:
        self.assertTrue(_wants_action(lambda s, d="", action=None: True))
        self.assertTrue(_wants_action(lambda s, d="", **k: True))
        self.assertFalse(_wants_action(lambda s, d="": True))

    def test_verb_is_delivered_to_action_aware_callback(self) -> None:
        seen = []

        def cb(summary, details="", action=None):
            seen.append(action)
            return True

        d = DesktopDriver(on_log=lambda s: None, on_approve=cb, jail_root=self.jail)
        d.make_dir("proj")
        d.write_file("proj/a.txt", "x")
        self.assertEqual(seen, ["make_dir", "write_file"])

    def test_two_arg_callback_still_works(self) -> None:
        calls = []

        def cb(summary, details=""):        # no `action` param — legacy shape
            calls.append(summary)
            return True

        d = DesktopDriver(on_log=lambda s: None, on_approve=cb, jail_root=self.jail)
        self.assertTrue(d.make_dir("proj")["ok"])
        self.assertEqual(len(calls), 1)

    def test_callback_can_gate_by_verb(self) -> None:
        # a policy-driven callback auto-approves make_dir but denies write_file.
        def cb(summary, details="", action=None):
            return action == "make_dir"

        d = DesktopDriver(on_log=lambda s: None, on_approve=cb, jail_root=self.jail)
        self.assertTrue(d.make_dir("proj")["ok"])
        r = d.write_file("proj/a.txt", "x")
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "denied")


class AuditTests(DriverTestBase):
    def test_executed_and_refused_actions_are_logged(self) -> None:
        self.driver.make_dir("proj")                 # executed -> ok
        self.driver.write_file(r"..\escape.txt", "x")  # refused -> blocked
        self.driver.run_command(["rm", "x"])           # refused -> blocked
        records = self.audit_records()
        outcomes = [r.get("outcome") for r in records]
        self.assertIn("ok", outcomes)
        self.assertEqual(outcomes.count("blocked"), 2)
        # every record carries a timestamp.
        self.assertTrue(all("ts" in r for r in records))


if __name__ == "__main__":
    unittest.main(verbosity=2)
