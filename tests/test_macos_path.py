"""WS-F — the macOS tester path.

Two things are being protected. First, that a Mac tester has an install path at
all: `install.bat`/`start.bat` are Windows-only, so without the `.command`
equivalents the Mac cohort literally cannot run the product. Second, that the
capture degradation is *honest* — the Console must say what this OS cannot do
before a tester clicks it, rather than handing them a 503 and no explanation.

The clean-Mac dry run (Gatekeeper, TCC prompts, BlackHole) cannot be automated
from here; it stays a manual gate. Everything checkable without a Mac is here.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.command"
START = REPO / "start.command"


class LauncherPresenceTests(unittest.TestCase):
    def test_both_launchers_exist_and_are_executable(self) -> None:
        for path in (INSTALL, START):
            self.assertTrue(path.is_file(), f"{path.name} missing")
            self.assertTrue(os.access(path, os.X_OK),
                            f"{path.name} is not executable — Finder will open "
                            "it in a text editor instead of running it")

    def test_they_are_bash_and_parse(self) -> None:
        """A syntax error here strands the entire cohort on install."""
        bash = shutil.which("bash")
        if not bash:                                   # pragma: no cover
            self.skipTest("bash not available")
        for path in (INSTALL, START):
            self.assertTrue(path.read_text().startswith("#!/usr/bin/env bash"))
            r = subprocess.run([bash, "-n", str(path)], capture_output=True)
            self.assertEqual(r.returncode, 0,
                             f"{path.name}: {r.stderr.decode()}")

    def test_line_endings_are_unix(self) -> None:
        """A CRLF shell script fails on macOS with a baffling '\\r' error."""
        for path in (INSTALL, START):
            self.assertNotIn(b"\r\n", path.read_bytes(), path.name)


class InstallerParityTests(unittest.TestCase):
    """The macOS installer must cover the same ground as install.ps1."""

    def setUp(self) -> None:
        self.sh = INSTALL.read_text(encoding="utf-8")
        self.ps1 = (REPO / "scripts" / "install.ps1").read_text(encoding="utf-8")

    def test_it_does_the_same_steps(self) -> None:
        for needle in ("venv", "requirements.txt", "playwright install chromium",
                       "download_models.py", ".env.example"):
            self.assertIn(needle, self.sh, f"macOS installer skips {needle}")

    def test_both_key_paths_are_offered(self) -> None:
        """Invite code first, BYO key intact — same as Windows (WS-D)."""
        self.assertIn("redeem_and_save", self.sh)
        self.assertIn("QUILL_INVITE_URL", self.sh)
        self.assertIn("ANTHROPIC_API_KEY", self.sh)
        # And the invite branch is conditional, exactly as on Windows.
        self.assertIn('if [ -n "$INVITE_URL" ]', self.sh)
        self.assertIn('if [ "$INVITED" = "0" ]', self.sh)

    def test_it_installs_the_microphone_backend(self) -> None:
        """Mic capture IS the macOS build; a missing PortAudio is fatal to it."""
        self.assertIn("portaudio", self.sh.lower())
        self.assertIn("import sounddevice", self.sh)

    def test_ollama_is_optional_here_not_silently_installed(self) -> None:
        """10 GB of local models is the tester's decision on their own laptop."""
        self.assertIn("optional", self.sh.lower())
        self.assertNotIn("brew install ollama", self.sh)

    def test_sed_calls_use_the_bsd_form(self) -> None:
        """GNU `sed -i` without a suffix silently fails on macOS."""
        for match in re.finditer(r"sed -i(.{0,4})", self.sh):
            self.assertTrue(match.group(1).startswith(" ''"),
                            f"sed -i needs a '' backup suffix on macOS: "
                            f"{match.group(0)!r}")

    def test_start_script_refuses_without_an_install(self) -> None:
        sh = START.read_text(encoding="utf-8")
        self.assertIn(".venv/bin/python", sh)
        self.assertIn("run install.command first", sh)

    def test_start_script_clears_the_gatekeeper_quarantine(self) -> None:
        """Otherwise every tester files the same 'Mnemos is damaged' report."""
        self.assertIn("com.apple.quarantine", START.read_text(encoding="utf-8"))


class CaptureSupportTests(unittest.TestCase):
    """The honest-degradation contract, per OS."""

    def test_windows_can_do_everything(self) -> None:
        from app.services import capture_support as cs
        st = cs.status("win32")
        self.assertEqual(st["os"], "windows")
        self.assertEqual(st["unsupported"], [])
        self.assertEqual(st["needs_setup"], [])
        self.assertEqual(st["note"], "")

    def test_macos_marks_desktop_capture_unsupported_with_a_reason(self) -> None:
        from app.services import capture_support as cs
        st = cs.status("darwin")
        self.assertEqual(st["os"], "macos")
        self.assertEqual(st["unsupported"], ["clicks", "screen"])
        for key in ("screen", "clicks"):
            src = st["sources"][key]
            self.assertFalse(src["available"])
            # A reason a non-engineer can act on, not a stack trace.
            self.assertIn("Windows/Linux-only", src["reason"])
        self.assertIn("meetings, not your screen", st["note"].lower())

    def test_macos_keeps_the_meeting_path_available(self) -> None:
        """The whole point of the Mac build must not be marked unsupported."""
        from app.services import capture_support as cs
        srcs = cs.support("darwin")
        for key in ("mic", "webcam", "save_audio"):
            self.assertTrue(srcs[key]["available"], key)
            self.assertEqual(srcs[key]["reason"], "")

    def test_macos_system_audio_is_offered_with_its_requirement(self) -> None:
        from app.services import capture_support as cs
        src = cs.support("darwin")["system_audio"]
        self.assertEqual(src["state"], cs.SETUP)
        self.assertTrue(src["available"])      # offered, not blocked
        self.assertIn("BlackHole", src["reason"])
        self.assertIn("mic still records", src["reason"])

    def test_linux_offers_desktop_capture_with_x11_note(self) -> None:
        from app.services import capture_support as cs
        st = cs.status("linux")
        self.assertEqual(st["unsupported"], [])
        self.assertEqual(st["needs_setup"], ["system_audio"])
        for key in ("screen", "clicks"):
            src = st["sources"][key]
            self.assertTrue(src["available"], key)
            self.assertEqual(src["state"], cs.AVAILABLE)
            self.assertIn("X11", src["reason"])
        self.assertIn("monitor", cs.support("linux")["system_audio"]["reason"])
        self.assertIn("X11", st["note"])


    def test_every_privacy_sheet_source_has_an_entry(self) -> None:
        """A source the UI shows but the map omits would render un-annotated."""
        from app.services import capture_support as cs
        ui = (REPO / "app" / "api" / "mnemos_ui.py").read_text(encoding="utf-8")
        block = ui[ui.index("_SOURCES: ["):ui.index("async status()")]
        shown = set(re.findall(r"key:'([a-z_]+)'", block))
        self.assertTrue(shown)
        for plat in ("win32", "darwin", "linux"):
            self.assertEqual(shown - set(cs.support(plat)), set(), plat)

    def test_is_available_defaults_open_for_unknown_sources(self) -> None:
        from app.services import capture_support as cs
        self.assertTrue(cs.is_available("something_new", "darwin"))


class CaptureStatusRouteTests(unittest.TestCase):
    def _status(self, platform: str) -> dict:
        from fastapi.testclient import TestClient
        from app.main import app
        with patch("app.services.capture_support.sys") as sysmod:
            sysmod.platform = platform
            return TestClient(app).get("/capture/status").json()

    def test_status_tells_the_console_what_this_os_can_do(self) -> None:
        body = self._status("darwin")
        self.assertIn("support", body)
        self.assertEqual(body["support"]["os"], "macos")
        self.assertEqual(body["support"]["unsupported"], ["clicks", "screen"])
        # The pre-existing payload is untouched.
        for key in ("consent", "running", "save_audio", "meeting_mode"):
            self.assertIn(key, body)

    def test_windows_status_is_unchanged_in_substance(self) -> None:
        body = self._status("win32")
        self.assertEqual(body["support"]["unsupported"], [])

    def test_the_privacy_sheet_disables_what_it_cannot_run(self) -> None:
        ui = (REPO / "app" / "api" / "mnemos_ui.py").read_text(encoding="utf-8")
        self.assertIn("box.disabled = blocked", ui)
        self.assertIn("cap.available === false", ui)
        # The reason is shown, not just the disabled state.
        self.assertIn("note.textContent = cap.reason", ui)

    def test_the_503_backstop_is_still_there(self) -> None:
        """The UI change is an explanation, not a replacement for the guard."""
        routes = (REPO / "app" / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn("status_code=503", routes)


class MacDocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = (REPO / "TESTER_SETUP-macos.md").read_text(encoding="utf-8")

    def test_it_covers_gatekeeper_concretely(self) -> None:
        """"Right-click → Open" is the single most common macOS support call."""
        low = self.doc.lower()
        self.assertIn("gatekeeper", low)
        self.assertIn("right-click", low)
        self.assertIn("open anyway", low)
        self.assertIn("xattr -dr com.apple.quarantine", self.doc)

    def test_it_is_honest_about_the_capture_degradation(self) -> None:
        for needle in ("Screen capture", "Windows-only", "BlackHole",
                       "Microphone"):
            self.assertIn(needle, self.doc)
        # It must state plainly that meetings still work without BlackHole.
        self.assertIn("This is optional.", self.doc)

    def test_it_points_at_the_real_scripts_and_paths(self) -> None:
        for needle in ("install.command", "start.command",
                       ".venv/bin/python scripts/restore_backup.py"):
            self.assertIn(needle, self.doc)

    def test_it_does_not_promise_windows_only_features(self) -> None:
        for feature in ("Screen capture", "Mouse-click capture",
                        "Desktop agent", "Phone notification mirror"):
            row = [ln for ln in self.doc.splitlines() if ln.startswith(f"| {feature}")]
            self.assertTrue(row, f"{feature} not listed in the capability table")
            self.assertIn("❌", row[0], f"{feature} is not marked unavailable")


if __name__ == "__main__":
    unittest.main()
