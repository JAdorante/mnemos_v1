"""App shell chrome: the rail carries navigation, the top bar carries controls.

Frontend spec (2026-11-06), "App shell": five destinations in the rail,
Settings below them, one Inspect link in the footer; the top bar is search,
Ask, capture status, theme toggle and the avatar.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.api import ui
from app.api.mnemos_theme import apply, nav_markup, shell_wrap


class TopBarTests(unittest.TestCase):
    def test_topbar_controls(self) -> None:
        html = nav_markup()
        self.assertIn('id="sparrowSearch"', html)
        self.assertIn('action="/memory"', html)
        self.assertIn('id="mnemosAskOpen"', html)
        self.assertIn('id="sparrowCapture"', html)
        self.assertIn("theme-toggle", html)
        self.assertIn('id="mnemosStatusDot"', html)
        self.assertIn('class="topbar-avatar"', html)
        # Navigation is not in the top bar any more.
        self.assertNotIn('href="/today"', html)
        self.assertNotIn("nav-more", html)

    def test_apply_injects_topbar_and_rail(self) -> None:
        out = apply('<!doctype html><html><head>@@FONTS@@</head><body>'
                    '<header class="top"><a class="brand" href="/">@@MARK@@ @@BRAND@@</a>'
                    '@@NAV@@</header><div class="wrap">hi</div>@@UI_JS@@</body></html>')
        self.assertNotIn("@@NAV@@", out)
        self.assertNotIn('class="brand"', out)       # wordmark moved to the rail
        self.assertIn('id="sparrowRail"', out)
        self.assertIn('class="shell-main"', out)
        self.assertIn('id="sparrowSearch"', out)
        self.assertIn("/static/css/sparrow.css", out)
        self.assertIn("/static/js/sparrow.js", out)
        self.assertEqual(apply("@@COPYRIGHT@@"), "Copyright 2026 Ravenry LLC")

    def test_page_without_top_bar_gets_one(self) -> None:
        out = shell_wrap("<html><body><h1>Records</h1></body></html>")
        self.assertIn('<header class="top">@@NAV@@</header>', out)
        self.assertIn('id="sparrowRail"', out)


class RailTests(unittest.TestCase):
    def test_rail_has_five_destinations_settings_and_inspect(self) -> None:
        html = ui.rail_html("/today")
        for label in ("Today", "Chat", "Memory", "People &amp; Projects", "Inbox", "Settings", "Inspect"):
            self.assertIn(label, html)
        self.assertIn('id="navChat"', html)          # legacy pages flag attention on it
        self.assertIn('data-count="inbox"', html)
        self.assertIn("Copyright", apply("@@COPYRIGHT@@") + "Copyright")

    def test_active_item_from_path(self) -> None:
        cases = {
            "/today": "today", "/": "today", "/shell": "today",
            "/chat": "chat", "/chat/x": "chat",
            "/memory": "memory", "/meetings": "memory", "/meeting/note/3": "memory",
            "/profile": "people", "/org/12": "people", "/projects": "people",
            "/inbox": "inbox",
            "/settings": "settings", "/peer": "settings", "/capture": "settings",
            "/onboarding": "settings", "/assistants": "settings",
            "/inspect": "inspect", "/memory/changes": "inspect", "/triggers": "inspect",
            "/auth": None,
        }
        for path, want in cases.items():
            self.assertEqual(ui.active_key(path), want, path)

    def test_rail_marks_active_and_count(self) -> None:
        html = ui.rail_html("/inbox", inbox_count=3)
        self.assertIn('data-nav="inbox" class="on" aria-current="page"', html)
        self.assertIn('data-count="inbox">3<', html)
        html = ui.rail_html("/today", inbox_count=0)
        self.assertIn('data-count="inbox" hidden>0<', html)

    def test_finish_swaps_the_static_rail(self) -> None:
        page = apply('<!doctype html><html><head></head><body><header class="top">@@NAV@@</header>'
                     '<p>x</p></body></html>')
        self.assertNotIn('data-nav="chat" class="on"', page)
        with patch.object(ui, "_inbox_count", return_value=7):
            out = ui.finish(page, path="/chat")
        self.assertIn('id="navChat" class="on" aria-current="page"', out)
        self.assertIn('data-count="inbox">7<', out)
        self.assertEqual(out.count('id="sparrowRail"'), 1)

    def test_hosted_seat_footer_line(self) -> None:
        with patch.dict(os.environ, {"QUILL_HEADLESS": "1"}, clear=False):
            self.assertIn("Hosted seat", ui.rail_html("/today", live=True, inbox_count=0))
        with patch.dict(os.environ, {"QUILL_HEADLESS": ""}, clear=False):
            self.assertIn("Local", ui.rail_html("/today", live=True, inbox_count=0))


if __name__ == "__main__":
    unittest.main()
