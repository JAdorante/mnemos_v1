"""Unit tests for desktop_agent.guards — the security-critical decision layer.

These pin down the guarantees the driver relies on: nothing escapes the jail,
only allowlisted commands run, unknown/blocked verbs are refused (default-deny),
and malformed input fails closed instead of crashing. Pure functions with no
side effects, so every attack vector can be exercised cheaply and in isolation.

Run with either:
    python -m unittest discover -s tests        # zero dependencies
    pytest tests/                               # if pytest is installed
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Sandbox the default jail BEFORE importing config, so any code path that falls
# back to cfg.JAIL_ROOT points at a throwaway dir rather than the real ~/.
os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from desktop_agent import config as cfg  # noqa: E402
from desktop_agent import guards  # noqa: E402
from desktop_agent.guards import Tier  # noqa: E402


class PathJailTests(unittest.TestCase):
    """within_jail / safe_child: the boundary the whole model rests on."""

    def setUp(self) -> None:
        self.jail = Path(tempfile.mkdtemp(prefix="jail_")).resolve()

    # --- safe_child: legitimate children resolve inside -------------------
    def test_simple_child_allowed(self) -> None:
        got = guards.safe_child(self.jail, "project")
        self.assertEqual(got, self.jail / "project")

    def test_nested_child_allowed(self) -> None:
        got = guards.safe_child(self.jail, "a/b/c.txt")
        self.assertIsNotNone(got)
        self.assertTrue(guards.within_jail(got, self.jail))

    def test_mixed_slashes_allowed(self) -> None:
        # forward and backslash separators both normalize to a jailed child.
        got = guards.safe_child(self.jail, r"sub\deep/file.txt")
        self.assertIsNotNone(got)
        self.assertTrue(guards.within_jail(got, self.jail))

    def test_env_var_literal_not_expanded(self) -> None:
        # Path does not expand %USERPROFILE% — it stays a literal folder name,
        # so it cannot be used to redirect outside the jail.
        got = guards.safe_child(self.jail, r"%USERPROFILE%\x")
        self.assertIsNotNone(got)
        self.assertTrue(guards.within_jail(got, self.jail))

    # --- safe_child: escapes are refused ---------------------------------
    def test_traversal_refused(self) -> None:
        for name in ("..", "../secret", "../../secret.txt", "a/../../../etc",
                     r"..\..\secret.txt"):
            with self.subTest(name=name):
                self.assertIsNone(guards.safe_child(self.jail, name))

    def test_absolute_path_refused(self) -> None:
        for name in (r"C:\Users\x\.ssh\id_rsa", r"C:\Windows\System32\x",
                     "/etc/passwd"):
            with self.subTest(name=name):
                self.assertIsNone(guards.safe_child(self.jail, name))

    def test_unc_path_refused(self) -> None:
        self.assertIsNone(guards.safe_child(self.jail, r"\\server\share\x"))

    def test_drive_relative_escape_refused(self) -> None:
        # "C:foo" is drive-relative; it must not resolve to another drive root.
        got = guards.safe_child(self.jail, "C:foo")
        self.assertTrue(got is None or guards.within_jail(got, self.jail))

    def test_empty_name_refused(self) -> None:
        self.assertIsNone(guards.safe_child(self.jail, ""))

    # --- safe_child: Windows reserved device names -----------------------
    def test_reserved_device_names_refused(self) -> None:
        # "jail/NUL" is really the NUL device on Windows, extension or not.
        for name in ("NUL", "CON", "AUX", "PRN", "COM1", "LPT1",
                     "nul.txt", "con.log", "NUL ", "nul."):
            with self.subTest(name=name):
                self.assertIsNone(guards.safe_child(self.jail, name),
                                  f"reserved device name not refused: {name!r}")

    def test_reserved_substring_still_allowed(self) -> None:
        # a name that merely CONTAINS a device token is a normal file.
        for name in ("console.txt", "communicate.md", "nullable.py"):
            with self.subTest(name=name):
                self.assertIsNotNone(guards.safe_child(self.jail, name))

    # --- safe_child: fail closed, never raise ----------------------------
    def test_malformed_name_fails_closed(self) -> None:
        # The guarantee is: never raise. A malformed name must yield a clean
        # decision (None or a jailed path), so the driver refuses instead of
        # crashing mid-action. Names that can't resolve at all return None.
        for name in ("a\x00b", "x" * 6000, "a\x00/../b"):
            with self.subTest(name=repr(name)[:24]):
                try:
                    got = guards.safe_child(self.jail, name)
                except Exception as exc:  # noqa: BLE001 - the whole point
                    self.fail(f"safe_child raised on {name!r}: {exc!r}")
                if got is not None:
                    # whatever survives must still be inside the jail.
                    self.assertTrue(guards.within_jail(got, self.jail))

    def test_null_byte_name_refused(self) -> None:
        # an embedded null can never be a real path -> refuse outright.
        self.assertIsNone(guards.safe_child(self.jail, "a\x00b"))

    # --- within_jail ------------------------------------------------------
    def test_within_jail_true_for_jailed_paths(self) -> None:
        self.assertTrue(guards.within_jail(self.jail, self.jail))
        self.assertTrue(guards.within_jail(self.jail / "a" / "b", self.jail))

    def test_within_jail_false_for_outside(self) -> None:
        self.assertFalse(guards.within_jail(self.jail.parent, self.jail))
        self.assertFalse(guards.within_jail(r"C:\Windows", self.jail))

    def test_within_jail_never_raises(self) -> None:
        # bad input is a boolean False, not an exception.
        self.assertFalse(guards.within_jail("a\x00b", self.jail))

    @unittest.skipUnless(os.name == "nt" or hasattr(os, "symlink"),
                         "symlink support required")
    def test_symlink_escape_refused(self) -> None:
        # a symlink inside the jail pointing outside must not grant access:
        # resolve() follows the link, landing outside -> refused.
        outside = Path(tempfile.mkdtemp(prefix="outside_")).resolve()
        link = self.jail / "escape"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot create symlink here: {exc}")
        self.assertFalse(guards.within_jail(link / "x", self.jail))
        self.assertIsNone(guards.safe_child(self.jail, "escape/x"))


class LaunchArgTests(unittest.TestCase):
    """check_launch_args: an app may only be pointed at jailed paths."""

    def setUp(self) -> None:
        self.jail = Path(tempfile.mkdtemp(prefix="jail_")).resolve()

    def test_flags_pass(self) -> None:
        self.assertIsNone(guards.check_launch_args(["-n", "--flag"], self.jail))

    def test_jailed_absolute_path_passes(self) -> None:
        p = str(self.jail / "song.flp")
        self.assertIsNone(guards.check_launch_args([p], self.jail))

    def test_traversal_arg_refused(self) -> None:
        self.assertIsNotNone(
            guards.check_launch_args([r"..\..\secret.flp"], self.jail))

    def test_absolute_outside_arg_refused(self) -> None:
        self.assertIsNotNone(
            guards.check_launch_args([r"C:\Windows\x.flp"], self.jail))

    def test_empty_args_ok(self) -> None:
        self.assertIsNone(guards.check_launch_args([], self.jail))
        self.assertIsNone(guards.check_launch_args(None, self.jail))


class CommandClassificationTests(unittest.TestCase):
    """scan_danger / classify_command: default-deny with a read/mutate split."""

    def assertTier(self, argv, tier: Tier) -> None:
        got, reason = guards.classify_command(argv)
        self.assertEqual(got, tier, f"{argv} -> {got} ({reason}), want {tier}")

    # --- read-only verbs auto-pass ---------------------------------------
    def test_read_verbs(self) -> None:
        for argv in (["dir"], ["ls", "-la"], ["whoami"], ["echo", "hi"],
                     ["where", "python"]):
            with self.subTest(argv=argv):
                self.assertTier(argv, Tier.READ_ONLY)

    # --- mutating verbs gated --------------------------------------------
    def test_mutate_verbs(self) -> None:
        for argv in (["npm", "install"], ["pip", "install", "flask"],
                     ["python", "app.py"], ["node", "index.js"]):
            with self.subTest(argv=argv):
                self.assertTier(argv, Tier.MUTATING)

    # --- git split by subcommand -----------------------------------------
    def test_git_read_subcommands(self) -> None:
        for sub in ("status", "log", "diff", "branch"):
            with self.subTest(sub=sub):
                self.assertTier(["git", sub], Tier.READ_ONLY)

    def test_git_mutate_subcommands(self) -> None:
        for sub in ("commit", "add", "checkout", "clone"):
            with self.subTest(sub=sub):
                self.assertTier(["git", sub], Tier.MUTATING)

    def test_git_unknown_subcommand_gated_not_blocked(self) -> None:
        # unknown git subcommand is gated (mutating), not silently run.
        self.assertTier(["git", "frobnicate"], Tier.MUTATING)

    # --- hard blocks ------------------------------------------------------
    def test_blocked_verbs(self) -> None:
        for argv in (["rm", "-rf", "x"], ["del", "x"], ["format", "c:"],
                     ["reg", "add"], ["shutdown"], ["runas"], ["curl", "x"],
                     ["powershell", "-c", "x"], ["cmd", "/c", "x"],
                     ["bash", "-c", "x"]):
            with self.subTest(argv=argv):
                self.assertTier(argv, Tier.BLOCKED)

    def test_exe_suffix_stripped_before_block(self) -> None:
        # "powershell.exe" must block just like "powershell".
        for verb in ("powershell.exe", "CMD.EXE", "rm.exe"):
            with self.subTest(verb=verb):
                self.assertTier([verb, "x"], Tier.BLOCKED)

    def test_unknown_verb_blocked_default_deny(self) -> None:
        for argv in (["telnet", "host"], ["ssh", "host"], ["madeupbinary"]):
            with self.subTest(argv=argv):
                self.assertTier(argv, Tier.BLOCKED)

    # --- danger screen runs before classification ------------------------
    def test_shell_metacharacters_blocked(self) -> None:
        for argv in (["echo", "a && rm b"], ["dir", "|", "more"],
                     ["echo", "$(whoami)"], ["ls", "a;b"], ["echo", "a`b`"]):
            with self.subTest(argv=argv):
                self.assertTier(argv, Tier.BLOCKED)

    def test_secret_markers_blocked(self) -> None:
        for argv in (["cat", r"C:\Users\me\.ssh\id_rsa"],
                     ["type", ".env"], ["dir", r"C:\Windows\System32"],
                     ["echo", "my-secrets-file"]):
            with self.subTest(argv=argv):
                self.assertTier(argv, Tier.BLOCKED)

    def test_traversal_in_argument_blocked(self) -> None:
        self.assertTier(["cat", "../../etc/passwd"], Tier.BLOCKED)

    def test_full_path_shell_blocked(self) -> None:
        # a shell invoked by absolute path is caught by the secret/system32
        # marker and/or default-deny, never run.
        self.assertTier(
            [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "x"],
            Tier.BLOCKED)

    def test_empty_command_blocked(self) -> None:
        self.assertTier([], Tier.BLOCKED)
        self.assertIsNotNone(guards.scan_danger([]))


class OpenTargetPolicyTests(unittest.TestCase):
    """open_target_allowed: an app is only opened on the targets it declares."""

    def assertAllowed(self, key: str, suffix: str, is_dir: bool) -> None:
        r = guards.open_target_allowed(key, suffix, is_dir)
        self.assertIsNone(r, f"{key} {suffix or 'dir'} unexpectedly refused: {r}")

    def assertRefused(self, key: str, suffix: str, is_dir: bool) -> None:
        self.assertIsNotNone(guards.open_target_allowed(key, suffix, is_dir))

    def test_editor_opens_folder_and_source(self) -> None:
        for key in ("cursor", "code"):
            self.assertAllowed(key, "", True)          # a project folder
            self.assertAllowed(key, ".py", False)
            self.assertAllowed(key, ".md", False)

    def test_extension_match_is_case_insensitive(self) -> None:
        self.assertAllowed("cursor", ".PY", False)
        self.assertAllowed("flstudio", ".FLP", False)

    def test_editor_refuses_unknown_extension(self) -> None:
        self.assertRefused("cursor", ".flp", False)

    @unittest.skipUnless(sys.platform == "win32",
                         "notepad is a win32-only registry entry: build_registry "
                         "drops off-platform apps, so off Windows it resolves to "
                         "LOCKED_CAPS and opens nothing")
    def test_notepad_refuses_folder_allows_text(self) -> None:
        self.assertRefused("notepad", "", True)
        self.assertAllowed("notepad", ".txt", False)

    def test_an_unregistered_app_opens_nothing(self) -> None:
        """The fail-closed half of the same contract, on every platform: an app
        with no capability entry — including a win32 app off Windows — is
        launch-only, never pointed at a target."""
        self.assertRefused("no-such-app", ".txt", False)
        self.assertRefused("no-such-app", "", True)

    def test_flstudio_opens_audio_and_project(self) -> None:
        for ext in (".flp", ".wav", ".mp3", ".mid", ".midi"):
            self.assertAllowed("flstudio", ext, False)
        self.assertRefused("flstudio", ".txt", False)
        self.assertRefused("flstudio", "", True)  # DAW opens files, not folders

    def test_chrome_opens_local_file_not_folder(self) -> None:
        self.assertAllowed("chrome", ".html", False)
        self.assertRefused("chrome", "", True)

    def test_launch_only_apps_open_nothing(self) -> None:
        # terminal / phonelink take no target at all.
        for key in ("terminal", "phonelink"):
            self.assertRefused(key, "", True)
            self.assertRefused(key, ".txt", False)

    def test_unknown_app_is_locked_down(self) -> None:
        # a key with no capability entry opens nothing (fail closed).
        self.assertRefused("mysterious_app", ".py", False)
        self.assertRefused("mysterious_app", "", True)


class CapabilityRegistryTests(unittest.TestCase):
    """The registry is the single source of truth — and can't silently drift."""

    def test_candidates_and_capabilities_keys_match(self) -> None:
        self.assertEqual(set(cfg.APP_CANDIDATES), set(cfg.APP_CAPABILITIES),
                         "APP_CANDIDATES and APP_CAPABILITIES drifted apart")

    def test_every_entry_has_wellformed_fields(self) -> None:
        for key, c in cfg.APP_CAPABILITIES.items():
            with self.subTest(app=key):
                self.assertIsInstance(c.get("open_jailed_files"), list)
                self.assertIsInstance(c.get("opens_dirs"), bool)
                self.assertTrue(c.get("display_name"))
                for ext in c["open_jailed_files"]:
                    self.assertTrue(ext.startswith("."), f"bad ext {ext!r}")

    def test_describe_apps_lists_every_app(self) -> None:
        text = cfg.describe_apps()
        for key in cfg.APP_CANDIDATES:
            self.assertIn(key, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
