"""Self-suppression guards — the app must not hear itself speak or watch its
own dashboard into memory.

Two feedback loops observed live (July 28 2026): TTS replies transcribed back
in as heard speech, and screen frames of the app's own console minting
entities ("Memory Console" in the constellation).
"""
from __future__ import annotations

import unittest

from app.services import name_quality as nq
from app.services import voice
from app.services.surface_filters import is_console_window, is_self_window

NOW = 1_800_000_000.0


class SpokenRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        with voice._SPOKEN_LOCK:
            voice._spoken.clear()

    def test_exact_reply_is_recognized(self) -> None:
        voice.register_spoken("Your meeting with Marc is at 3pm today.", now=NOW)
        self.assertTrue(voice.recently_spoken(
            "Your meeting with Marc is at 3pm today.", now=NOW + 5))

    def test_transcript_fragment_is_recognized(self) -> None:
        # Whisper chunks the played audio — a fragment must still match.
        voice.register_spoken(
            "I found three tasks in your notes. The first is the robotics "
            "market report, due Friday. Want me to schedule time for it?",
            now=NOW)
        self.assertTrue(voice.recently_spoken(
            "the first is the robotics market report due Friday", now=NOW + 20))

    def test_users_own_words_pass_through(self) -> None:
        voice.register_spoken("Your meeting with Marc is at 3pm today.", now=NOW)
        self.assertFalse(voice.recently_spoken(
            "remind me to call Julia about the beach house tomorrow",
            now=NOW + 5))

    def test_short_segments_never_attributed(self) -> None:
        voice.register_spoken("Okay, done. Anything else?", now=NOW)
        # 1-2 word segments are too thin to blame on TTS.
        self.assertFalse(voice.recently_spoken("okay", now=NOW + 2))

    def test_expiry_frees_old_utterances(self) -> None:
        voice.register_spoken("This is a fairly short reply for you.", now=NOW)
        # Same words much later (past TTL ≈ 30s + 90s grace) are the user's.
        self.assertFalse(voice.recently_spoken(
            "this is a fairly short reply for you", now=NOW + 600))

    def test_env_kill_switch(self) -> None:
        import os
        voice.register_spoken("Kill switch check sentence here.", now=NOW)
        os.environ["QUILL_TTS_ECHO_GUARD"] = "0"
        try:
            self.assertFalse(voice.recently_spoken(
                "kill switch check sentence here", now=NOW + 1))
        finally:
            del os.environ["QUILL_TTS_ECHO_GUARD"]

    def test_offer_boilerplate_without_registry(self) -> None:
        # Live failure: Whisper glued an offer; registry match alone was weak.
        blob = (
            "I noticed a to-do list, groceries with one item, one, buy milk "
            "reply yes to have me run the web doable ones, I'll pause for "
            "approval before anything irreversible, or no to skip."
        )
        self.assertTrue(voice.recently_spoken(blob, now=NOW + 1))

    def test_spoken_buried_in_longer_chunk(self) -> None:
        # Direction B: our full line is inside a longer Whisper window.
        voice.register_spoken(
            "I noticed a to-do list — Groceries — with 1 item:\n"
            "1. Buy milk\n\n"
            "Reply 'yes' to have me run the web-doable ones "
            "(I'll pause for approval before anything irreversible), "
            "or 'no' to skip.",
            now=NOW)
        chunk = (
            "do list, groceries with one item, one milk reply yes to have me "
            "run the web-doable ones, I'll pause for approval before anything "
            "irreversible, or no to skip."
        )
        self.assertTrue(voice.recently_spoken(chunk, now=NOW + 5))

    def test_calendar_offer_boilerplate(self) -> None:
        self.assertTrue(voice.recently_spoken(
            "Meeting with Kyshev and Ethan Saturday, August 1st, all day, "
            "at Dell Technologies Capital reply yes to add it, or no to skip.",
            now=NOW + 1))

    def test_real_user_yes_alone_still_passes(self) -> None:
        # A bare "yes" must not trip offer fingerprint (needs ≥2 markers).
        self.assertFalse(voice.recently_spoken("yes", now=NOW + 1))
        self.assertFalse(voice.recently_spoken(
            "yes please add milk to the list", now=NOW + 1))


class SelfWindowTests(unittest.TestCase):
    def test_own_ui_titles_are_self(self) -> None:
        for title in ("Sparrow — Chat - Google Chrome",
                      "Sparrow — Memory Console",
                      "Memory changes - Chromium",
                      "Weekly check-in",
                      "localhost:8000/console — Profile"):
            self.assertTrue(is_self_window(title), title)
            self.assertTrue(is_console_window(title), title)

    def test_real_user_windows_are_not_self(self) -> None:
        for title in ("Quarterly report.docx - Word",
                      "Marc - WhatsApp",
                      "console.cloud.google.com - Chrome",  # someone ELSE's console
                      "Robotics Market Analysis - Notion"):
            self.assertFalse(is_self_window(title), title)


class EntityGateTests(unittest.TestCase):
    def test_own_surface_names_never_entities(self) -> None:
        for name in ("Memory Console", "Desktop Access", "Weekly check-in"):
            self.assertFalse(nq.is_plausible_entity(name), name)
            self.assertFalse(nq.is_plausible_person(name), name)

    def test_possessive_ui_labels_rejected(self) -> None:
        self.assertFalse(nq.is_plausible_entity("My Contacts"))
        self.assertFalse(nq.is_plausible_person("My Contacts"))
        self.assertFalse(nq.is_plausible_entity("My Files"))

    def test_real_entities_still_pass(self) -> None:
        # External tools/projects the user genuinely discusses stay mintable —
        # the fix is not-watching-our-own-screen, not banning real things.
        self.assertTrue(nq.is_plausible_entity("Robotics Market"))
        self.assertTrue(nq.is_plausible_entity("Claude Code"))
        self.assertTrue(nq.is_plausible_person("Julia Beech"))


if __name__ == "__main__":
    unittest.main()
