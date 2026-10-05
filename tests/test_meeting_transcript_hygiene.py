"""Meeting transcripts: repetition loops never become memory or prompt.

On the 2026-09 pilot half of every meeting transcript was a Whisper loop
("nd nd nd ..."). Loops decode with HIGH confidence, so the logprob floor
passed them; each kept loop then went into the next clip's initial_prompt,
and that context outlived the evening. These pin the three breaks in that
chain: the shape check, the clean-only context, and the reset at meetings.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import asr
from app.services import audio as audio_mod
from app.services.ingest_filter import assess, degenerate_reason
from app.services.vocabulary import SessionContext

_LOOP = " ".join(["nd"] * 120)


class DegenerateReasonTests(unittest.TestCase):
    def test_token_loop(self):
        self.assertIsNotNone(degenerate_reason(_LOOP))

    def test_phrase_loop(self):
        self.assertIsNotNone(degenerate_reason(
            "there's no doubt that " * 6))

    def test_underscores(self):
        self.assertEqual(degenerate_reason("______ ____"), "underscores")

    def test_segment_compression_ratio(self):
        seg = SimpleNamespace(compression_ratio=3.1)
        self.assertIn("compression_ratio",
                      degenerate_reason("a short line", [seg]))

    def test_impossible_speaking_rate(self):
        """A full sentence out of a 0.74 s clip was not said in that clip."""
        why = degenerate_reason("be back with more information on the project.",
                                duration_s=0.8)
        self.assertTrue(why.startswith("chars_per_s"))

    def test_real_speech_with_repeats_survives(self):
        text = ("I really want a dog. Oh, I think shelter is about four. "
                "Oh, no, no, no,")
        self.assertIsNone(degenerate_reason(text, duration_s=11.2))

    def test_plain_speech_survives(self):
        self.assertIsNone(degenerate_reason(
            "the design review moved to thursday", duration_s=2.5))


class AssessTests(unittest.TestCase):
    def test_confident_loop_is_audio_only(self):
        seg = SimpleNamespace(avg_logprob=-0.08, no_speech_prob=0.1)
        v = assess(_LOOP, [seg], duration_s=1.5)
        self.assertEqual(v.action, "store_audio_only")
        self.assertTrue(v.reason.startswith("degenerate:"))

    def test_clean_line_still_kept(self):
        seg = SimpleNamespace(avg_logprob=-0.3, no_speech_prob=0.05)
        v = assess("the design review moved to thursday", [seg], duration_s=2.5)
        self.assertEqual(v.action, "keep")

    def test_duration_is_optional(self):
        seg = SimpleNamespace(avg_logprob=-0.3, no_speech_prob=0.05)
        self.assertEqual(assess("we ship on friday", [seg]).action, "keep")


class SessionContextTests(unittest.TestCase):
    def test_stale_context_is_forgotten(self):
        ctx = SessionContext(maxlen=4, max_age_s=60)
        ctx.add("hello there")
        self.assertEqual(ctx.recent(), ["hello there"])
        with patch("app.services.vocabulary.time.time",
                   return_value=time.time() + 61):
            self.assertEqual(ctx.recent(), [])

    def test_reset_shared_context(self):
        shared = audio_mod._get_shared_session()
        shared.add("left over from yesterday")
        audio_mod.reset_shared_context()
        self.assertEqual(shared.recent(), [])


class _Engine:
    engine_id = "stub:v1"
    model_id = "stub"
    supports_context = True
    confidence_kind = asr.AVG_LOGPROB

    def __init__(self, text):
        self.text = text

    def transcribe(self, samples, sample_rate, context=None):
        return asr.ASRResult(
            text=self.text, avg_confidence=-0.08,
            confidence_kind=self.confidence_kind, engine_id=self.engine_id,
            segments=[SimpleNamespace(avg_logprob=-0.08, no_speech_prob=0.1)])


class PipelineContextTests(unittest.TestCase):
    def _run(self, text):
        from tests.test_asr_engine_seam import _LoopHarness

        h = _LoopHarness(_Engine(text))
        h.pipeline._session = SessionContext(maxlen=4)
        events = h.run(timeout=2.0)
        return events, h.pipeline._session.recent()

    def test_loop_is_kept_out_of_memory_and_prompt(self):
        events, ctx = self._run(_LOOP)
        self.assertEqual([e.raw for e in events], [""])   # audio-only event
        self.assertTrue(events[0].meta["skipped"].startswith("degenerate:"))
        self.assertEqual(ctx, [])

    def test_clean_line_feeds_context(self):
        events, ctx = self._run("the design review moved to thursday")
        self.assertEqual(events[0].raw, "the design review moved to thursday")
        self.assertEqual(ctx, ["the design review moved to thursday"])


class MeetingBoundaryTests(unittest.TestCase):
    def setUp(self):
        from app.services import meeting_session as ms
        from app.storage import Store

        ms.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db",
                           audio_dir=Path(self.tmp.name) / "audio")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        from app.services import meeting_session as ms
        ms.reset()
        self.store.close()
        self.tmp.cleanup()

    def test_start_and_end_clear_asr_context(self):
        from app.services import meeting_session as ms

        shared = audio_mod._get_shared_session()
        shared.add("nd nd nd")
        ms.start_manual(title="Standup", store=self.store)
        self.assertEqual(shared.recent(), [])
        shared.add("something said in the meeting")
        with patch.object(ms, "kick_note_pipeline", lambda sid: {}):
            ms.end(store=self.store)
        self.assertEqual(shared.recent(), [])


class TemperatureLadderTests(unittest.TestCase):
    def test_greedy_gets_fallback_ladder(self):
        self.assertEqual(asr._temperatures(0.0), asr.FALLBACK_TEMPERATURES)
        self.assertEqual(asr.FALLBACK_TEMPERATURES[0], 0.0)

    def test_explicit_temperature_is_kept(self):
        self.assertEqual(asr._temperatures(0.3), 0.3)


if __name__ == "__main__":
    unittest.main()
