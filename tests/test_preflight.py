"""Unit tests for desktop_agent.preflight — the deterministic "what's possible
here" snapshot handed to the planner before the desktop loop runs.

Pure reporting over config + a path probe, so it's exercised without launching
anything. Environment flags (pixel UI, approval, budget) are patched per test.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

if sys.platform != "win32":
    raise unittest.SkipTest(
        "Windows-only: preflight probes Windows desktop app availability")

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from desktop_agent import config as cfg          # noqa: E402
from desktop_agent import preflight as pf         # noqa: E402


class DetectAppsTests(unittest.TestCase):
    def test_detects_named_apps(self) -> None:
        self.assertEqual(pf.detect_apps("make a song in FL Studio"), ["flstudio"])
        self.assertEqual(pf.detect_apps("open Cursor and start a project"), ["cursor"])
        self.assertEqual(pf.detect_apps("open notepad please"), ["notepad"])
        self.assertIn("explorer", pf.detect_apps("reveal it in file explorer"))

    def test_word_boundary_avoids_false_positive(self) -> None:
        # "encode" must not match the generic bare key "code".
        self.assertEqual(pf.detect_apps("encode a video file"), [])

    def test_empty_goal(self) -> None:
        self.assertEqual(pf.detect_apps(""), [])
        self.assertEqual(pf.detect_apps("say hello"), [])


class PreflightBase(unittest.TestCase):
    _FLAGS = ("PIXEL_UI", "PIXEL_VISION", "REQUIRE_APPROVAL", "MAX_ACTIONS_PER_TASK",
              "AGENT_AUTONOMY_DESKTOP", "AGENT_AUTONOMY_SHELL")

    def setUp(self) -> None:
        self._saved = {k: getattr(cfg, k) for k in self._FLAGS}

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(cfg, k, v)


class EnvironmentTests(PreflightBase):
    def test_pixel_ui_off_blocks_ui_actions(self) -> None:
        cfg.PIXEL_UI = False
        r = pf.preflight("do a desktop thing")
        blocked = {b["action"] for b in r["blocked_actions"]}
        self.assertEqual({"screenshot", "click_at", "type_text", "press_key"}, blocked)
        for a in ("click_at", "type_text", "press_key"):
            self.assertNotIn(a, r["allowed_next_actions"])
        self.assertFalse(r["pixel_ui_enabled"])

    def test_pixel_ui_on_enables_ui_actions(self) -> None:
        cfg.PIXEL_UI = True
        r = pf.preflight("do a desktop thing")
        self.assertEqual(r["blocked_actions"], [])
        for a in ("click_at", "type_text", "press_key", "screenshot"):
            self.assertIn(a, r["allowed_next_actions"])

    def test_approval_reflects_run_mode(self) -> None:
        self.assertFalse(pf.preflight("x", autonomous=True)["approval_required"])
        self.assertTrue(pf.preflight("x", autonomous=False)["approval_required"])

    def test_approval_defaults_to_config(self) -> None:
        cfg.REQUIRE_APPROVAL = True
        self.assertTrue(pf.preflight("x")["approval_required"])
        cfg.REQUIRE_APPROVAL = False
        self.assertFalse(pf.preflight("x")["approval_required"])

    def test_budget_accounts_for_actions_used(self) -> None:
        cfg.MAX_ACTIONS_PER_TASK = 10
        r = pf.preflight("x", actions_used=4)
        self.assertEqual(r["budget"], {"max": 10, "remaining": 6})

    def test_exhausted_budget_is_blocked(self) -> None:
        cfg.MAX_ACTIONS_PER_TASK = 10
        r = pf.preflight("x", actions_used=10)
        self.assertEqual(r["budget"]["remaining"], 0)
        self.assertTrue(any(b["action"] == "*" for b in r["blocked_actions"]))

    def test_jail_exists_reflects_disk(self) -> None:
        jail = Path(tempfile.mkdtemp(prefix="jail_")).resolve()
        self.assertTrue(pf.preflight("x", jail=jail)["jail_exists"])
        self.assertFalse(pf.preflight("x", jail=jail / "nope")["jail_exists"])

    def test_always_usable_actions_present(self) -> None:
        for a in ("make_dir", "write_file", "launch_app", "run_command",
                  "list_dir", "ask_human", "done"):
            self.assertIn(a, pf.preflight("x")["allowed_next_actions"])


class FocusTests(PreflightBase):
    def test_unknown_app_not_launchable_with_recoveries(self) -> None:
        r = pf.preflight("make music in Ableton", app="ableton")
        f = r["focus"]
        self.assertEqual(f["app"], "ableton")
        self.assertFalse(f["can_launch"])
        self.assertIsNone(f["resolved_path"])
        self.assertTrue(f["recoveries"])

    def test_daw_opens_files_not_generic(self) -> None:
        self.assertTrue(pf.preflight("x", app="flstudio")["focus"]["can_open_project"])
        # terminal opens no target at all.
        self.assertFalse(pf.preflight("x", app="terminal")["focus"]["can_open_project"])

    def test_can_click_type_tracks_pixel_ui(self) -> None:
        cfg.PIXEL_UI = False
        self.assertFalse(pf.preflight("x", app="flstudio")["focus"]["can_click_type"])
        cfg.PIXEL_UI = True
        self.assertTrue(pf.preflight("x", app="flstudio")["focus"]["can_click_type"])

    def test_no_target_no_focus(self) -> None:
        self.assertIsNone(pf.preflight("just do something vague")["focus"])

    @unittest.skipUnless(os.name == "nt", "app resolution is Windows-specific here")
    def test_installed_app_resolves(self) -> None:
        r = pf.preflight("open notepad", app="notepad")
        self.assertTrue(r["apps"]["notepad"]["installed"])
        self.assertTrue(r["focus"]["can_launch"])
        self.assertIsNotNone(r["focus"]["resolved_path"])


class AutonomyBlockTests(PreflightBase):
    def test_no_autonomy_block_when_gated(self) -> None:
        self.assertIsNone(pf.preflight("x", autonomous=False)["autonomy"])

    def test_autonomy_split_reported(self) -> None:
        cfg.AGENT_AUTONOMY_DESKTOP = "jailed_files"
        cfg.AGENT_AUTONOMY_SHELL = False
        a = pf.preflight("x", autonomous=True)["autonomy"]
        self.assertEqual(a["level"], "jailed_files")
        self.assertIn("launch_app", a["auto_verbs"])
        self.assertIn("write_file", a["auto_verbs"])
        self.assertIn("click_at", a["gated_verbs"])
        self.assertIn("run_command", a["gated_verbs"])

    def test_full_plus_shell_gates_nothing(self) -> None:
        cfg.AGENT_AUTONOMY_DESKTOP = "full"
        cfg.AGENT_AUTONOMY_SHELL = True
        a = pf.preflight("x", autonomous=True)["autonomy"]
        self.assertEqual(a["gated_verbs"], [])

    def test_autonomy_line_renders(self) -> None:
        cfg.AGENT_AUTONOMY_DESKTOP = "jailed_files"
        block = pf.format_preflight(pf.preflight("open Cursor", autonomous=True))
        self.assertIn("autonomy (desktop=jailed_files", block)
        block.encode("ascii")


class RenderTests(PreflightBase):
    def test_summary_and_block_render(self) -> None:
        cfg.PIXEL_UI = False
        r = pf.preflight("make a song in FL Studio", autonomous=True)
        summary = pf.summary_line(r)
        block = pf.format_preflight(r)
        self.assertIn("preflight:", summary)
        self.assertIn("flstudio", summary)
        self.assertIn("PREFLIGHT", block)
        self.assertIn("flstudio", block)
        self.assertIn("unavailable actions", block)
        # block must be plain ASCII so a Windows console never mangles it.
        block.encode("ascii")


if __name__ == "__main__":
    unittest.main(verbosity=2)
