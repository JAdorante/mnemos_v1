"""Cross-source echo dedupe: mic copies of loopback audio (and vice versa)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.services import echo_dedup


def _check(text, source, now, **kw):
    kw.setdefault("window_s", 10.0)
    kw.setdefault("threshold", 0.8)
    return echo_dedup.check_and_register(text, source, now=now, **kw)


class EchoDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        echo_dedup.clear()

    def test_mic_echo_of_system_dropped(self) -> None:
        self.assertIsNone(_check(
            "Welcome back to the channel, today we cover neoclouds.",
            "audio.system", now=100.0))
        got = _check("welcome back to the channel today we cover neoclouds",
                     "audio", now=103.0)
        self.assertEqual(got, "system")

    def test_first_wins_is_symmetric(self) -> None:
        self.assertIsNone(_check("the quarterly numbers look strong",
                                 "audio", now=50.0))
        self.assertEqual(_check("The quarterly numbers look strong.",
                                "audio.system", now=52.0), "mic")

    def test_different_content_both_kept(self) -> None:
        self.assertIsNone(_check("Welcome back to the channel everyone",
                                 "audio.system", now=10.0))
        self.assertIsNone(_check("Remind me to email Patrick after this video",
                                 "audio", now=12.0))

    def test_fragment_containment_matches(self) -> None:
        self.assertIsNone(_check(
            "Thank you for watching, have a good day, see you next time.",
            "audio.system", now=10.0))
        # Mic VAD split the same audio into a shorter fragment.
        self.assertEqual(_check("have a good day see you next time",
                                "audio", now=13.5), "system")

    def test_short_phrases_need_exact_match(self) -> None:
        self.assertIsNone(_check("okay", "audio.system", now=10.0))
        # Short + not identical: kept (no fuzzy matching under 12 chars).
        self.assertIsNone(_check("okay then", "audio", now=11.0))

    def test_window_expiry(self) -> None:
        self.assertIsNone(_check("this phrase repeats much later",
                                 "audio.system", now=10.0))
        self.assertIsNone(_check("this phrase repeats much later",
                                 "audio", now=25.0))   # 15s later — outside 10s

    def test_same_group_never_matches(self) -> None:
        # Mic-vs-mic duplicates are the ingest filter's job, not ours.
        self.assertIsNone(_check("same words twice in a row", "audio", now=10.0))
        self.assertIsNone(_check("same words twice in a row", "audio", now=11.0))

    def test_enabled_follows_system_audio(self) -> None:
        on = SimpleNamespace(system_audio=SimpleNamespace(
            enabled=True, echo_dedup=True))
        off = SimpleNamespace(system_audio=SimpleNamespace(
            enabled=False, echo_dedup=True))
        veto = SimpleNamespace(system_audio=SimpleNamespace(
            enabled=True, echo_dedup=False))
        with mock.patch.object(echo_dedup, "settings", on):
            self.assertTrue(echo_dedup.enabled())
        with mock.patch.object(echo_dedup, "settings", off):
            self.assertFalse(echo_dedup.enabled())
        with mock.patch.object(echo_dedup, "settings", veto):
            self.assertFalse(echo_dedup.enabled())


if __name__ == "__main__":
    unittest.main()
