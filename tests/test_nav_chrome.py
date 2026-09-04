"""Shared top nav: primary work vs More overflow."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.api.mnemos_theme import apply, nav_markup


class NavChromeTests(unittest.TestCase):
    def test_primary_contains_core_links(self) -> None:
        html = nav_markup()
        for label in ("Today", "Chat", "Memory", "People", "More"):
            self.assertIn(label, html)
        self.assertIn('id="navChat"', html)
        self.assertIn("/profile?tab=people", html)
        self.assertIn("/meetings", html)
        self.assertIn("/peer", html)
        self.assertIn("Setup", html)
        self.assertIn("nav-profile", html)
        self.assertIn("Copyright 2026 Ravenry LLC", html)
        self.assertNotIn(">You</a>", html)
        # Meetings lives under More, not primary strip
        self.assertLess(html.index("Chat"), html.index('href="/meetings"'))

    def test_profile_control_has_chrome_css(self) -> None:
        from app.api.mnemos_theme import CHROME_CSS

        self.assertIn(".nav-profile{", CHROME_CSS)
        self.assertIn(".nav-profile-dot{", CHROME_CSS)
        self.assertIn("order:20", CHROME_CSS)
        self.assertIn(".nav-more[open] > .nav-more-menu", CHROME_CSS)
        self.assertIn(".nav-more-copy{", CHROME_CSS)

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
        self.assertIn("nav-profile", out)
        self.assertIn("Copyright 2026 Ravenry LLC", out)
        self.assertEqual(apply("@@COPYRIGHT@@"), "Copyright 2026 Ravenry LLC")


if __name__ == "__main__":
    unittest.main()
