"""Per-engine ingest thresholds, and the fit that produces them (§3.3).

The failure this whole mechanism exists to prevent is silent: point Whisper's
`avg_logprob` thresholds at an engine with a different confidence scale and
nothing errors — the filter just starts drawing the keep/drop line somewhere
else, and a memory product begins discarding real speech or storing ghosts with
no signal that anything changed.

So the tests care about two things above accuracy: an absent or broken
calibration must fall back to the shipped defaults (never to no filter), and the
fit must be checked against behaviour on labelled audio, not just against the
shape of a distribution.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app.config import settings
from app.services import asr_calibration as calib

import calibrate_asr_confidence as fitter  # noqa: E402


class _TmpData(unittest.TestCase):
    """Each test gets its own QUILL_DATA_DIR so the calibration file is real."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="quill_calib_")
        self.dir = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": str(self.dir)})
        self.env.start()
        calib.reset_cache()

    def tearDown(self) -> None:
        self.env.stop()
        calib.reset_cache()
        self.tmp.cleanup()

    def write_table(self, engines: dict) -> Path:
        p = self.dir / "asr_calibration.json"
        p.write_text(json.dumps({"version": 1, "engines": engines}),
                     encoding="utf-8")
        return p


class LookupTests(_TmpData):
    def test_an_uncalibrated_engine_gets_the_shipped_config_unchanged(self) -> None:
        """Whisper has no calibration and needs none — the defaults were written
        for its scale. Identity, not a copy, so nothing downstream can be
        confused about which object it holds."""
        self.assertIs(calib.cfg_for("whisper:small"), settings.ingest)
        self.assertIs(calib.cfg_for(None), settings.ingest)

    def test_a_calibrated_engine_is_judged_on_its_own_thresholds(self) -> None:
        self.write_table({"parakeet-onnx:v2": {
            "thresholds": {"min_avg_logprob": 0.7, "low_conf_logprob": 0.75}}})
        cfg = calib.cfg_for("parakeet-onnx:v2")
        self.assertEqual(cfg.min_avg_logprob, 0.7)
        self.assertEqual(cfg.low_conf_logprob, 0.75)
        # untouched fields keep the shipped values
        self.assertEqual(cfg.min_chars, settings.ingest.min_chars)
        self.assertEqual(cfg.enabled, settings.ingest.enabled)

    def test_a_calibration_cannot_turn_the_filter_off(self) -> None:
        """A threshold file is not a kill switch. `enabled` and the word/time
        fields are not on the engine's confidence scale, so they are not the
        calibration's business."""
        self.write_table({"e:1": {"thresholds": {
            "enabled": False, "dedup_window_s": 0, "min_chars": 999,
            "min_avg_logprob": 0.5}}})
        cfg = calib.cfg_for("e:1")
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.min_chars, settings.ingest.min_chars)
        self.assertEqual(cfg.dedup_window_s, settings.ingest.dedup_window_s)
        self.assertEqual(cfg.min_avg_logprob, 0.5)

    def test_family_fallback_when_the_exact_checkpoint_is_absent(self) -> None:
        self.write_table({"parakeet-onnx": {"thresholds": {"min_avg_logprob": 0.6}}})
        self.assertEqual(calib.cfg_for("parakeet-onnx:tdt-0.6b-v3")
                         .min_avg_logprob, 0.6)

    def test_an_exact_entry_wins_over_the_family(self) -> None:
        self.write_table({
            "parakeet-onnx": {"thresholds": {"min_avg_logprob": 0.6}},
            "parakeet-onnx:v2": {"thresholds": {"min_avg_logprob": 0.9}}})
        self.assertEqual(calib.cfg_for("parakeet-onnx:v2").min_avg_logprob, 0.9)

    def test_a_torn_file_falls_back_to_defaults_not_to_nothing(self) -> None:
        (self.dir / "asr_calibration.json").write_text('{"engines": {"e', "utf-8")
        self.assertIs(calib.cfg_for("e:1"), settings.ingest)

    def test_a_garbage_field_name_does_not_break_the_pipeline(self) -> None:
        self.write_table({"e:1": {"thresholds": {"min_avg_logprob": 0.5}}})
        with patch.object(calib, "CALIBRATABLE", ("min_avg_logprob", "nonsense")):
            self.write_table({"e:1": {"thresholds": {"nonsense": 1.0}}})
            self.assertIs(calib.cfg_for("e:1"), settings.ingest)

    def test_a_refit_takes_effect_without_a_restart(self) -> None:
        import time
        p = self.write_table({"e:1": {"thresholds": {"min_avg_logprob": 0.5}}})
        self.assertEqual(calib.cfg_for("e:1").min_avg_logprob, 0.5)
        time.sleep(0.01)
        p.write_text(json.dumps({"version": 1, "engines": {
            "e:1": {"thresholds": {"min_avg_logprob": 0.8}}}}), encoding="utf-8")
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
        self.assertEqual(calib.cfg_for("e:1").min_avg_logprob, 0.8)

    def test_describe_says_whether_transcripts_are_calibrated(self) -> None:
        self.assertFalse(calib.describe("whisper:small")["calibrated"])
        self.write_table({"e:1": {"thresholds": {"min_avg_logprob": 0.5},
                                  "n_utterances": 412}})
        d = calib.describe("e:1")
        self.assertTrue(d["calibrated"])
        self.assertEqual(d["n_utterances"], 412)


class QuantileMapTests(unittest.TestCase):
    def test_quantile_and_value_round_trip(self) -> None:
        vals = [float(i) for i in range(101)]
        for x in (0.0, 25.0, 50.0, 99.0, 100.0):
            q = fitter.quantile_of(vals, x)
            self.assertAlmostEqual(fitter.value_at(vals, q), x, places=6)

    def test_out_of_range_clamps_rather_than_extrapolating(self) -> None:
        """The fixtures contain no evidence about values beyond what was
        observed; inventing a slope out there would invent a threshold."""
        vals = [1.0, 2.0, 3.0]
        self.assertEqual(fitter.quantile_of(vals, -99.0), 0.0)
        self.assertEqual(fitter.quantile_of(vals, 99.0), 1.0)
        self.assertEqual(fitter.value_at(vals, -1.0), 1.0)
        self.assertEqual(fitter.value_at(vals, 2.0), 3.0)

    def test_it_recovers_a_known_monotone_transform(self) -> None:
        """The whole premise: if engine B's confidence is some monotone squash
        of engine A's, the quantile map finds it without being told."""
        def squash(x):
            return 0.55 + 0.40 * math.exp(x * 0.9)

        ref = [-0.05 * i for i in range(400)]           # 0 .. -19.95
        cand = [squash(x) for x in ref]
        ref_s, cand_s = sorted(ref), sorted(cand)
        for t in (-0.5, -0.7, -1.0):
            got, _q = fitter.map_threshold(ref_s, cand_s, t)
            self.assertAlmostEqual(got, squash(t), places=2)

    def test_an_empty_distribution_yields_no_threshold(self) -> None:
        self.assertIsNone(fitter.value_at([], 0.5))
        got, _q = fitter.map_threshold([], [], -1.0)
        self.assertIsNone(got)


class OrderingTests(unittest.TestCase):
    def test_a_valid_order_is_left_alone(self) -> None:
        out, notes = fitter._ordered({"phrase_avg_logprob": 0.8,
                                      "low_conf_logprob": 0.7,
                                      "min_avg_logprob": 0.6})
        self.assertEqual(out["low_conf_logprob"], 0.7)
        self.assertEqual(notes, [])

    def test_a_crossed_order_is_clamped_and_reported(self) -> None:
        """`low_conf` above `phrase` would let a denylisted ghost be flagged
        instead of dropped. Silently fixing it would hide that the fit was
        under-determined, so the correction is reported."""
        out, notes = fitter._ordered({"phrase_avg_logprob": 0.6,
                                      "low_conf_logprob": 0.9,
                                      "min_avg_logprob": 0.5})
        self.assertEqual(out["low_conf_logprob"], 0.6)
        self.assertTrue(notes)
        self.assertIn("under-determined", notes[0])


def _report(engine, rows, tag="t"):
    """Build a minimal eval_asr report from (text, conf, nsp, is_speech) rows."""
    clips = []
    for i, (text, conf, nsp, speech) in enumerate(rows):
        clips.append({"id": f"c{i}", "expect_speech": speech,
                      "category": "close_mic" if speech else "no_speech",
                      "segments": [{"text": text, "avg_confidence": conf,
                                    "no_speech_prob": nsp,
                                    "confidence_kind": "test",
                                    "asr_ms": 1.0, "audio_ms": 1000.0,
                                    "offline_utterance_ms": 1.0,
                                    "kept": True, "action": "keep"}]})
    return {"tag": tag, "config": {"engine_id": engine}, "per_clip": clips}


class OperatingPointTests(unittest.TestCase):
    """Replaying the REAL filter, not a reimplementation of its rules."""

    def test_confident_speech_is_kept_and_weak_ghosts_are_dropped(self) -> None:
        rows = fitter.utterances(_report("e:1", [
            ("the review moved to thursday", -0.2, 0.05, True),
            ("send abby the deck tonight", -0.25, 0.05, True),
            ("Thank you.", -1.2, 0.8, False),
            ("Thanks for watching!", -1.3, 0.85, False),
        ]))
        op = fitter.operating_point(rows, settings.ingest)
        self.assertEqual(op["false_drop_rate"], 0.0)
        self.assertEqual(op["hallucination_drop_rate"], 1.0)

    def test_an_engine_on_the_wrong_scale_stops_rejecting_ghosts(self) -> None:
        """The exact silent failure: positive-valued confidences never fall
        below a threshold written in negative log-probabilities, so rule 4 can
        never fire and the ghosts sail through."""
        rows = fitter.utterances(_report("e:2", [
            ("Thank you.", 0.62, None, False),
            ("Thanks for watching!", 0.60, None, False),
        ]))
        op = fitter.operating_point(rows, settings.ingest)
        self.assertEqual(op["hallucination_drop_rate"], 0.0)

    def test_empty_text_utterances_are_not_scored(self) -> None:
        rows = fitter.utterances(_report("e:1", [("", -0.2, 0.1, True)]))
        self.assertEqual(rows, [])


class FitTests(_TmpData):
    def _pair(self):
        """A reference on Whisper's scale and a candidate on a squashed one,
        over the same fixture population."""
        def squash(x):
            return round(0.55 + 0.40 * math.exp(x * 0.9), 4)

        speech = [(-0.20 - 0.004 * i) for i in range(60)]
        ghosts = [(-0.95 - 0.004 * i) for i in range(30)]
        ref_rows = ([("the review moved to thursday", c, 0.05, True)
                     for c in speech]
                    + [("Thank you.", c, 0.75, False) for c in ghosts])
        cand_rows = ([("the review moved to thursday", squash(c), None, True)
                      for c in speech]
                     + [("Thank you.", squash(c), None, False) for c in ghosts])
        return _report("whisper:small", ref_rows), _report("parakeet:v2", cand_rows)

    def test_the_fit_restores_hallucination_rejection(self) -> None:
        ref, cand = self._pair()
        out = fitter.fit(ref, cand)
        op = out["operating_point"]
        self.assertEqual(op["candidate_uncalibrated"]["hallucination_drop_rate"],
                         0.0)
        self.assertGreater(op["candidate_calibrated"]["hallucination_drop_rate"],
                           0.9)

    def test_it_does_not_start_dropping_real_speech(self) -> None:
        ref, cand = self._pair()
        op = fitter.fit(ref, cand)["operating_point"]
        self.assertLessEqual(op["candidate_calibrated"]["false_drop_rate"],
                             op["reference"]["false_drop_rate"] + 0.02)

    def test_thresholds_stay_ordered(self) -> None:
        t = fitter.fit(*self._pair())["thresholds"]
        self.assertGreaterEqual(t["phrase_avg_logprob"], t["low_conf_logprob"])
        self.assertGreaterEqual(t["low_conf_logprob"], t["min_avg_logprob"])

    def test_a_missing_no_speech_signal_is_called_out(self) -> None:
        """Losing that signal disables two of the filter's six rules. It is a
        behaviour change whether or not the thresholds are right."""
        notes = " ".join(fitter.fit(*self._pair())["notes"])
        self.assertIn("no_speech_prob", notes)

    def test_calibrating_an_engine_against_itself_is_refused(self) -> None:
        ref, _cand = self._pair()
        with self.assertRaises(SystemExit) as ctx:
            fitter.fit(ref, ref)
        self.assertIn("against itself", str(ctx.exception))

    def test_false_drop_matching_only_loosens(self) -> None:
        """Its job is repairing the deletion-dangerous direction. Letting it
        tighten too would fit the engine to this corpus's hard utterances."""
        ref, cand = self._pair()
        quant = fitter.fit(ref, cand, match="quantile")
        matched = fitter.fit(ref, cand, match="false-drop")
        self.assertLessEqual(matched["thresholds"]["min_avg_logprob"],
                             quant["thresholds"]["min_avg_logprob"])

    def test_it_says_when_there_was_nothing_to_repair(self) -> None:
        notes = " ".join(fitter.fit(*self._pair(), match="false-drop")["notes"])
        self.assertIn("already at or below", notes)

    def test_write_merges_rather_than_replacing_other_engines(self) -> None:
        self.write_table({"other:1": {"thresholds": {"min_avg_logprob": 0.1}}})
        fitter.write(fitter.fit(*self._pair()))
        table = calib.load(force=True)
        self.assertIn("other:1", table)
        self.assertIn("parakeet:v2", table)

    def test_a_written_fit_is_what_the_pipeline_then_uses(self) -> None:
        """The round trip that matters: fit -> file -> the config `assess()` is
        actually called with on the live path."""
        result = fitter.write(fitter.fit(*self._pair()))
        cfg = calib.cfg_for("parakeet:v2")
        self.assertIsNot(cfg, settings.ingest)
        self.assertGreater(cfg.min_avg_logprob, 0.0)


if __name__ == "__main__":
    unittest.main()
