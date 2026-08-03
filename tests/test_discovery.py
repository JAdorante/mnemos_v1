"""Unit tests for desktop_agent.discovery — criteria-based app vetting.

The old model refused any app key not enumerated in the registry. The new model
lets launch_app resolve ANY installed app through OS registration channels and
judge it against criteria. These tests pin the criteria down: bare names only,
no shells/script hosts/interpreters, nothing inside the jail or scratch dirs,
locked (launch-only) capabilities, and a first-use human gate that no autonomy
level auto-approves — then a grant so later launches follow normal autonomy.

Run with either:
    python -m unittest discover -s tests        # zero dependencies
    pytest tests/                               # if pytest is installed
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Sandbox the default jail BEFORE importing config, so any code path that falls
# back to cfg.JAIL_ROOT points at a throwaway dir rather than the real ~/.
os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from desktop_agent import access  # noqa: E402
from desktop_agent import config as cfg  # noqa: E402
from desktop_agent import discovery  # noqa: E402
from desktop_agent import driver as drv  # noqa: E402


def _fake_exe(root: Path, name: str) -> Path:
    """Create a real file so vet_path's is_file() check passes."""
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"MZ")
    return p


class VetPathTests(unittest.TestCase):
    """vet_path: the pure criteria. Everything unclassifiable is refused."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="apps_")).resolve()
        self.jail = Path(tempfile.mkdtemp(prefix="jail_")).resolve()
        # The temp dir itself matches the untrusted markers on most machines
        # (…\AppData\Local\Temp). Neutralize them per-test so each criterion is
        # exercised in isolation; dedicated tests below re-enable them.
        self._markers = mock.patch.object(
            cfg, "DISCOVERY_UNTRUSTED_MARKERS", ())
        self._markers.start()
        self.addCleanup(self._markers.stop)

    def test_normal_installed_exe_allowed(self) -> None:
        exe = _fake_exe(self.root, "Spotify.exe")
        self.assertIsNone(discovery.vet_path(exe, self.jail))

    def test_missing_file_refused(self) -> None:
        reason = discovery.vet_path(self.root / "ghost.exe", self.jail)
        self.assertIn("does not exist", reason)

    @unittest.skipUnless(os.name == "nt", "extension rule is Windows-only")
    def test_non_exe_refused(self) -> None:
        for name in ("run.bat", "run.cmd", "run.ps1", "noext"):
            bad = _fake_exe(self.root, name)
            reason = discovery.vet_path(bad, self.jail)
            self.assertIsNotNone(reason, name)

    def test_shells_and_hosts_refused_by_basename(self) -> None:
        # Shells, script hosts, interpreters, admin tools: refused even when
        # they exist as real files in an ordinary location.
        for name in ("cmd.exe", "powershell.exe", "wscript.exe", "msiexec.exe",
                     "python.exe", "node.exe", "regedit.exe", "curl.exe"):
            bad = _fake_exe(self.root, name)
            reason = discovery.vet_path(bad, self.jail)
            self.assertIsNotNone(reason, name)
            self.assertIn("run_command", reason)

    def test_deny_basename_case_insensitive(self) -> None:
        bad = _fake_exe(self.root, "PowerShell.EXE")
        self.assertIsNotNone(discovery.vet_path(bad, self.jail))

    def test_exe_inside_jail_refused(self) -> None:
        # The agent can author files in the jail; it must never launch them.
        bad = _fake_exe(self.jail, "evil.exe")
        reason = discovery.vet_path(bad, self.jail)
        self.assertIn("jail", reason)

    def test_untrusted_locations_refused(self) -> None:
        self._markers.stop()  # restore the real markers for this test
        try:
            for sub in ("Temp", "Downloads"):
                bad = _fake_exe(self.root / sub, "dropper.exe")
                reason = discovery.vet_path(bad, self.jail)
                self.assertIsNotNone(reason, sub)
                self.assertIn("untrusted location", reason)
        finally:
            self._markers.start()


class DiscoverAppTests(unittest.TestCase):
    """discover_app: bare names only, channel probing, first vetted hit wins."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="apps_")).resolve()
        self.jail = Path(tempfile.mkdtemp(prefix="jail_")).resolve()
        self._markers = mock.patch.object(
            cfg, "DISCOVERY_UNTRUSTED_MARKERS", ())
        self._markers.start()
        self.addCleanup(self._markers.stop)

    def test_path_like_names_refused(self) -> None:
        for name in (r"C:\evil\app.exe", "..\\app", "a/b", "x:y", ""):
            info, why = discovery.discover_app(name, self.jail)
            self.assertIsNone(info, name)
            self.assertIsNotNone(why, name)

    def test_not_installed_returns_none_none(self) -> None:
        with mock.patch.object(discovery.shutil, "which", return_value=None), \
             mock.patch.object(discovery.app_registry, "resolve_from_app_paths",
                               return_value=None), \
             mock.patch.object(discovery.app_registry, "resolve_from_start_menu",
                               return_value=None):
            info, why = discovery.discover_app("nonexistent", self.jail)
        self.assertIsNone(info)
        self.assertIsNone(why)

    def test_vetted_path_hit_wins(self) -> None:
        exe = _fake_exe(self.root, "Spotify.exe")
        with mock.patch.object(discovery.shutil, "which",
                               return_value=str(exe)):
            info, why = discovery.discover_app("Spotify", self.jail)
        self.assertIsNone(why)
        self.assertEqual(info["key"], "spotify")
        self.assertEqual(info["source"], "PATH")
        self.assertEqual(Path(info["path"]), exe)

    def test_unvetted_hit_reports_reason_not_silence(self) -> None:
        bad = _fake_exe(self.root, "powershell.exe")
        with mock.patch.object(discovery.shutil, "which",
                               return_value=str(bad)), \
             mock.patch.object(discovery.app_registry, "resolve_from_app_paths",
                               return_value=None), \
             mock.patch.object(discovery.app_registry, "resolve_from_start_menu",
                               return_value=None):
            info, why = discovery.discover_app("powershell", self.jail)
        self.assertIsNone(info)
        self.assertIn("run_command", why)


class FirstUseGateTests(unittest.TestCase):
    """Driver integration: discovered apps gate as launch_unlisted_app on first
    use (never auto-approved), are granted after approval, and stay launch-only."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="apps_")).resolve()
        self.jail = Path(tempfile.mkdtemp(prefix="jail_")).resolve()
        sessions = Path(tempfile.mkdtemp(prefix="sess_")).resolve()
        self._patches = [
            mock.patch.object(cfg, "SESSIONS_ROOT", sessions),
            mock.patch.object(cfg, "APP_DISCOVERY", True),
            mock.patch.object(cfg, "DISCOVERY_UNTRUSTED_MARKERS", ()),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.exe = _fake_exe(self.root, "Spotify.exe")
        self.info = {"key": "spotify", "display_name": "spotify",
                     "path": str(self.exe), "source": "PATH"}

    def _driver(self, approve) -> drv.DesktopDriver:
        return drv.DesktopDriver(on_log=lambda s: None, on_approve=approve,
                                 jail_root=self.jail)

    def test_first_use_gates_as_unlisted_then_grants(self) -> None:
        seen: list[str | None] = []

        def approve(summary: str, details: str = "", action=None) -> bool:
            seen.append(action)
            return True

        d = self._driver(approve)
        with mock.patch.object(drv.subprocess, "Popen") as popen, \
             mock.patch("desktop_agent.discovery.discover_app",
                        return_value=(self.info, None)):
            res = d.launch_app("spotify")
            self.assertTrue(res["ok"], res)
            self.assertEqual(seen, ["launch_unlisted_app"])
            self.assertTrue(access.app_granted("spotify"))
            # Second launch: granted, so it gates as a normal launch_app.
            res2 = d.launch_app("spotify")
            self.assertTrue(res2["ok"], res2)
            self.assertEqual(seen[-1], "launch_app")
            self.assertEqual(popen.call_count, 2)

    def test_denied_first_use_grants_nothing(self) -> None:
        d = self._driver(lambda *a, **k: False)
        with mock.patch.object(drv.subprocess, "Popen") as popen, \
             mock.patch("desktop_agent.discovery.discover_app",
                        return_value=(self.info, None)):
            res = d.launch_app("spotify")
        self.assertFalse(res["ok"])
        self.assertFalse(access.app_granted("spotify"))
        popen.assert_not_called()

    def test_discovered_app_is_launch_only(self) -> None:
        # Locked caps: a discovered app may not be pointed at any target,
        # even one safely inside the jail.
        target = self.jail / "song.mp3"
        target.write_text("x")
        d = self._driver(lambda *a, **k: True)
        with mock.patch.object(drv.subprocess, "Popen") as popen, \
             mock.patch("desktop_agent.discovery.discover_app",
                        return_value=(self.info, None)):
            res = d.launch_app("spotify", [str(target)])
        self.assertFalse(res["ok"])
        popen.assert_not_called()

    def test_unvetted_app_refused_with_policy_reason(self) -> None:
        d = self._driver(lambda *a, **k: True)
        with mock.patch.object(drv.subprocess, "Popen") as popen, \
             mock.patch("desktop_agent.discovery.discover_app",
                        return_value=(None, "shells never launch")):
            res = d.launch_app("powershell")
        self.assertFalse(res["ok"])
        self.assertIn("discovery policy", res["detail"])
        popen.assert_not_called()

    def test_discovery_off_restores_closed_allowlist(self) -> None:
        d = self._driver(lambda *a, **k: True)
        with mock.patch.object(cfg, "APP_DISCOVERY", False), \
             mock.patch.object(drv.subprocess, "Popen") as popen:
            res = d.launch_app("spotify")
        self.assertFalse(res["ok"])
        self.assertIn("not on allowlist", res["detail"])
        popen.assert_not_called()

    def test_unlisted_verb_never_autoapproves(self) -> None:
        # The autonomy policy must defer first-use launches to a human at
        # EVERY level — that's what makes discovery safe to leave on.
        for level in cfg.DESKTOP_AUTONOMY_LEVELS:
            self.assertFalse(
                cfg.desktop_autoapprove("launch_unlisted_app", level), level)


if __name__ == "__main__":
    unittest.main()
