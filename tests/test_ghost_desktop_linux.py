"""Linux ghost desktop: exclusion policy, capture guards, and soft-fail paths."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if sys.platform == "win32":
    raise unittest.SkipTest("Linux-only: ghost_x11 parks/captures X11 windows")

from desktop_agent import config as dcfg, ghost_x11  # noqa: E402
from desktop_agent.driver import DesktopDriver  # noqa: E402


class GhostableTests(unittest.TestCase):
    def test_excluded_apps_stay_visible(self) -> None:
        with mock.patch.object(dcfg, "GHOST_DESKTOP", True), \
             mock.patch("desktop_agent.ghost_x11.enabled", return_value=True):
            self.assertFalse(ghost_x11.ghostable("flstudio"))
            self.assertFalse(ghost_x11.ghostable("phonelink"))

    def test_normal_apps_ghost_when_enabled(self) -> None:
        with mock.patch.object(dcfg, "GHOST_DESKTOP", True), \
             mock.patch("desktop_agent.ghost_x11.enabled", return_value=True):
            self.assertTrue(ghost_x11.ghostable("gedit"))

    def test_disabled_flag_wins(self) -> None:
        with mock.patch.object(dcfg, "GHOST_DESKTOP", False):
            self.assertFalse(ghost_x11.ghostable("gedit"))


class CaptureGuardTests(unittest.TestCase):
    def test_window_png_bad_xid(self) -> None:
        self.assertIsNone(ghost_x11.window_png(0))

    def test_park_disabled_refuses(self) -> None:
        with mock.patch("desktop_agent.ghost_x11.enabled", return_value=False):
            g = ghost_x11.park_new_windows("gedit", set())
        self.assertFalse(g["ok"])
        self.assertIn("disabled", g["reason"])


class DriverGhostFrameTests(unittest.TestCase):
    def test_ghost_frame_only_streams_parked_windows(self) -> None:
        from desktop_agent import a11y
        from desktop_agent.driver import DesktopDriver

        jail = Path(tempfile.mkdtemp(prefix="quill_jail_"))
        d = DesktopDriver(jail_root=jail, on_log=lambda s: None,
                          on_approve=lambda *a, **k: True)
        with mock.patch.object(a11y, "last_window_hwnd", return_value=12345), \
             mock.patch.object(ghost_x11, "parked_apps", return_value={}), \
             mock.patch.object(ghost_x11, "publish_frame",
                               side_effect=AssertionError("user window streamed")):
            d._ghost_frame()


if __name__ == "__main__":
    unittest.main()
