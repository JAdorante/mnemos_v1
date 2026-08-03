"""Tests for the desktop agent's security boundary (desktop_agent/guards.py).

This allowlist IS the sandbox — there is no OS container behind it — so the
decisions are tested exhaustively: the path jail (traversal, absolute paths,
Windows reserved device names), the hard-block danger scan (destructive verbs,
elevation, nested shells, shell metacharacters, secret paths), default-deny
tier classification, launch-arg jailing, per-app open-target capability, and
the autonomy auto-approve ladder. All pure logic — no filesystem mutations, no
processes, no prompts.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop_agent import config as cfg
from desktop_agent.guards import (
    Tier,
    check_launch_args,
    classify_command,
    open_target_allowed,
    safe_child,
    scan_danger,
    within_jail,
)


class JailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="quill_jail_")).resolve()

    def test_inside_and_root_itself_are_jailed(self) -> None:
        self.assertTrue(within_jail(self.root, self.root))
        self.assertTrue(within_jail(self.root / "notes" / "a.txt", self.root))

    def test_outside_is_not_jailed(self) -> None:
        self.assertFalse(within_jail(self.root.parent, self.root))
        self.assertFalse(within_jail(Path.home(), self.root))

    def test_safe_child_resolves_nested_names(self) -> None:
        got = safe_child(self.root, "projects/demo/readme.md")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertTrue(within_jail(got, self.root))

    def test_safe_child_refuses_traversal(self) -> None:
        self.assertIsNone(safe_child(self.root, "../escape.txt"))
        self.assertIsNone(safe_child(self.root, "a/../../escape.txt"))

    def test_safe_child_refuses_absolute_paths(self) -> None:
        outside = str(self.root.parent / "outside.txt")
        self.assertIsNone(safe_child(self.root, outside))

    def test_safe_child_refuses_empty_and_malformed(self) -> None:
        self.assertIsNone(safe_child(self.root, ""))
        self.assertIsNone(safe_child(self.root, "bad\x00name"))

    def test_safe_child_refuses_windows_device_names(self) -> None:
        # "jail/NUL" IS the NUL device on Windows, regardless of extension,
        # trailing dot, or case — never a file inside the jail.
        for name in ("NUL", "nul.txt", "CON", "con.", "com1.log", "lpt9",
                     "logs/AUX/x.txt"):
            self.assertIsNone(safe_child(self.root, name), name)

    def test_safe_child_allows_lookalike_names(self) -> None:
        # "nullable.txt" / "console.md" merely START with a device name.
        for name in ("nullable.txt", "console.md", "com10.txt", "auxiliary"):
            self.assertIsNotNone(safe_child(self.root, name), name)


class DangerScanTests(unittest.TestCase):
    def test_empty_command_blocked(self) -> None:
        self.assertIsNotNone(scan_danger([]))

    def test_destructive_and_elevation_verbs_blocked(self) -> None:
        for verb in ("rm", "del", "format", "diskpart", "reg", "sudo",
                     "runas", "shutdown", "taskkill"):
            self.assertIsNotNone(scan_danger([verb, "x"]), verb)

    def test_nested_shells_and_downloaders_blocked(self) -> None:
        # A nested shell would escape the argv-list protection; downloaders
        # reach the network.
        for verb in ("powershell", "pwsh", "cmd", "bash", "sh", "curl",
                     "wget", "certutil", "mshta", "rundll32"):
            self.assertIsNotNone(scan_danger([verb]), verb)

    def test_verb_matching_ignores_case_and_exe_suffix(self) -> None:
        self.assertIsNotNone(scan_danger(["RM", "-rf", "x"]))
        self.assertIsNotNone(scan_danger(["PowerShell.exe", "-c", "hi"]))
        self.assertIsNotNone(scan_danger(["cmd.BAT"]))

    def test_shell_metacharacters_in_any_arg_blocked(self) -> None:
        for bad in ("a;b", "a|b", "a&b", "`whoami`", "$(x)", "a>b", "a\nb"):
            self.assertIsNotNone(scan_danger(["echo", bad]), bad)

    def test_secret_paths_blocked(self) -> None:
        for arg in (r"C:\Users\u\.ssh\id_rsa", ".env", r"C:\Windows\System32\x",
                    "my-credentials.json", ".aws/config"):
            self.assertIsNotNone(scan_danger(["type", arg]), arg)

    def test_traversal_in_args_blocked(self) -> None:
        self.assertIsNotNone(scan_danger(["type", "../outside.txt"]))

    def test_clean_command_passes(self) -> None:
        self.assertIsNone(scan_danger(["echo", "hello world"]))
        self.assertIsNone(scan_danger(["python", "script.py", "--flag"]))


class ClassifyTests(unittest.TestCase):
    def test_read_verbs_run_free(self) -> None:
        for verb in ("ls", "dir", "type", "whoami"):
            tier, _ = classify_command([verb])
            self.assertEqual(tier, Tier.READ_ONLY, verb)

    def test_mutating_verbs_are_gated(self) -> None:
        for argv in (["python", "x.py"], ["npm", "install"], ["node", "a.js"]):
            tier, _ = classify_command(argv)
            self.assertEqual(tier, Tier.MUTATING, argv)

    def test_git_split_by_subcommand(self) -> None:
        self.assertEqual(classify_command(["git", "status"])[0], Tier.READ_ONLY)
        self.assertEqual(classify_command(["git", "commit", "-m", "x"])[0],
                         Tier.MUTATING)
        # Unknown git subcommand and bare `git` are gated, not free.
        self.assertEqual(classify_command(["git", "frobnicate"])[0],
                         Tier.MUTATING)
        self.assertEqual(classify_command(["git"])[0], Tier.MUTATING)

    def test_unknown_verbs_default_deny(self) -> None:
        for verb in ("notepad", "xcopy", "robocopy", "msiexec"):
            tier, reason = classify_command([verb, "x"])
            self.assertEqual(tier, Tier.BLOCKED, verb)
            self.assertIn("allowlist", reason)

    def test_danger_screens_before_allowlist(self) -> None:
        # Even an allowlisted verb is BLOCKED when an arg is dangerous —
        # no approval prompt may ever see these.
        tier, _ = classify_command(["python", "../outside/evil.py"])
        self.assertEqual(tier, Tier.BLOCKED)
        tier, _ = classify_command(["type", r"C:\Users\u\.ssh\id_rsa"])
        self.assertEqual(tier, Tier.BLOCKED)
        tier, _ = classify_command(["rm", "-rf", "anything"])
        self.assertEqual(tier, Tier.BLOCKED)


class LaunchArgTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="quill_jail_")).resolve()

    def test_flags_and_plain_words_pass(self) -> None:
        self.assertIsNone(check_launch_args(["-n", "--new-window"], self.root))
        self.assertIsNone(check_launch_args(["projectname"], self.root))
        self.assertIsNone(check_launch_args([], self.root))

    def test_jailed_path_passes_outside_path_refused(self) -> None:
        inside = str(self.root / "song.flp")
        self.assertIsNone(check_launch_args([inside], self.root))
        outside = str(self.root.parent / "escape.txt")
        self.assertIsNotNone(check_launch_args([outside], self.root))

    def test_drive_colon_counts_as_pathlike(self) -> None:
        self.assertIsNotNone(check_launch_args([r"C:\Windows\evil"], self.root))


class OpenTargetTests(unittest.TestCase):
    """Capability policy is data-driven; fake the config lookups so the tests
    pin the DECISION logic, not this machine's app registry."""

    def _caps(self, exts=(".txt",), dirs=False):
        return (mock.patch.object(cfg, "openable_extensions",
                                  return_value=set(exts)),
                mock.patch.object(cfg, "app_opens_dirs", return_value=dirs),
                mock.patch.object(cfg, "app_display_name",
                                  return_value="TestApp"))

    def test_declared_extension_allowed(self) -> None:
        p1, p2, p3 = self._caps(exts=(".flp", ".wav"))
        with p1, p2, p3:
            self.assertIsNone(open_target_allowed("fl", ".FLP".lower(), False))
            self.assertIsNotNone(open_target_allowed("fl", ".exe", False))
            self.assertIsNotNone(open_target_allowed("fl", "", False))

    def test_folder_only_when_declared(self) -> None:
        p1, p2, p3 = self._caps(dirs=True)
        with p1, p2, p3:
            self.assertIsNone(open_target_allowed("editor", "", True))
        p1, p2, p3 = self._caps(dirs=False)
        with p1, p2, p3:
            self.assertIsNotNone(open_target_allowed("player", "", True))


class AutonomyLadderTests(unittest.TestCase):
    """desktop_autoapprove: which verbs may self-approve in an autonomous run.
    It only answers auto-vs-ask; it can never widen what the driver allows."""

    def test_read_only_verbs_always_auto(self) -> None:
        for verb in ("list_dir", "screenshot"):
            self.assertTrue(cfg.desktop_autoapprove(verb, "off"))

    def test_ladder_orders_the_verbs(self) -> None:
        self.assertTrue(cfg.desktop_autoapprove("launch_app", "launch_only"))
        self.assertFalse(cfg.desktop_autoapprove("write_file", "launch_only"))
        self.assertTrue(cfg.desktop_autoapprove("write_file", "jailed_files"))
        # Pixel UI can act OUTSIDE the jail — gated until ui_control.
        self.assertFalse(cfg.desktop_autoapprove("click_at", "jailed_files"))
        self.assertTrue(cfg.desktop_autoapprove("click_at", "ui_control"))

    def test_off_and_invalid_levels_never_auto(self) -> None:
        self.assertFalse(cfg.desktop_autoapprove("launch_app", "off"))
        self.assertFalse(cfg.desktop_autoapprove("launch_app", "bogus"))

    def test_shell_is_its_own_axis(self) -> None:
        # run_command never rides the desktop ladder — even `full` doesn't
        # unlock it; only the explicit shell opt-in does.
        self.assertFalse(cfg.desktop_autoapprove("run_command", "full",
                                                 shell=False))
        self.assertTrue(cfg.desktop_autoapprove("run_command", "off",
                                                shell=True))

    def test_unknown_verbs_defer_to_the_human(self) -> None:
        self.assertFalse(cfg.desktop_autoapprove("new_scary_verb", "full"))
        self.assertFalse(cfg.desktop_autoapprove("", "full"))


if __name__ == "__main__":
    unittest.main()
