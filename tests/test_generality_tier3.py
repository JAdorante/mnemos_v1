"""Tier 3 (Track B) — data-calibration generality.

B2: the speaker acoustic constants (environment cutoffs, threshold clamps, online
    adaptation bounds, embedder id, per-profile deltas) are env/JSON-overridable,
    so the same code adapts to any mic/rooms.
B4: the machine-specific audio floors auto-calibrate from THIS machine's own audio
    into calibration.json, with precedence env var > calibration.json > literal and
    a fail-safe cold start.
"""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


class B2SpeakerEnvTests(unittest.TestCase):
    def _fresh_speakers(self):
        import app.config as C
        import app.services.speakers as S
        S.settings = C.Settings()          # rebuild from current env
        return S

    def test_classify_environment_uses_config_cutoffs(self) -> None:
        S = self._fresh_speakers()
        # default noisy cutoff is 8 dB
        self.assertEqual(
            S.classify_environment({"snr_est": 6.0, "rms": 0.05, "clipping_pct": 0}),
            "noisy_room")
        self.assertEqual(
            S.classify_environment({"snr_est": 30.0, "rms": 0.10, "clipping_pct": 0}),
            "close_mic")
        self.assertEqual(S.classify_environment(None), "unknown_env")

    def test_profile_adjust_overlay(self) -> None:
        import app.services.speakers as S
        d = Path(tempfile.mkdtemp())
        f = d / "adj.json"
        f.write_text(json.dumps({"noisy_room": [-0.10, -0.08, 0.10]}))
        os.environ["QUILL_SPK_PROFILE_ADJUST"] = str(f)
        try:
            importlib.reload(S)
            self.assertEqual(S._PROFILE_ADJUST["noisy_room"], (-0.10, -0.08, 0.10))
            self.assertEqual(S._PROFILE_ADJUST["close_mic"], (0.04, 0.03, 0.0))
            f.write_text("{ bad json")           # malformed -> fail safe
            importlib.reload(S)
            self.assertEqual(S._PROFILE_ADJUST["noisy_room"], (-0.06, -0.04, 0.06))
        finally:
            os.environ.pop("QUILL_SPK_PROFILE_ADJUST", None)
            importlib.reload(S)

    def test_identifier_reads_config_clamps_and_model(self) -> None:
        S = self._fresh_speakers()
        spk = S.SpeakerIdentifier(voiceprint_dir=tempfile.mkdtemp())
        self.assertEqual(spk._id_clamp, (0.25, 0.80))
        self.assertEqual(spk._cluster_clamp, (0.20, 0.70))
        self.assertEqual(spk._adapt["min_n"], 8)
        self.assertTrue(spk.model_id)          # embedder id present + swappable


class B4CalibrationTests(unittest.TestCase):
    def test_cal_helper_import_safe_and_precedence(self) -> None:
        from app.services import calibration as cal
        d = Path(tempfile.mkdtemp())
        os.environ["QUILL_DATA_DIR"] = str(d)
        cal.invalidate()
        try:
            # no file -> default returned
            self.assertEqual(cal.cal("audio_quality.bad_snr_db", 3.0), 3.0)
            # file present -> value returned
            (d / "calibration.json").write_text(json.dumps(
                {"audio_quality": {"bad_snr_db": 4.5}}))
            cal.invalidate()
            self.assertEqual(cal.cal("audio_quality.bad_snr_db", 3.0), 4.5)
            self.assertEqual(cal.cal("missing.key", 9), 9)
        finally:
            os.environ.pop("QUILL_DATA_DIR", None)
            cal.invalidate()

    def test_autocalibrate_declines_without_enough_audio(self) -> None:
        from app.services import calibration as cal
        d = Path(tempfile.mkdtemp())
        os.environ["QUILL_DATA_DIR"] = str(d)
        cal.invalidate()
        try:
            self.assertIsNone(cal.maybe_autocalibrate())
            self.assertFalse(cal.calibration_path().is_file())
        finally:
            os.environ.pop("QUILL_DATA_DIR", None)
            cal.invalidate()

    def test_autocalibrate_disabled_flag(self) -> None:
        from app.services import calibration as cal
        d = Path(tempfile.mkdtemp())
        os.environ["QUILL_DATA_DIR"] = str(d)
        os.environ["QUILL_AUTO_CALIBRATE"] = "0"
        cal.invalidate()
        try:
            self.assertIsNone(cal.maybe_autocalibrate())
        finally:
            os.environ.pop("QUILL_DATA_DIR", None)
            os.environ.pop("QUILL_AUTO_CALIBRATE", None)
            cal.invalidate()

    def test_derive_clamps_to_safe_bounds(self) -> None:
        """A derived value can never escape the safe band around the default —
        the cheap always-available half of the eval gate."""
        import scripts.calibrate_audio as CA
        import numpy as np
        # feed an extreme distribution and confirm outputs stay bounded
        real = CA._score_distribution
        CA._score_distribution = lambda limit: (
            np.full(100, 0.5), np.full(100, 99.0))   # absurdly loud + high SNR
        try:
            from app.config import settings
            p = CA.derive_calibration(limit=None, source="test")
            aq = settings.audio_quality
            self.assertLessEqual(p["audio_quality"]["silence_rms"], aq.silence_rms * 3.0)
            self.assertLessEqual(p["audio_quality"]["bad_snr_db"], aq.bad_snr_db + 3.0)
            self.assertLessEqual(p["audio_quality"]["noisy_snr_db"], aq.noisy_snr_db + 5.0)
            # ordering invariant preserved
            self.assertLess(p["audio_quality"]["bad_snr_db"],
                            p["audio_quality"]["noisy_snr_db"])
        finally:
            CA._score_distribution = real


if __name__ == "__main__":
    unittest.main()
