"""UIA-first desktop driver: fail-closed paths, gating, and dispatch wiring.

No windows are launched here — live behavior (Notepad scan/set/read-back) is
covered by the scratchpad verification; these tests pin the safety contract.
"""
from __future__ import annotations

import unittest
from unittest import mock

from desktop_agent import uia
from desktop_agent.driver import DesktopDriver


def _driver(approve: bool = True):
    calls = []

    def ask(summary, details="", action=None):
        calls.append((action, summary))
        return approve

    d = DesktopDriver(on_log=lambda s: None, on_approve=ask)
    return d, calls


class UiaFailClosedTests(unittest.TestCase):
    def test_scan_refuses_non_allowlisted_app(self) -> None:
        d, calls = _driver()
        res = d.ui_scan("regedit")
        self.assertFalse(res["ok"])
        self.assertIn("not allowlisted", res["detail"])
        self.assertEqual(calls, [])          # refused before any gate

    def test_invoke_without_scan_fails_cleanly(self) -> None:
        with uia._lock:
            uia._last_controls = []
        d, _calls = _driver(approve=True)
        res = d.ui_invoke(0)
        self.assertFalse(res["ok"])
        self.assertIn("ui_scan", res["detail"])

    def test_invoke_bad_id_refused(self) -> None:
        d, _calls = _driver()
        res = d.ui_invoke("not-a-number")
        self.assertFalse(res["ok"])
        self.assertIn("integer", res["detail"])

    def test_set_text_denied_gate_blocks(self) -> None:
        d, calls = _driver(approve=False)
        with mock.patch.object(uia, "describe", return_value="edit: Notes"), \
             mock.patch.object(uia, "last_window_title", return_value="Notes - App"), \
             mock.patch.object(uia, "set_value",
                               side_effect=AssertionError("must not run")):
            res = d.ui_set_text(0, "new content")
        self.assertFalse(res["ok"])
        self.assertEqual(res["detail"], "denied")
        # The approval prompt names the control, the window, and REPLACE.
        action, summary = calls[0]
        self.assertEqual(action, "ui_set_text")
        self.assertIn("REPLACE", summary)
        self.assertIn("edit: Notes", summary)
        self.assertIn("Notes - App", summary)

    def test_invoke_approval_names_window(self) -> None:
        d, calls = _driver(approve=True)
        with mock.patch.object(uia, "describe", return_value="button: Save"), \
             mock.patch.object(uia, "last_window_title", return_value="Doc - App"), \
             mock.patch.object(uia, "invoke", return_value="invoked"):
            res = d.ui_invoke(3)
        self.assertTrue(res["ok"])
        action, summary = calls[0]
        self.assertEqual(action, "ui_invoke")
        self.assertIn("button: Save", summary)
        self.assertIn("Doc - App", summary)
        self.assertIn("no mouse taken", summary)


class DispatchWiringTests(unittest.TestCase):
    def test_desktop_dispatch_routes_ui_actions(self) -> None:
        from browser_agent.orchestrator import Agent

        d = mock.Mock()
        Agent._desktop_dispatch(mock.Mock(), d, "ui_scan",
                                {"app": "notepad", "title": "t"})
        d.ui_scan.assert_called_once_with("notepad", title="t")
        Agent._desktop_dispatch(mock.Mock(), d, "ui_invoke", {"control_id": 4})
        d.ui_invoke.assert_called_once_with(4)
        Agent._desktop_dispatch(mock.Mock(), d, "ui_set_text",
                                {"control_id": 1, "text": "x"})
        d.ui_set_text.assert_called_once_with(1, "x")

    def test_desktop_tools_include_uia(self) -> None:
        from browser_agent.tools import DESKTOP_TOOLS

        names = {t["name"] for t in DESKTOP_TOOLS}
        self.assertLessEqual({"ui_scan", "ui_invoke", "ui_set_text"}, names)
        pixel = next(t for t in DESKTOP_TOOLS if t["name"] == "click_at")
        self.assertIn("FALLBACK", pixel["description"])


if __name__ == "__main__":
    unittest.main()
