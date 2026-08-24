"""Linux AT-SPI driver: fail-closed paths, gating, and dispatch wiring."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if sys.platform == "win32":
    raise unittest.SkipTest("Linux-only: AT-SPI driver wraps PyGObject Atspi")

from desktop_agent import a11y, a11y_linux  # noqa: E402
from desktop_agent.driver import DesktopDriver  # noqa: E402


def _driver(approve: bool = True):
    calls = []
    jail = Path(tempfile.mkdtemp(prefix="quill_jail_"))

    def ask(summary, details="", action=None):
        calls.append((action, summary))
        return approve

    d = DesktopDriver(jail_root=jail, on_log=lambda s: None, on_approve=ask)
    return d, calls


class A11yFailClosedTests(unittest.TestCase):
    def test_scan_refuses_non_allowlisted_app(self) -> None:
        d, calls = _driver()
        res = d.ui_scan("regedit")
        self.assertFalse(res["ok"])
        self.assertIn("not allowlisted", res["detail"])
        self.assertEqual(calls, [])

    def test_invoke_without_scan_fails_cleanly(self) -> None:
        with a11y_linux._lock:
            a11y_linux._last_controls = []
        d, _calls = _driver(approve=True)
        res = d.ui_invoke(0)
        self.assertFalse(res["ok"])
        self.assertIn("ui_scan", res["detail"])

    def test_set_text_denied_gate_blocks(self) -> None:
        d, calls = _driver(approve=False)
        with mock.patch.object(a11y, "describe", return_value="edit: Notes"), \
             mock.patch.object(a11y, "last_window_title", return_value="Notes"), \
             mock.patch.object(a11y, "set_value",
                               side_effect=AssertionError("must not run")):
            res = d.ui_set_text(0, "new content")
        self.assertFalse(res["ok"])
        self.assertEqual(res["detail"], "denied")
        action, summary = calls[0]
        self.assertEqual(action, "ui_set_text")
        self.assertIn("REPLACE", summary)


if __name__ == "__main__":
    unittest.main()
