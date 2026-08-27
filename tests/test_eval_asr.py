"""The ASR acceptance harness's own scorers (perception Phase 0).

A harness that decides an engine swap has to be trusted more than the engines it
scores. These tests cover the parts that can be wrong *quietly*: the boundary
and attribution scorers (a bug there would recommend Sortformer for a problem
that isn't segmentation), the manifest validator (its whole job is catching a
fixture with no ground truth before it scores as perfect), and the aggregation
of the two hallucination rates. No model, no audio device, no network.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import eval_asr  # noqa: E402


def _seg(start, end, label=None, text="x", kept=True):
    return {"start": start, "end": end, "label": label, "text": text,
            "kept": kept, "asr_ms": 10.0, "audio_ms": (end - start) * 1000,
            "offline_utterance_ms": 12.0}


def _utt(start, end, speaker):
    return {"start": start, "end": end, "speaker": speaker, "text": "x"}


class BoundaryMetricsTests(unittest.TestCase):
    def test_clean_one_to_one_segmentation(self) -> None:
        truth = [_utt(0.0, 2.0, "A"), _utt(3.0, 5.0, "B")]
        pred = [_seg(0.0, 2.0), _seg(3.0, 5.0)]
        m = eval_asr.boundary_metrics(pred, truth)
        self.assertEqual(m["fused_rate"], 0.0)
        self.assertEqual(m["split_rate"], 0.0)
        self.assertEqual(m["missed_rate"], 0.0)
        self.assertEqual(m["start_mae_ms"], 0.0)

    def test_fused_segment_spanning_two_speakers(self) -> None:
        """The Phase C measurement: one VAD segment covering two people. It
        yields ONE embedding for two voices, so no speaker stack can attribute
        it correctly — which is why this is measured before touching one."""
        truth = [_utt(0.0, 2.0, "A"), _utt(2.1, 4.0, "B")]
        pred = [_seg(0.0, 4.0)]
        m = eval_asr.boundary_metrics(pred, truth)
        self.assertEqual(m["fused_rate"], 1.0)
        self.assertEqual(m["n_pred"], 1)

    def test_brief_overlap_does_not_count_as_fused(self) -> None:
        # 50 ms of tail overlap is VAD padding, not a fused turn.
        truth = [_utt(0.0, 2.0, "A"), _utt(1.95, 4.0, "B")]
        pred = [_seg(0.0, 2.0)]
        self.assertEqual(eval_asr.boundary_metrics(pred, truth)["fused_rate"], 0.0)

    def test_split_and_missed(self) -> None:
        truth = [_utt(0.0, 4.0, "A"), _utt(6.0, 8.0, "B")]
        pred = [_seg(0.0, 1.8), _seg(2.2, 4.0)]      # A cut in two, B missed
        m = eval_asr.boundary_metrics(pred, truth)
        self.assertEqual(m["split_rate"], 0.5)
        self.assertEqual(m["missed_rate"], 0.5)

    def test_no_truth_yields_no_metrics(self) -> None:
        self.assertEqual(eval_asr.boundary_metrics([_seg(0, 1)], []), {})


class AttributionTests(unittest.TestCase):
    def test_anonymous_labels_map_to_speakers(self) -> None:
        """speakers.py returns "Speaker 1"/"Speaker 2" with nothing enrolled, so
        consistent-but-anonymous labelling must score as correct."""
        truth = [_utt(0, 1, "Justin"), _utt(2, 3, "Abby"), _utt(4, 5, "Justin")]
        pred = [_seg(0, 1, "Speaker 1"), _seg(2, 3, "Speaker 2"),
                _seg(4, 5, "Speaker 1")]
        m = eval_asr.attribution_error(pred, truth)
        self.assertEqual(m["attribution_error_rate"], 0.0)
        self.assertEqual(m["n_labels"], 2)
        self.assertEqual(m["n_speakers"], 2)

    def test_two_speakers_collapsed_into_one_cluster(self) -> None:
        truth = [_utt(0, 1, "Justin"), _utt(2, 3, "Abby"),
                 _utt(4, 5, "Justin"), _utt(6, 7, "Abby")]
        pred = [_seg(0, 1, "Speaker 1"), _seg(2, 3, "Speaker 1"),
                _seg(4, 5, "Speaker 1"), _seg(6, 7, "Speaker 1")]
        m = eval_asr.attribution_error(pred, truth)
        # Greedy mapping gives Speaker 1 to whichever speaker it covers most;
        # the other speaker's turns are all wrong. 2 of 4 here.
        self.assertEqual(m["wrong_speaker_rate"], 0.5)

    def test_unlabeled_truth_counts_as_error(self) -> None:
        truth = [_utt(0, 1, "Justin"), _utt(9, 10, "Abby")]
        pred = [_seg(0, 1, "Speaker 1")]             # second turn never segmented
        m = eval_asr.attribution_error(pred, truth)
        self.assertEqual(m["unlabeled_rate"], 0.5)
        self.assertEqual(m["attribution_error_rate"], 0.5)

    def test_mapping_is_one_to_one(self) -> None:
        """Two clusters must not both map onto the same speaker — that would let
        a system that splits one person into five voices score perfectly."""
        truth = [_utt(0, 1, "Justin"), _utt(2, 3, "Justin"), _utt(4, 5, "Abby")]
        pred = [_seg(0, 1, "Speaker 1"), _seg(2, 3, "Speaker 2"),
                _seg(4, 5, "Speaker 3")]
        m = eval_asr.attribution_error(pred, truth)
        self.assertGreater(m["attribution_error_rate"], 0.0)


class ManifestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "clips").mkdir()
        self._wav(self.base / "clips" / "a.wav")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _wav(path: Path) -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
            wf.writeframes(np.zeros(1600, dtype="<i2").tobytes())

    def _row(self, **over):
        row = {"id": "a", "audio": "clips/a.wav", "category": "close_mic",
               "channel": "mic", "reference": "hello there"}
        row.update(over)
        return row

    def test_valid_row_has_no_problems(self) -> None:
        self.assertEqual(eval_asr.validate_manifest([self._row()], self.base), [])

    def test_speech_clip_without_reference_is_caught(self) -> None:
        # The failure this exists to prevent: an empty reference scores WER 0.0
        # against an empty hypothesis and looks like a perfect clip.
        problems = eval_asr.validate_manifest([self._row(reference="")], self.base)
        self.assertTrue(any("no 'reference'" in p for p in problems))

    def test_no_speech_clip_with_reference_is_caught(self) -> None:
        problems = eval_asr.validate_manifest(
            [self._row(category="no_speech", reference="oops")], self.base)
        self.assertTrue(any("empty 'reference'" in p for p in problems))

    def test_missing_audio_and_bad_category(self) -> None:
        problems = eval_asr.validate_manifest(
            [self._row(audio="clips/nope.wav", category="podcast")], self.base)
        self.assertTrue(any("audio not found" in p for p in problems))
        self.assertTrue(any("category" in p for p in problems))

    def test_duplicate_ids(self) -> None:
        problems = eval_asr.validate_manifest([self._row(), self._row()], self.base)
        self.assertTrue(any("duplicate id" in p for p in problems))

    def test_backwards_utterance_span(self) -> None:
        problems = eval_asr.validate_manifest(
            [self._row(utterances=[{"start": 4.0, "end": 1.0, "speaker": "A"}])],
            self.base)
        self.assertTrue(any("end <= start" in p for p in problems))


class AggregateTests(unittest.TestCase):
    def test_raw_and_post_filter_hallucination_are_separate(self) -> None:
        """The gap between them is how much work ingest_filter is doing; an
        aggregation that collapses them hides the thing a new engine must fix."""
        per = [
            {"id": "p1", "category": "no_speech", "expect_speech": False,
             "raw_hallucinated": True, "post_filter_hallucinated": False,
             "segments": [_seg(0, 1)]},
            {"id": "p2", "category": "no_speech", "expect_speech": False,
             "raw_hallucinated": True, "post_filter_hallucinated": True,
             "segments": [_seg(0, 1)]},
        ]
        agg = eval_asr.aggregate(per)
        self.assertEqual(agg["raw_hallucination_rate"], 1.0)
        self.assertEqual(agg["post_filter_hallucination_rate"], 0.5)

    def test_rtf_is_total_asr_over_total_audio(self) -> None:
        per = [{"id": "c", "category": "close_mic", "expect_speech": True,
                "wer": 0.1, "segments": [
                    {"asr_ms": 500.0, "audio_ms": 1000.0,
                     "offline_utterance_ms": 520.0},
                    {"asr_ms": 1500.0, "audio_ms": 1000.0,
                     "offline_utterance_ms": 1520.0}]}]
        agg = eval_asr.aggregate(per)
        self.assertEqual(agg["rtf"], 1.0)
        self.assertEqual(agg["asr_ms_p50"], 500.0)

    def test_errored_clips_are_excluded_not_scored(self) -> None:
        per = [{"id": "bad", "error": "sample rate 44100 != 16000"},
               {"id": "ok", "category": "close_mic", "expect_speech": True,
                "wer": 0.2, "segments": []}]
        agg = eval_asr.aggregate(per)
        self.assertEqual(agg["n_clips"], 1)
        self.assertEqual(agg["wer"], 0.2)


class EngineRegistryTests(unittest.TestCase):
    def test_unknown_engine_names_the_alternatives(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            eval_asr.get_engine("parakeet-onnx")
        self.assertIn("whisper", str(ctx.exception))

    def test_result_defaults_are_safe_for_ingest_filter(self) -> None:
        """An engine with no per-segment confidence must still work: assess()
        falls back to text-only checks rather than crashing."""
        from app.services.ingest_filter import assess

        res = eval_asr.ASRResult(text="the report is due friday", engine_id="stub")
        verdict = assess(res.text, res.segments)
        self.assertIn(verdict.action,
                      ("keep", "keep_low_confidence", "needs_user_review",
                       "store_audio_only", "drop_hallucination"))


class EvaluatorPipelineTests(unittest.TestCase):
    """The clip loop with a stub engine — no model, no audio hardware."""

    class _StubEngine:
        engine_id = "stub:test"

        def __init__(self, text="the quarterly report is due on friday"):
            self.text = text
            self.calls = []

        def transcribe(self, samples, sample_rate, context=None):
            self.calls.append((len(samples), context))
            return eval_asr.ASRResult(text=self.text, avg_confidence=-0.2,
                                      engine_id=self.engine_id)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "clips").mkdir()
        rng = np.random.default_rng(7)
        eval_asr.write_wav(self.base / "clips" / "a.wav",
                           (0.05 * rng.standard_normal(16000 * 2)).astype(np.float32),
                           16000)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scores_a_clip_without_vad(self) -> None:
        ev = eval_asr.Evaluator(self._StubEngine())
        row = {"id": "a", "audio": "clips/a.wav", "category": "close_mic",
               "channel": "mic",
               "reference": "the quarterly report is due on friday"}
        out = ev.clip(row, base=self.base, use_vad=False)
        self.assertEqual(out["wer"], 0.0)
        self.assertEqual(out["n_segments"], 1)
        self.assertTrue(out["segments"][0]["kept"])

    def test_wrong_sample_rate_errors_rather_than_scoring(self) -> None:
        with wave.open(str(self.base / "clips" / "b.wav"), "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(44100)
            wf.writeframes(np.zeros(44100, dtype="<i2").tobytes())
        ev = eval_asr.Evaluator(self._StubEngine())
        out = ev.clip({"id": "b", "audio": "clips/b.wav", "category": "close_mic",
                       "reference": "x"}, base=self.base, use_vad=False)
        self.assertIn("error", out)
        self.assertIn("44100", out["error"])

    def test_session_bias_feeds_context_forward(self) -> None:
        engine = self._StubEngine()
        ev = eval_asr.Evaluator(engine, bias="session")
        row = {"id": "a", "audio": "clips/a.wav", "category": "close_mic",
               "reference": "the quarterly report is due on friday"}
        ev.clip(row, base=self.base, use_vad=False)
        self.assertIsNone(engine.calls[0][1])   # nothing to bias with yet

    def test_no_bias_by_default(self) -> None:
        engine = self._StubEngine()
        eval_asr.Evaluator(engine).clip(
            {"id": "a", "audio": "clips/a.wav", "category": "close_mic",
             "reference": "x"}, base=self.base, use_vad=False)
        self.assertIsNone(engine.calls[0][1])


class SmokeTests(unittest.TestCase):
    """The CI gate: no-speech audio must not become a memory.

    Its value is entirely in the failing case — a gate that cannot fail is a
    green light with no wiring behind it — so that is tested first."""

    class _Ghost:
        engine_id = "ghost:test"
        model_id = "ghost"
        supports_context = False
        confidence_kind = "avg_logprob"

        def __init__(self, text="Thanks for watching!", conf=-1.2):
            self.text, self.conf = text, conf

        def transcribe(self, samples, sample_rate, context=None):
            return eval_asr.ASRResult(text=self.text, avg_confidence=self.conf,
                                      engine_id=self.engine_id)

    def setUp(self) -> None:
        from app.services import asr

        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "clips").mkdir()
        rng = np.random.default_rng(5)
        eval_asr.write_wav(self.base / "clips" / "sil.wav",
                           (0.0005 * rng.standard_normal(16000)).astype(np.float32),
                           16000)
        (self.base / "manifest.jsonl").write_text(json.dumps({
            "id": "sil", "audio": "clips/sil.wav", "category": "no_speech",
            "channel": "mic", "expect_speech": False, "reference": ""}) + "\n",
            encoding="utf-8")
        self.patches = [
            patch.object(eval_asr, "FIXTURE_DIR", self.base),
            patch.object(eval_asr, "MANIFEST", self.base / "manifest.jsonl"),
        ]
        for pt in self.patches:
            pt.start()
        self.asr = asr

    def tearDown(self) -> None:
        for pt in self.patches:
            pt.stop()
        self.asr.ENGINES.pop("ghost", None)
        self.asr.reset_shared()
        self._tmp.cleanup()

    def _register(self, factory):
        self.asr.register("ghost", factory)

    def test_it_fails_when_a_ghost_survives_the_filter(self) -> None:
        # A confident ghost: the denylist phrase is there, but the confidence
        # signals say "trust it", so the filter keeps it — exactly the shape of
        # a confidence-scale mismatch.
        self._register(lambda **kw: self._Ghost(
            text="the quarterly numbers look wrong", conf=-0.1))
        self.assertEqual(eval_asr.smoke("ghost"), 1)

    def test_it_passes_when_the_filter_refuses_the_ghost(self) -> None:
        self._register(lambda **kw: self._Ghost())
        self.assertEqual(eval_asr.smoke("ghost"), 0)

    def test_silence_yielding_no_text_passes(self) -> None:
        self._register(lambda **kw: self._Ghost(text=""))
        self.assertEqual(eval_asr.smoke("ghost"), 0)

    def test_no_probes_in_the_manifest_is_an_error_not_a_pass(self) -> None:
        """A green gate over an empty fixture set is worse than no gate."""
        (self.base / "manifest.jsonl").write_text(json.dumps({
            "id": "sp", "audio": "clips/sil.wav", "category": "close_mic",
            "expect_speech": True, "reference": "hello"}) + "\n",
            encoding="utf-8")
        self._register(lambda **kw: self._Ghost())
        self.assertEqual(eval_asr.smoke("ghost"), 2)

    def test_a_broken_clip_is_an_error_not_a_pass(self) -> None:
        import wave
        with wave.open(str(self.base / "clips" / "sil.wav"), "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(44100)
            wf.writeframes(np.zeros(44100, dtype="<i2").tobytes())
        self._register(lambda **kw: self._Ghost())
        self.assertEqual(eval_asr.smoke("ghost"), 2)


class ManifestRoundTripTests(unittest.TestCase):
    def test_comments_and_blank_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "manifest.jsonl"
            p.write_text('# a comment\n\n{"id": "a"}\n', encoding="utf-8")
            self.assertEqual(eval_asr.load_manifest(p), [{"id": "a"}])

    def test_bad_json_names_the_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "manifest.jsonl"
            p.write_text('{"id": "a"}\n{oops\n', encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                eval_asr.load_manifest(p)
            self.assertIn("line 2", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
