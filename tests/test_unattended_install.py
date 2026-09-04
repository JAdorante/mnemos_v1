"""Unattended install — the contract the clean-install CI job depends on.

A prompt nobody can answer does not fail, it *hangs*, and a hung install on a
clean-machine test looks identical to a slow one until the job times out an
hour later. So the guards are asserted here rather than discovered in CI:
both installers must go quiet when told to, and must also work that out for
themselves when stdin is redirected.

The bash half is executed for real against a pty, because `[ -t 0 ]` is the
half that cannot be checked by reading the file. The PowerShell half is
static — there is no pwsh on the Linux dev box or the Linux CI runner, and the
windows-latest job in `.github/workflows/clean-install.yml` is what actually
runs it.
"""
from __future__ import annotations

import os
import pty
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.command"
INSTALL_PS1 = REPO / "scripts" / "install.ps1"
WORKFLOW = REPO / ".github" / "workflows" / "clean-install.yml"


def _prologue() -> str:
    """The shipped switch-detection block, lifted verbatim from the installer."""
    text = INSTALL_SH.read_text()
    m = re.search(r"^(NONINTERACTIVE=0\n.*?)^hold\(\)", text, re.S | re.M)
    assert m, "install.command no longer defines the unattended switches"
    return m.group(1)


def _decide(*, env: dict[str, str] | None = None, tty: bool) -> tuple[str, str]:
    """Run the real detection block and report (NONINTERACTIVE, SKIP_OLLAMA)."""
    script = _prologue() + '\nprintf "%s %s" "$NONINTERACTIVE" "$SKIP_OLLAMA"\n'
    full = dict(os.environ)
    full.pop("QUILL_INSTALL_NONINTERACTIVE", None)
    full.pop("QUILL_INSTALL_SKIP_OLLAMA", None)
    full.update(env or {})
    if tty:
        primary, secondary = pty.openpty()
        try:
            out = subprocess.run(["bash", "-c", script], stdin=secondary,
                                 capture_output=True, text=True, env=full).stdout
        finally:
            os.close(primary)
            os.close(secondary)
    else:
        with open(os.devnull) as devnull:
            out = subprocess.run(["bash", "-c", script], stdin=devnull,
                                 capture_output=True, text=True, env=full).stdout
    parts = out.strip().split()
    return (parts[0], parts[1])


@unittest.skipUnless(shutil.which("bash"), "bash not available")
class MacInstallerSwitchTests(unittest.TestCase):
    def test_a_real_terminal_still_prompts(self) -> None:
        """The tester experience must not change: a human still gets asked."""
        self.assertEqual(_decide(tty=True)[0], "0")

    def test_the_env_var_silences_it(self) -> None:
        non, _ = _decide(env={"QUILL_INSTALL_NONINTERACTIVE": "1"}, tty=True)
        self.assertEqual(non, "1")

    def test_a_redirected_stdin_is_enough_on_its_own(self) -> None:
        """The env var could be dropped from CI; this is the second net."""
        self.assertEqual(_decide(tty=False)[0], "1")

    def test_falsey_values_are_not_a_yes(self) -> None:
        for value in ("0", "false", "False", "no", ""):
            with self.subTest(value=value):
                non, _ = _decide(
                    env={"QUILL_INSTALL_NONINTERACTIVE": value}, tty=True)
                self.assertEqual(non, "0", f"{value!r} should not silence it")

    def test_ollama_skip_is_independent_of_interactivity(self) -> None:
        non, skip = _decide(env={"QUILL_INSTALL_SKIP_OLLAMA": "1"}, tty=True)
        self.assertEqual((non, skip), ("0", "1"))


class MacInstallerGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = INSTALL_SH.read_text()

    def test_every_prompt_sits_behind_a_guard(self) -> None:
        """No bare `read` may remain outside the unattended branches.

        The closing "press return to close" is the easy one to forget — it runs
        on the success path, so an unattended install would hang *after*
        reporting success.
        """
        for line_no, line in enumerate(self.text.splitlines(), 1):
            stripped = line.strip()
            if not re.search(r"\bread\s+-r\s+-p", stripped):
                continue
            self.assertTrue(
                stripped.startswith("hold()")
                or "NONINTERACTIVE" in line
                or self._inside_guarded_block(line_no),
                f"unguarded prompt at install.command:{line_no}: {stripped}")

    def _inside_guarded_block(self, line_no: int) -> bool:
        """True when some enclosing line above opened a non-interactive guard."""
        head = "\n".join(self.text.splitlines()[:line_no])
        return 'NONINTERACTIVE" != "1"' in head or 'NONINTERACTIVE" = "1"' in head

    def test_the_closing_pause_goes_through_hold(self) -> None:
        self.assertIn("\nhold\n", self.text)
        self.assertIn('hold() { [ "$NONINTERACTIVE" = "1" ] || read -r -p',
                      self.text)

    def test_failure_exits_instead_of_waiting_forever(self) -> None:
        self.assertRegex(self.text, r"fail\(\) \{[^\n]*hold; exit 1")

    def test_unattended_takes_the_key_from_the_environment(self) -> None:
        self.assertIn("QUILL_INVITE_CODE", self.text)
        self.assertIn("ANTHROPIC_API_KEY:-", self.text)

    def test_ollama_is_skippable_before_it_is_pulled(self) -> None:
        skip = self.text.index('if [ "$SKIP_OLLAMA" = "1" ]')
        pull = self.text.index("ollama pull qwen2.5:7b-instruct")
        self.assertLess(skip, pull, "the skip must precede the 4.7 GB pull")


class WindowsInstallerGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = INSTALL_PS1.read_text()

    def test_both_triggers_exist(self) -> None:
        self.assertIn("QUILL_INSTALL_NONINTERACTIVE", self.text)
        self.assertIn("[Console]::IsInputRedirected", self.text)

    def test_the_key_prompt_is_guarded(self) -> None:
        self.assertIn("if ((-not $nonInteractive) -and (-not $invited)) {",
                      self.text)

    def test_the_invite_prompt_is_the_interactive_branch(self) -> None:
        """`if ($nonInteractive) … elseif ($inviteUrl)` — not two ifs."""
        self.assertIn("} elseif ($inviteUrl) {", self.text)
        non = self.text.index("if ($nonInteractive) {\n    # Same two paths")
        prompt = self.text.index('Read-Host "  Choose 1, 2 or 3"')
        self.assertLess(non, prompt)

    def test_no_read_host_outside_those_two_blocks(self) -> None:
        guarded = ('Choose 1, 2 or 3', 'Invite code (like', 'Paste YOUR Anthropic')
        for line_no, line in enumerate(self.text.splitlines(), 1):
            if "Read-Host" not in line:
                continue
            self.assertTrue(any(g in line for g in guarded),
                            f"unguarded Read-Host at install.ps1:{line_no}: "
                            f"{line.strip()}")

    def test_winget_does_not_install_ollama_on_a_skipped_run(self) -> None:
        """Skipping the pulls but still installing ~1 GB of Ollama is not a skip."""
        guard = self.text.index("if (-not $skipOllama) {")
        winget = self.text.index("winget install -e --id Ollama.Ollama")
        self.assertLess(guard, winget)

    def test_unattended_takes_the_key_from_the_environment(self) -> None:
        self.assertIn("$env:QUILL_INVITE_CODE", self.text)
        self.assertIn("$env:ANTHROPIC_API_KEY", self.text)


class BatchLauncherExitCodeTests(unittest.TestCase):
    """`pause` succeeds even when the install failed.

    Without capturing ERRORLEVEL first, install.bat always exits 0 — so the
    clean-install job's "run the installer" step passes on a broken install and
    only the assertions after it catch anything.
    """

    def _text(self, name: str) -> str:
        return (REPO / name).read_text()

    def test_install_bat_propagates_the_installers_exit_code(self) -> None:
        text = self._text("install.bat")
        self.assertIn("set RC=%ERRORLEVEL%", text)
        self.assertIn("exit /b %RC%", text)
        # rindex: the word also appears in the comment explaining why.
        self.assertLess(text.index("set RC="), text.rindex("pause"),
                        "ERRORLEVEL must be captured before pause resets it")

    def test_uninstall_bat_propagates_too(self) -> None:
        text = self._text("uninstall.bat")
        self.assertIn("set RC=%ERRORLEVEL%", text)
        self.assertIn("exit /b %RC%", text)
        self.assertLess(text.index("set RC="), text.rindex("pause"))


class CleanInstallWorkflowTests(unittest.TestCase):
    """The job only proves something if it runs the launchers a tester runs."""

    def setUp(self) -> None:
        if not WORKFLOW.is_file():
            self.skipTest("clean-install workflow not present")
        self.text = WORKFLOW.read_text()

    def test_it_runs_the_real_installers(self) -> None:
        self.assertIn("install.bat < NUL", self.text)
        self.assertIn("bash install.command < /dev/null", self.text)

    def test_it_sets_both_switches(self) -> None:
        self.assertIn('QUILL_INSTALL_NONINTERACTIVE: "1"', self.text)
        self.assertIn('QUILL_INSTALL_SKIP_OLLAMA: "1"', self.text)

    def test_it_runs_on_machines_we_do_not_own(self) -> None:
        self.assertIn("runs-on: windows-latest", self.text)
        self.assertIn("runs-on: macos-latest", self.text)

    def test_it_proves_the_app_starts_and_then_uninstalls_clean(self) -> None:
        self.assertIn("/health", self.text)
        self.assertIn("scripts/uninstall.py --yes --credentials", self.text)
        self.assertIn("scripts\\uninstall.py --yes --credentials", self.text)
        self.assertIn("sparrow-deletion-receipt-", self.text)

    def test_it_does_not_use_force_to_dodge_the_running_server_probe(self) -> None:
        """--force here would hide a shutdown bug as a passing wipe."""
        self.assertNotIn("uninstall.py --yes --force", self.text)

    def test_the_manual_gaps_are_written_down_not_implied(self) -> None:
        for claim in ("SmartScreen", "Gatekeeper", "winget", "TCC"):
            self.assertIn(claim, self.text,
                          f"the workflow header should say {claim} is not covered")


if __name__ == "__main__":
    unittest.main()
