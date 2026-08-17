"""Shared top nav: primary work vs More overflow."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.api.mnemos_theme import apply, nav_markup


class NavChromeTests(unittest.TestCase):
    def test_primary_contains_meetings(self) -> None:
        html = nav_markup()
        for label in ("Today", "Meetings", "Chat", "Memory", "You", "More"):
            self.assertIn(label, html)
        self.assertIn('id="navChat"', html)
        self.assertIn("/meetings", html)
        self.assertIn("/peer", html)
        self.assertIn("Setup", html)

    def test_tester_hides_desktop_phone_org(self) -> None:
        with patch.dict(os.environ, {"QUILL_PROFILE": "tester"}, clear=False):
            html = nav_markup()
        self.assertIn('href="/meetings"', html)
        self.assertIn('href="/peer"', html)
        self.assertNotIn('href="/desktop-access"', html)
        self.assertNotIn('href="/phone"', html)
        self.assertNotIn('href="/org-network"', html)

    def test_apply_injects_nav_placeholder(self) -> None:
        out = apply("<header>@@NAV@@</header>")
        self.assertNotIn("@@NAV@@", out)
        self.assertIn('id="mnemosNav"', out)


if __name__ == "__main__":
    unittest.main()
