"""The packaged desktop build — frozen paths and the packaging contract.

None of this can be caught by running from a checkout, which is why the
packaged build shipped for months with a spec that excluded torch (no VAD, so
no memory at all) and a first-run page that re-executed the app instead of
downloading models. The frozen branches are asserted here by faking
``sys.frozen``; the spec and installer script are asserted by reading them.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import desktop_app
from app import runtime

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "packaging" / "mnemos.spec"
ISS = REPO / "packaging" / "mnemos.iss"


class RuntimeTests(unittest.TestCase):
    def test_a_checkout_is_not_frozen_and_keeps_writing_local_data(self) -> None:
        self.assertFalse(runtime.is_frozen())
        self.assertEqual(runtime.default_data_dir(), Path("data"))
        self.assertEqual(runtime.bundle_root(), REPO)

    def test_apply_env_defaults_is_a_no_op_in_a_checkout(self) -> None:
        """Developers, tests and the scripted install all rely on ./data."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUILL_DATA_DIR", None)
            self.assertEqual(runtime.apply_env_defaults(), {})
            self.assertIsNone(os.environ.get("QUILL_DATA_DIR"))

    def test_frozen_relocates_data_and_credentials(self) -> None:
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="quill_rt_"))
        with patch.object(runtime, "is_frozen", lambda: True), \
                patch.dict(os.environ, {"QUILL_USER_DATA_ROOT": str(tmp)},
                           clear=False):
            os.environ.pop("QUILL_DATA_DIR", None)
            os.environ.pop("QUILL_CREDENTIALS_FILE", None)
            applied = runtime.apply_env_defaults()
        self.assertEqual(applied["QUILL_DATA_DIR"], str(tmp / "data"))
        self.assertEqual(applied["QUILL_CREDENTIALS_FILE"],
                         str(tmp / ".credentials.env"))
        self.assertTrue((tmp / "data").is_dir())
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_an_explicit_data_dir_always_wins(self) -> None:
        with patch.object(runtime, "is_frozen", lambda: True), \
                patch.dict(os.environ, {"QUILL_DATA_DIR": "/somewhere/else"},
                           clear=False):
            applied = runtime.apply_env_defaults()
        self.assertNotIn("QUILL_DATA_DIR", applied)

    @unittest.skipUnless(sys.platform == "win32", "windows path rule")
    def test_windows_uses_local_not_roaming(self) -> None:
        self.assertIn("Local", str(runtime.user_data_root()))


class FrozenWriteTargetTests(unittest.TestCase):
    """Nothing the tester creates may land inside the bundle."""

    def test_the_deletion_receipt_leaves_the_bundle(self) -> None:
        import tempfile
        from app.services import wipe
        tmp = Path(tempfile.mkdtemp(prefix="quill_rcpt_"))
        with patch.object(runtime, "is_frozen", lambda: True), \
                patch.dict(os.environ, {"QUILL_USER_DATA_ROOT": str(tmp)},
                           clear=False):
            self.assertEqual(wipe._receipt_dir(), tmp)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_install_root_follows_the_bundle(self) -> None:
        from app.services import wipe
        with patch.object(runtime, "bundle_root", lambda: Path("/opt/Mnemos")):
            self.assertEqual(wipe.install_root(), Path("/opt/Mnemos"))


class SpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SPEC.read_text()

    def test_torch_is_bundled_not_excluded(self) -> None:
        """silero_vad imports torch even in ONNX mode; excluding it = no VAD."""
        self.assertNotIn('"torch"', self.text.split("excludes=")[1].split("]")[0])
        self.assertIn('"torch"', self.text)

    def test_a_cuda_wheel_is_refused_rather_than_shipped(self) -> None:
        """2-3 GB of GPU runtime a tester downloads and never uses.

        And it cannot be stripped afterwards: dropping the CUDA binaries builds
        cleanly, then fails at import on libtorch_cuda.so.
        """
        self.assertIn("_check_torch_build()", self.text)
        self.assertIn("QUILL_ALLOW_CUDA_BUILD", self.text)
        self.assertIn("download.pytorch.org/whl/cpu", self.text)
        # The guard has to run before the bundle is assembled, not after.
        self.assertLess(self.text.index("_check_torch_build()\n\n\na = Analysis"),
                        self.text.index("pyz = PYZ"))

    def test_no_binary_surgery_on_the_cuda_payload(self) -> None:
        """A previous attempt filtered a.binaries and produced a broken app."""
        self.assertNotIn("a.binaries = [", self.text)

    def test_no_console_window(self) -> None:
        self.assertIn("console=False", self.text)
        self.assertNotIn("console=True", self.text)

    def test_the_entry_point_is_the_desktop_shell(self) -> None:
        self.assertIn('"desktop_app.py"', self.text)

    def test_it_ships_an_icon(self) -> None:
        self.assertIn("mnemos.ico", self.text)
        self.assertTrue((REPO / "packaging" / "mnemos.ico").is_file())

    def test_the_fail_closed_config_tables_are_bundled(self) -> None:
        for name in ("source_policies.json", "model_prices.json",
                     "score_config.json"):
            self.assertIn(name, self.text, f"{name} missing from datas")

    def test_macos_bundle_declares_its_capture_purposes(self) -> None:
        """Without these keys macOS never shows the prompt and the mic is silent."""
        self.assertIn("NSMicrophoneUsageDescription", self.text)


class InnoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = ISS.read_text()

    def test_the_wipe_targets_local_appdata(self) -> None:
        """Must match runtime.user_data_root(); Roaming would also replicate
        gigabytes of meeting audio to a corporate file server."""
        self.assertIn("{localappdata}\\Mnemos", self.text)
        self.assertNotIn("DelTree(ExpandConstant('{userappdata}\\Mnemos')",
                         self.text)

    def test_it_still_clears_the_runtime_session_dirs(self) -> None:
        self.assertIn("{app}\\sessions", self.text)
        self.assertIn("{app}\\desktop_agent\\sessions", self.text)

    def test_it_has_an_icon(self) -> None:
        self.assertIn("SetupIconFile=mnemos.ico", self.text)

    def test_start_on_login_is_opt_in_hkcu(self) -> None:
        """Ambient capture must not autostart unless the tester ticked it."""
        self.assertIn("startupicon", self.text)
        self.assertIn("CurrentVersion\\Run", self.text)
        self.assertIn("PrivilegesRequired=lowest", self.text)
        # Default-on would start capture-capable software at login.
        startup = self.text.split('Name: "startupicon"')[1].split("\n")[0]
        self.assertIn("unchecked", startup)

    def test_ollama_setup_is_not_bundled(self) -> None:
        """Admin prompt + pinned version. First-run pulls if ollama is present."""
        files = self.text.split("[Files]")[1].split("[Icons]")[0]
        sources = [ln for ln in files.splitlines()
                   if ln.lstrip().startswith("Source:")]
        self.assertTrue(sources)
        self.assertFalse(any("Ollama" in ln for ln in sources))
        self.assertNotIn("Ollama.Ollama", self.text)


class DesktopAppTests(unittest.TestCase):
    def test_health_wait_gives_up_rather_than_hanging(self) -> None:
        with patch.object(desktop_app, "urlopen",
                          side_effect=OSError("refused")):
            self.assertFalse(
                desktop_app.wait_for_health("http://127.0.0.1:9", timeout_s=0.2))

    def test_health_wait_accepts_a_live_server(self) -> None:
        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(desktop_app, "urlopen", lambda *a, **k: _Resp()):
            self.assertTrue(
                desktop_app.wait_for_health("http://127.0.0.1:8000", timeout_s=1))

    def test_a_missing_window_toolkit_falls_back_to_the_browser(self) -> None:
        """pywebview absent must open a browser, not crash the app."""
        opened = []
        with patch.dict(sys.modules, {"webview": None}), \
                patch("webbrowser.open", opened.append):
            self.assertFalse(desktop_app.open_window("http://127.0.0.1:8000"))
        self.assertEqual(opened, ["http://127.0.0.1:8000"])

    def test_a_missing_tray_toolkit_is_not_fatal(self) -> None:
        with patch.dict(sys.modules, {"pystray": None}):
            self.assertIsNone(
                desktop_app.start_tray("http://127.0.0.1:8000", on_quit=lambda: None))

    def test_the_tray_stop_uses_the_same_call_as_the_ui(self) -> None:
        from app.services import wipe
        with patch.object(wipe, "stop_capture", lambda: {"ok": True}):
            self.assertIn("stopped", desktop_app._stop_capture())

    def test_a_frozen_build_with_missing_weights_opens_bootstrap(self) -> None:
        url = desktop_app.launch_url(
            "http://127.0.0.1:8000", frozen=True,
            missing_weights=["whisper 'small'"])
        self.assertEqual(url, "http://127.0.0.1:8000/bootstrap")

    def test_a_checkout_never_hijacks_the_first_screen(self) -> None:
        url = desktop_app.launch_url(
            "http://127.0.0.1:8000", frozen=False,
            missing_weights=["whisper 'small'"])
        self.assertEqual(url, "http://127.0.0.1:8000")

    def test_cached_weights_open_the_app_not_bootstrap(self) -> None:
        url = desktop_app.launch_url(
            "http://127.0.0.1:8000", frozen=True, missing_weights=[])
        self.assertEqual(url, "http://127.0.0.1:8000")

    def test_the_desktop_build_does_not_spawn_script_children(self) -> None:
        """run_all.py's browser-agent/coordinator children re-exec Mnemos.exe."""
        text = (REPO / "desktop_app.py").read_text()
        self.assertNotIn("exec_webapp.py", text)
        self.assertNotIn("Popen", text)


class SelfTestTests(unittest.TestCase):
    def test_it_names_the_module_whose_absence_is_silent(self) -> None:
        names = [n for n, _why in desktop_app.CRITICAL_IMPORTS]
        for required in ("torch", "silero_vad", "faster_whisper"):
            self.assertIn(required, names)

    def test_it_passes_in_a_working_environment(self) -> None:
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()) as out:
            code = desktop_app.self_test()
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("PASS", out.getvalue())

    def test_a_missing_module_fails_the_build_check(self) -> None:
        import io
        from contextlib import redirect_stdout
        with patch.object(desktop_app, "CRITICAL_IMPORTS",
                          (("definitely_not_a_module", "the canary"),)):
            with redirect_stdout(io.StringIO()) as out:
                code = desktop_app.self_test()
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out.getvalue())
        self.assertIn("definitely_not_a_module", out.getvalue())

    def test_missing_weights_are_not_a_build_failure(self) -> None:
        """A download a tester can complete is not a broken bundle."""
        import io
        from contextlib import redirect_stdout
        from app.services import model_fetch
        with patch.object(model_fetch, "check", lambda log: ["whisper 'small'"]):
            with redirect_stdout(io.StringIO()) as out:
                code = desktop_app.self_test()
        self.assertEqual(code, 0)
        self.assertIn("to download", out.getvalue())


class ReleaseWorkflowTests(unittest.TestCase):
    """CI built the bundle for months and never launched it."""

    def setUp(self) -> None:
        path = REPO / ".github" / "workflows" / "release.yml"
        if not path.is_file():
            self.skipTest("release workflow not present")
        self.text = path.read_text()

    def test_it_runs_the_self_test(self) -> None:
        self.assertIn("--self-test", self.text)

    def test_it_launches_what_it_built(self) -> None:
        self.assertIn("--no-window", self.text)
        self.assertIn("/health", self.text)
        self.assertIn("HF_HUB_OFFLINE", self.text)
        self.assertIn("HasExited", self.text)

    def test_it_builds_the_installer_not_just_the_folder(self) -> None:
        self.assertIn("ISCC.exe", self.text)
        self.assertIn("MnemosSetup.exe", self.text)

    def test_a_tag_publishes_the_setup_exe_as_the_install_link(self) -> None:
        self.assertIn("action-gh-release", self.text)
        self.assertIn("files: dist/MnemosSetup.exe", self.text)

    def test_tags_sign_with_trusted_signing_when_secrets_exist(self) -> None:
        self.assertIn("trusted-signing-action", self.text)
        self.assertIn("timestamp.acs.microsoft.com", self.text)
        self.assertIn("HAS_SIGNING", self.text)
        self.assertIn("AZURE_CLIENT_SECRET", self.text)
        # secrets.* in `if:` invalidates the whole workflow (zero jobs).
        if_lines = [ln for ln in self.text.splitlines() if ln.lstrip().startswith("if:")]
        self.assertFalse(any("secrets." in ln for ln in if_lines), if_lines)

    def test_program_files_x86_is_brace_quoted(self) -> None:
        """$env:ProgramFiles(x86) without braces evaluates to an empty path."""
        self.assertIn("${env:ProgramFiles(x86)}", self.text)

    def test_packaging_changes_trigger_it(self) -> None:
        self.assertIn("packaging/**", self.text)
        self.assertIn("desktop_app.py", self.text)


if __name__ == "__main__":
    unittest.main()
