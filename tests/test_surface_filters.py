"""Tests for terminal/CLI intake scrubbing + OS-account name gate."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.services import name_quality as nq
from app.services import self_profile
from app.services import surface_filters as sf


class IntakeScrubTests(unittest.TestCase):
    def test_console_window_not_ingested(self):
        self.assertTrue(sf.is_console_window("Windows PowerShell"))
        self.assertFalse(sf.should_ingest_screen("Windows Terminal",
                                                 ocr="PS C:\\> dir", summary=""))

    def test_strip_clasp_keeps_real_text(self):
        raw = (
            "Go onto Uber Eats and find Chinese food\n"
            "clasp push\n"
            "clasp login\n"
            "Compose mail to Patrick"
        )
        cleaned = sf.strip_noise_lines(raw)
        self.assertIn("Uber Eats", cleaned)
        self.assertIn("Compose mail", cleaned)
        self.assertNotIn("clasp push", cleaned)
        self.assertNotIn("clasp login", cleaned)

    def test_cli_only_not_ingested(self):
        self.assertFalse(sf.should_ingest_screen(
            "nexus_v1 - Cursor",
            ocr="clasp push\nclasp login\nclasp logout",
            summary="clasp push"))

    def test_scrub_vision_drops_cli_items(self):
        res = sf.scrub_vision_result({
            "ocr_text": "Buy milk\nclasp push\ngit status",
            "description": "A todo list with Buy milk",
            "items": ["Buy milk", "clasp push", "Call dentist"],
            "content_type": "todo_list",
        })
        self.assertIsNotNone(res)
        assert res is not None
        self.assertNotIn("clasp push", res["ocr_text"])
        self.assertEqual(res["items"], ["Buy milk", "Call dentist"])

    def test_real_todo_still_ingested(self):
        self.assertTrue(sf.should_ingest_screen(
            "Microsoft To Do",
            ocr="Buy milk\nCall the dentist",
            summary="Buy milk"))


class OsAccountPersonTests(unittest.TestCase):
    def test_os_username_rejected_as_person(self):
        with patch.dict(os.environ, {"USERNAME": "Dell AI User", "USER": ""}, clear=False):
            self.assertTrue(nq.is_os_account_name("Dell AI User"))
            self.assertFalse(nq.is_plausible_person("Dell AI User"))

    def test_os_username_is_not_self(self):
        # Must not park path-OCR owners on the real user.
        with patch.dict(os.environ, {"USERNAME": "Dell AI User"}, clear=False):
            self.assertFalse(self_profile.is_self_name("Dell AI User"))
            self.assertFalse(self_profile.is_self_name("dell ai user"))
        self.assertTrue(self_profile.is_self_name("me"))

    def test_real_name_still_person(self):
        self.assertTrue(nq.is_plausible_person("Justin Adorante"))
        self.assertFalse(self_profile.is_self_name("Justin Adorante"))


class PrefixMatchTests(unittest.TestCase):
    def test_nickname_to_full_name(self):
        from app.services.resolution import _prefix_match
        self.assertTrue(_prefix_match("Chris", "Chris Falloon"))
        self.assertTrue(_prefix_match("Chris Falloon", "Chris"))

    def test_no_string_prefix_false_merge(self):
        from app.services.resolution import _prefix_match
        self.assertFalse(_prefix_match("Chris", "Christina"))
        self.assertFalse(_prefix_match("Chris", "Christopher"))
        self.assertFalse(_prefix_match("Marc", "Marcus"))


class SocialFeedTests(unittest.TestCase):
    def test_viral_post_is_activity_only(self):
        ocr = (
            "internet hall of fame @InternetH0F\n"
            "Ben Shapiro calls 'The Odyssey' a masterpiece\n"
            "182.8K Views\n166 replies 69 reposts 2.8K likes 137 bookmarks\n"
            "Post your reply"
        )
        self.assertTrue(sf.is_social_feed_surface(
            "X", title="", ocr=ocr, summary="social post"))
        self.assertTrue(sf.is_activity_only_social(
            "X", "", ocr, "social post"))
        self.assertFalse(sf.is_user_social_compose("X", "", ocr, ""))

    def test_linkedin_style_feed_activity_only(self):
        ocr = (
            "For you\nJane Doe @janedoe\n"
            "Excited to announce...\n"
            "1.2K reactions 84 comments 40 reposts"
        )
        self.assertTrue(sf.is_activity_only_social(
            "Feed", "", ocr, ""))

    def test_compose_new_post_allows_extract(self):
        self.assertFalse(sf.is_activity_only_social(
            "Compose post", "Compose",
            ocr="What's on your mind?\nRemind Marc about the deck",
            summary="compose"))
        self.assertTrue(sf.is_user_social_compose(
            "Compose post", "Compose",
            "What's on your mind?\nRemind Marc about the deck", ""))

    def test_email_inbox_not_treated_as_feed(self):
        ocr = "Inbox\nFrom: Patrick\nSubject: Weekly update\nPlease review the deck"
        self.assertFalse(sf.is_social_feed_surface(
            "Mail - Outlook", "", ocr, ""))
        self.assertFalse(sf.is_activity_only_social(
            "Mail - Outlook", "", ocr, ""))


if __name__ == "__main__":
    unittest.main()
