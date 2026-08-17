"""Workstream 1 — meeting-first capture gate and first-win / unlock."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import first_run, meeting_session as ms
from app.storage import Store


class FirstRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_fr_"))
        self.env = patch.dict(os.environ, {
            "QUILL_DATA_DIR": str(self.tmp),
            "QUILL_FIRST_RUN_MODE": "meeting",
            "QUILL_MEETING_PAD_MIN": "5",
            "QUILL_UNLOCK_AFTER_BRIEFS": "3",
        }, clear=False)
        self.env.start()
        first_run._cached = None
        ms.reset()

    def tearDown(self) -> None:
        self.env.stop()
        first_run._cached = None
        ms.reset()

    def test_mode_default_meeting(self) -> None:
        self.assertEqual(first_run.mode(), "meeting")
        self.assertTrue(first_run.is_meeting_first())

    def test_continuous_mic_off_until_opt_in(self) -> None:
        self.assertFalse(first_run.allows_continuous("mic"))
        first_run.set_ambient_opt_in({"mic": True}, persist_consent=False)
        # Still false: capture_consent was not granted.
        self.assertFalse(first_run.allows_continuous("mic"))

    def test_audio_blocked_outside_window_without_listen_consent(self) -> None:
        self.assertFalse(first_run.audio_event_allowed("audio.whisper", now=time.time()))

    def test_audio_allowed_inside_padded_calendar_window(self) -> None:
        first_run.save({"meeting_listen_consent": True})
        store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        now = 1_700_000_000.0
        store.upsert_calendar_event(
            event_id="cal-1", calendar="test", uid="cal-1",
            title="Standup", start=now, end=now + 1800, all_day=False)
        self.assertTrue(first_run.in_meeting_window(now + 60, store=store))
        # 5 min pad after end
        self.assertTrue(first_run.in_meeting_window(now + 1800 + 200, store=store))
        self.assertFalse(first_run.in_meeting_window(now + 1800 + 400, store=store))
        self.assertTrue(first_run.audio_event_allowed(
            "audio.whisper", now=now + 60, store=store))

    def test_should_ingest_drops_mic_without_listen_consent(self) -> None:
        now = time.time()
        self.assertFalse(ms.should_ingest("audio.whisper", now=now))

    def test_unlock_card_after_n_briefs_is_ui_only(self) -> None:
        self.assertIsNone(first_run.unlock_card())
        for i in range(3):
            first_run.note_brief_ready(i + 1, has_facts=True, href=f"/meetings/{i+1}")
        card = first_run.unlock_card()
        self.assertIsNotNone(card)
        self.assertTrue(card["show"])
        first_run.mark_unlock_shown()
        self.assertIsNone(first_run.unlock_card())

    def test_first_win_never_blank_preference(self) -> None:
        first_run.note_brief_ready(9, has_facts=False, href="/meetings/9")
        pend = first_run.consume_first_win()
        self.assertEqual(pend["href"], "/meetings/9")
        self.assertFalse(pend["has_facts"])
        self.assertIsNone(first_run.consume_first_win())

    def test_ambient_opt_in_does_not_silently_enable_without_save(self) -> None:
        # persist_consent True still requires explicit True values from the card.
        first_run.set_ambient_opt_in({"mic": False, "webcam": False, "desktop": False})
        self.assertFalse(first_run.load()["ambient"]["mic"])


class TesterProfileTests(unittest.TestCase):
    def test_tester_pins_meeting_first(self) -> None:
        with patch.dict(os.environ, {"QUILL_PROFILE": "tester"}, clear=False):
            os.environ.pop("QUILL_FIRST_RUN_MODE", None)
            os.environ.pop("QUILL_PHONE_LINK", None)
            os.environ.pop("QUILL_ANTICIPATE", None)
            os.environ.pop("QUILL_DESKTOP_CAPTURE", None)
            os.environ.pop("QUILL_PHONE_WATCH", None)
            from app.config import apply_tester_profile
            apply_tester_profile()
            self.assertEqual(os.environ.get("QUILL_FIRST_RUN_MODE"), "meeting")
            self.assertEqual(os.environ.get("QUILL_PHONE_LINK"), "0")
            self.assertEqual(os.environ.get("QUILL_ANTICIPATE"), "0")
            self.assertEqual(os.environ.get("QUILL_DESKTOP_CAPTURE"), "0")
            self.assertEqual(os.environ.get("QUILL_PHONE_WATCH"), "0")


class FirstRunCompatTests(unittest.TestCase):
    def test_unset_mode_is_full(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUILL_FIRST_RUN_MODE", None)
            self.assertEqual(first_run.mode(), "full")
            self.assertFalse(first_run.is_meeting_first())


if __name__ == "__main__":
    unittest.main()
