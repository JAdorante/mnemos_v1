"""General-purpose browser surfaces + perception + messaging mode."""
from __future__ import annotations

import unittest

from browser_agent.modes import resolve_mode
from browser_agent.perception import render_observation, signature
from browser_agent.provider_tips import tips_for_url
from browser_agent.surfaces import (
    is_chat_host,
    is_chat_surface,
    is_open_conversation_url,
    looks_like_chat,
    wants_early_vision,
)


class SurfaceTests(unittest.TestCase):
    def test_chat_hosts(self):
        self.assertTrue(is_chat_host("https://www.snapchat.com/web"))
        self.assertTrue(is_chat_host("https://web.whatsapp.com/"))
        self.assertTrue(is_chat_host(host="discord.com"))
        self.assertFalse(is_chat_host("https://example.com/about"))

    def test_open_conversation_url(self):
        self.assertFalse(is_open_conversation_url("https://www.snapchat.com/web"))
        self.assertTrue(is_open_conversation_url(
            "https://www.snapchat.com/web/dec6501f-cdaa-5660"))
        self.assertTrue(is_open_conversation_url(
            "https://web.whatsapp.com/send?phone=1"))

    def test_early_vision_on_chat(self):
        self.assertTrue(wants_early_vision("https://www.snapchat.com/web/abc"))
        self.assertFalse(wants_early_vision("https://example.com/docs"))

    def test_structural_chat_detection_unlisted_host(self):
        # A self-hosted chat app (NOT in CHAT_HOSTS) recognized by structure:
        # composer + message-log region.
        scan = {
            "url": "https://chat.internal.corp/", "elements": [
                {"id": 0, "role": "textbox", "editable": True,
                 "name": "Write a message", "tag": "textarea"},
            ],
            "chat_signals": {"has_log": True, "composers": 1, "list_rows": 3},
        }
        self.assertTrue(looks_like_chat(scan))
        self.assertTrue(wants_early_vision("https://chat.internal.corp/",
                                           scan=scan))

    def test_structural_chat_rows_plus_selection(self):
        scan = {
            "elements": [{"id": 0, "name": "Friend", "selected": True}],
            "chat_signals": {"has_log": False, "composers": 1, "list_rows": 12},
        }
        self.assertTrue(looks_like_chat(scan))

    def test_plain_site_with_search_box_is_not_chat(self):
        # A composer alone (e.g. a search box) must not qualify.
        scan = {
            "elements": [{"id": 0, "role": "searchbox", "editable": True}],
            "chat_signals": {"has_log": False, "composers": 1, "list_rows": 0},
        }
        self.assertFalse(looks_like_chat(scan))
        self.assertFalse(looks_like_chat(None))
        self.assertFalse(looks_like_chat({}))


class MessagingModeTests(unittest.TestCase):
    def test_snapchat_site_is_messaging_not_research(self):
        m = resolve_mode("send_chat_message", "snapchat.com/web")
        self.assertEqual(m.key, "messaging")

    def test_read_alone_still_research(self):
        m = resolve_mode("read", "example.com")
        self.assertEqual(m.key, "research")

    def test_whatsapp_intent(self):
        m = resolve_mode("read_message", "whatsapp")
        self.assertEqual(m.key, "messaging")


class PerceptionTests(unittest.TestCase):
    def test_signature_includes_page_and_selection(self):
        scan = {
            "url": "https://chat.example/web/1",
            "title": "Chat",
            "count": 1,
            "scrollY": 0,
            "page_text": "hello from them",
            "selected": "Friend",
            "elements": [
                {"name": "Friend", "selected": True},
                {"name": "Send a chat", "editable": True, "value": "hi"},
            ],
        }
        sig = signature(scan)
        self.assertIn("page_hash", sig)
        self.assertEqual(sig["selected"], "Friend")
        self.assertIn("hi", sig["compose"])
        # page text change flips hash
        scan2 = dict(scan, page_text="hello from them — new line")
        self.assertNotEqual(signature(scan)["page_hash"],
                            signature(scan2)["page_hash"])

    def test_observation_shows_page_text(self):
        scan = {
            "url": "https://x/web/1", "title": "t", "count": 0,
            "scrollY": 0, "scrollMax": 0,
            "page_text": "visible bubble text here",
            "elements": [],
        }
        obs = render_observation(scan)
        self.assertIn("Visible page text", obs)
        self.assertIn("visible bubble text here", obs)

    def test_long_page_text_keeps_tail(self):
        # Chat logs append at the bottom — truncation must keep the NEWEST text.
        newest = "NEWEST MESSAGE AT THE END"
        scan = {
            "url": "https://x/web/1", "title": "t", "count": 0,
            "scrollY": 0, "scrollMax": 0,
            "page_text": ("old " * 900) + newest,
            "elements": [],
        }
        obs = render_observation(scan)
        self.assertIn(newest, obs)
        self.assertIn("TRUNCATED", obs)

    def test_scan_marked_truncated_flags_observation(self):
        scan = {
            "url": "https://x/web/1", "title": "t", "count": 0,
            "scrollY": 0, "scrollMax": 0,
            "page_text": "short but pre-truncated by SCAN_JS",
            "page_truncated": True,
            "elements": [],
        }
        self.assertIn("TRUNCATED", render_observation(scan))


class ProviderTipTests(unittest.TestCase):
    def test_generic_chat_tip(self):
        tip = tips_for_url("https://www.snapchat.com/web/abc")
        self.assertIn("Web chat tip", tip)
        tip2 = tips_for_url("https://web.whatsapp.com/")
        self.assertIn("Web chat tip", tip2)


class SpiralGuardTests(unittest.TestCase):
    def test_consecutive_repeats_counted(self):
        from browser_agent.orchestrator import _SpiralGuard
        g = _SpiralGuard()
        self.assertEqual(g.observe("click:a", "s1"), 0)
        self.assertEqual(g.observe("click:a", "s1"), 1)
        self.assertEqual(g.observe("click:a", "s1"), 2)

    def test_oscillation_trips_via_state_pairs(self):
        # click A, click B, click A, click B — the last-action counter stays 0,
        # but revisiting (state, action) pairs must climb.
        from browser_agent.orchestrator import _SpiralGuard
        g = _SpiralGuard()
        self.assertEqual(g.observe("click:a", "s1"), 0)
        self.assertEqual(g.observe("click:b", "s1"), 0)
        self.assertEqual(g.observe("click:a", "s1"), 1)
        self.assertEqual(g.observe("click:b", "s1"), 1)
        self.assertEqual(g.observe("click:a", "s1"), 2)

    def test_state_change_is_progress_not_spiral(self):
        # Same action on a CHANGED page (e.g. paging "Next") is real progress.
        from browser_agent.orchestrator import _SpiralGuard
        g = _SpiralGuard()
        g.observe("click:next", "page1")
        self.assertEqual(g.observe("click:next", "page2"), 1)  # streak only
        g2 = _SpiralGuard()
        g2.observe("click:next", "page1")
        g2.observe("read:{}", "page1")
        self.assertEqual(g2.observe("click:next", "page2"), 0)

    def test_forgive_grants_fresh_start(self):
        from browser_agent.orchestrator import _SpiralGuard
        g = _SpiralGuard()
        for _ in range(3):
            n = g.observe("click:a", "s1")
        self.assertEqual(n, 2)
        g.forgive("click:a", "s1")
        self.assertEqual(g.observe("click:a", "s1"), 0)
        self.assertEqual(g._pairs["click:a@s1"], 1)


class DistillTests(unittest.TestCase):
    def test_distill_keeps_read_and_wall_note(self):
        from browser_agent.orchestrator import _distill

        hist = [
            {"action": "navigate", "args": {"url": "https://a/web"}, "verified": True},
            {"action": "read", "verified": True, "read_text": "hi"},
            {"action": "click", "verified": False, "vreason": "no change",
             "target": "row"},
        ]
        recipe, notes = _distill(hist, status="stopped_repeat")
        self.assertTrue(any("read" in r for r in recipe))
        self.assertTrue(any("SPA/chat" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
