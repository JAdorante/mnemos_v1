"""Tests for the idle-footprint harness (scripts/bench_listen_idle.py).

The number this script produces becomes Phase B's acceptance baseline — "idle
footprint ≤ half of baseline" is meaningless if the baseline is wrong or if two
runs measured different things. So the tests cover the ways it could quietly lie:
a CPU share computed against the wrong denominator, and a comparison between
runs paced differently, which shows a large improvement that is entirely an
artefact of the harness.

No model, no microphone. The VAD itself is stubbed — its cost is what the script
measures, not what the script is.
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bench_listen_idle as bench  # noqa: E402


class SignalTests(unittest.TestCase):
    def test_silence_is_near_but_not_digital_zero(self) -> None:
        """A zero vector is not audio any microphone produces, and measuring
        against it invites an optimisation that special-cases it."""
        x = bench.silence(16000, np.random.default_rng(1))
        self.assertEqual(x.dtype, np.float32)
        rms = float(np.sqrt(np.mean(x * x)))
        self.assertGreater(rms, 0.0)
        self.assertLess(rms, 0.005)

    def test_speech_like_is_loud_enough_to_wake_a_gate(self) -> None:
        x = bench.speech_like(16000, np.random.default_rng(1))
        quiet = bench.silence(16000, np.random.default_rng(1))
        self.assertGreater(float(np.sqrt(np.mean(x * x))),
                           20 * float(np.sqrt(np.mean(quiet * quiet))))

    def test_signals_are_deterministic_for_a_given_seed(self) -> None:
        a = bench.silence(800, np.random.default_rng(7))
        b = bench.silence(800, np.random.default_rng(7))
        self.assertTrue(np.array_equal(a, b))


class _FakeVadIterator:
    """Costs a fixed, known amount of wall time per frame."""

    def __init__(self, *a, per_frame_s=0.001, fire_on=(), **kw):
        self.per_frame_s = per_frame_s
        self.fire_on = set(fire_on)
        self.calls = 0

    def __call__(self, chunk, return_seconds=False):
        import time
        self.calls += 1
        t = time.perf_counter()
        while time.perf_counter() - t < self.per_frame_s:
            pass                                    # burn, don't sleep
        return {"start": 0} if self.calls in self.fire_on else None

    def reset_states(self):
        return None


class VadMeasurementTests(unittest.TestCase):
    def _patch(self, **kw):
        fake = _FakeVadIterator(**kw)
        mod = mock.MagicMock()
        mod.load_silero_vad.return_value = object()
        mod.VADIterator.side_effect = lambda *a, **k: fake
        return mock.patch.dict(sys.modules, {"silero_vad": mod}), fake

    def test_cpu_share_is_vad_time_over_audio_time(self) -> None:
        """The headline number. 1 ms of work per 32 ms frame is 1/32 of one
        core — the fraction of real time the CPU must spend to keep up with the
        microphone, which is the only form that says whether the fan comes on.
        """
        patch, _fake = self._patch(per_frame_s=0.001)
        with patch:
            out = bench.measure_vad(1.0, realtime=False, with_speech=False)
        self.assertAlmostEqual(out["vad_cpu_share"], 1.0 / 32.0, delta=0.01)

    def test_it_reports_how_many_frames_it_actually_pushed(self) -> None:
        patch, fake = self._patch(per_frame_s=0.0)
        with patch:
            out = bench.measure_vad(1.0, realtime=False, with_speech=False)
        self.assertEqual(out["frames"], fake.calls)
        # audio_s is what was actually pushed, not what was asked for: a whole
        # number of 32 ms frames, so it lands just under the request. The share
        # is divided by this, not by the request, or a partial final frame would
        # skew every reported cost.
        frame_s = 512 / bench.SR
        self.assertAlmostEqual(out["audio_s"], out["frames"] * frame_s, places=2)
        self.assertLessEqual(1.0 - out["audio_s"], frame_s)

    def test_vad_events_are_counted(self) -> None:
        patch, _fake = self._patch(per_frame_s=0.0, fire_on=(3, 9))
        with patch:
            out = bench.measure_vad(1.0, realtime=False, with_speech=True)
        self.assertEqual(out["vad_events"], 2)

    def test_the_pacing_mode_is_recorded_in_the_result(self) -> None:
        """Without it a report cannot tell whether it is comparable to another,
        and the CPU numbers differ several-fold between the two modes."""
        patch, _fake = self._patch(per_frame_s=0.0)
        with patch:
            fast = bench.measure_vad(0.5, realtime=False, with_speech=False)
        self.assertIs(fast["realtime"], False)


class PacingGuardTests(unittest.TestCase):
    """A `--fast` run must never be diffed against a paced baseline."""

    @staticmethod
    def _res(realtime, share):
        return {"config": {"asr_engine": "whisper", "whisper_model": "small",
                           "compute_type": "int8", "device": "cpu",
                           "frame_ms": 32, "vad_threshold": 0.5,
                           "min_silence_ms": 500},
                "vad": {"realtime": realtime, "audio_s": 10.0, "wall_s": 10.0,
                        "vad_events": 2, "rss_delta_mb": 40.0,
                        "per_frame_ms": {"mean": 0.4, "p50": 0.4, "p95": 0.6},
                        "process_cpu_share": 0.02},
                "engine": {"skipped": True},
                "always_on": {"cpu_share_of_one_core": share, "rss_mb": None,
                              "engine_rss_mb": None}}

    def _render(self, res, baseline):
        buf = io.StringIO()
        with redirect_stdout(buf):
            bench.print_report(res, baseline)
        return buf.getvalue()

    def test_cross_paced_cpu_comparison_is_refused(self) -> None:
        out = self._render(self._res(False, 0.003), self._res(True, 0.015))
        self.assertIn("not compared: different pacing", out)
        self.assertNotIn("80.0%", out)

    def test_same_paced_comparison_is_shown(self) -> None:
        out = self._render(self._res(True, 0.0073), self._res(True, 0.0146))
        self.assertIn("vs baseline", out)
        self.assertNotIn("not compared", out)

    def test_a_fast_run_says_it_understates(self) -> None:
        out = self._render(self._res(False, 0.003), None)
        self.assertIn("--fast", out)
        self.assertIn("understates", out)

    def test_a_paced_run_carries_no_warning(self) -> None:
        out = self._render(self._res(True, 0.014), None)
        self.assertNotIn("understates", out)

    def test_process_cpu_is_hidden_when_racing(self) -> None:
        """Whole-process CPU means nothing when the harness is spinning through
        frames as fast as it can; printing it invites it to be quoted."""
        self.assertNotIn("process cpu", self._render(self._res(False, 0.003), None))
        self.assertIn("process cpu", self._render(self._res(True, 0.014), None))


class ReportShapeTests(unittest.TestCase):
    def test_always_on_separates_compute_from_memory(self) -> None:
        """Phase B can win on memory alone — a resident 120M model instead of a
        0.6B one — while leaving VAD's cost untouched. A single blended
        'footprint' number could not show that."""
        with mock.patch.object(bench, "measure_vad",
                               return_value={"realtime": True, "audio_s": 1.0,
                                             "wall_s": 1.0, "frames": 31,
                                             "vad_events": 0, "load_ms": 1.0,
                                             "rss_delta_mb": 40.0,
                                             "per_frame_ms": {"mean": .4, "p50": .4,
                                                              "p95": .5},
                                             "vad_cpu_share": 0.0125,
                                             "process_cpu_share": 0.02}):
            res = bench.run(1.0, engine=None, realtime=True, with_speech=False,
                            skip_engine=True)
        self.assertEqual(res["always_on"]["cpu_share_of_one_core"], 0.0125)
        self.assertIn("rss_mb", res["always_on"])
        self.assertTrue(res["engine"]["skipped"])

    def test_an_engine_that_fails_to_load_is_reported_not_raised(self) -> None:
        with mock.patch("app.services.asr.make_engine",
                        side_effect=RuntimeError("no weights")):
            out = bench.measure_engine("parakeet-onnx")
        self.assertIn("no weights", out["error"])


if __name__ == "__main__":
    unittest.main()
