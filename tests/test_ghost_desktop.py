"""Ghost desktop: exclusion policy, capture guards, and no-window fallback.

Live behavior (Explorer parked off-screen, PrintWindow frames, relay publish)
is verified by the scratchpad test; these pin the policy and the soft-fail
paths without launching real windows.
"""
from __future__ import annotations

import unittest
from unittest import mock

from desktop_agent import config as dcfg, ghost_win


class GhostableTests(unittest.TestCase):
    def test_excluded_apps_stay_visible(self) -> None:
        with mock.patch.object(dcfg, "GHOST_DESKTOP", True):
            self.assertFalse(ghost_win.ghostable("flstudio"))
            self.assertFalse(ghost_win.ghostable("phonelink"))
            self.assertFalse(ghost_win.ghostable("FLStudio"))

    def test_normal_apps_ghost_when_enabled(self) -> None:
        with mock.patch.object(dcfg, "GHOST_DESKTOP", True):
            self.assertTrue(ghost_win.ghostable("notepad"))
            self.assertTrue(ghost_win.ghostable("explorer"))

    def test_disabled_flag_wins(self) -> None:
        with mock.patch.object(dcfg, "GHOST_DESKTOP", False):
            self.assertFalse(ghost_win.ghostable("notepad"))

    def test_exclude_list_is_configurable_data(self) -> None:
        with mock.patch.object(dcfg, "GHOST_DESKTOP", True), \
             mock.patch.object(dcfg, "GHOST_DESKTOP_EXCLUDE",
                               frozenset({"notepad"})):
            self.assertFalse(ghost_win.ghostable("notepad"))
            self.assertTrue(ghost_win.ghostable("flstudio"))


class CaptureGuardTests(unittest.TestCase):
    def test_window_png_bad_hwnd(self) -> None:
        self.assertIsNone(ghost_win.window_png(0))
        self.assertIsNone(ghost_win.window_png(0xDEAD0000))

    def test_snapshot_windows_returns_set(self) -> None:
        s = ghost_win.snapshot_windows()
        self.assertIsInstance(s, set)

    def test_park_no_new_windows_soft_fails(self) -> None:
        with mock.patch.object(dcfg, "GHOST_DESKTOP", True):
            before = ghost_win.snapshot_windows()
            g = ghost_win.park_new_windows("notepad", before,
                                           retries=1, delay_s=0.0)
        self.assertFalse(g["ok"])
        self.assertIn("no new window", g["reason"])

    def test_park_disabled_refuses(self) -> None:
        with mock.patch.object(dcfg, "GHOST_DESKTOP", False):
            g = ghost_win.park_new_windows("notepad", set())
        self.assertFalse(g["ok"])
        self.assertIn("disabled", g["reason"])


class DriverGhostFrameTests(unittest.TestCase):
    def test_ghost_frame_without_scan_is_safe(self) -> None:
        from desktop_agent.driver import DesktopDriver

        d = DesktopDriver(on_log=lambda s: None, on_approve=lambda *a, **k: True)
        d._ghost_frame()   # no scan yet, nothing parked — must not raise

    def test_ghost_frame_only_streams_parked_windows(self) -> None:
        from desktop_agent import uia
        from desktop_agent.driver import DesktopDriver

        d = DesktopDriver(on_log=lambda s: None, on_approve=lambda *a, **k: True)
        with mock.patch.object(uia, "last_window_hwnd", return_value=12345), \
             mock.patch.object(ghost_win, "parked_apps", return_value={}), \
             mock.patch.object(ghost_win, "publish_frame",
                               side_effect=AssertionError("user window streamed")):
            d._ghost_frame()   # 12345 not parked -> never published


if __name__ == "__main__":
    unittest.main()
