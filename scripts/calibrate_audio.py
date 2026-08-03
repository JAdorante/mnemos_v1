"""#B4 — Auto-calibrate audio thresholds from THIS machine's own utterances.

The audio_quality floors (silence RMS, bad/noisy SNR) and the speaker-env cutoffs
were originally hand-tuned to one developer's 1400 utterances. This turns that into
"auto-calibrates from the first N utterances on any machine": it reads the real
WAVs the app already saved under $QUILL_DATA_DIR/audio, scores them with the same
audio_quality.score() the pipeline uses, and derives the thresholds from PERCENTILES
of the user's own distribution — then writes $QUILL_DATA_DIR/calibration.json, which
config.py layers UNDER any explicit env var and OVER the shipped literal.

    python scripts/calibrate_audio.py                 # derive + write calibration.json
    python scripts/calibrate_audio.py --dry-run       # print derived values, don't write
    python scripts/calibrate_audio.py --limit 500     # cap how many clips are scored
    python scripts/calibrate_audio.py --force         # overwrite an existing calibration.json

Derivation (percentiles of the machine's own clips):
    silence_rms   ~ p5  of rms      (only the quietest few % count as near-silence)
    bad_snr_db    ~ p10 of snr      (the worst-SNR tail is "unusable")
    noisy_snr_db  ~ p30 of snr      ("degraded but usable" band)
    speaker-env noisy/farfield SNR + close/farfield RMS = quantile splits
    min_duration_ms stays PHYSICAL (a code literal, not calibrated)

SAFETY (the always-available half of the eval gate): every derived value is clamped
to a bounded, sane range around the shipped default, so a weird distribution can
NEVER produce a threshold that would start dropping real speech. The AUTHORITATIVE
adoption check for a manual run is still the voice eval — run it before trusting a
calibration on a new mic:

    python scripts/eval_voice.py run --tag baseline -o baseline.json
    python scripts/calibrate_audio.py
    python scripts/eval_voice.py run --tag calibrated -o calibrated.json
    python scripts/eval_voice.py compare baseline.json calibrated.json   # must not be worse
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.config import settings                              # noqa: E402
from app.services.audio_quality import score as aq_score     # noqa: E402
from app.services import calibration as _calib               # noqa: E402


def _audio_dir() -> Path:
    # Read the data dir the SAME way calibration.py does (live env), so count_clips
    # and calibration_path stay consistent even if QUILL_DATA_DIR changed post-import.
    return Path(_calib._data_dir()) / "audio"


def count_clips() -> int:
    d = _audio_dir()
    try:
        return sum(1 for _ in d.glob("*.wav"))
    except Exception:
        return 0


def _read_wav(path: Path):
    import wave
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return x, sr


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(lo if x < lo else hi if x > hi else x)


def _score_distribution(limit: int | None):
    """Score up to `limit` clips (oldest-first) → arrays of rms and snr."""
    files = sorted(_audio_dir().glob("*.wav"))
    if limit:
        files = files[:limit]
    rms, snr = [], []
    for f in files:
        try:
            x, sr = _read_wav(f)
            q = aq_score(x, sr)
        except Exception:
            continue
        rms.append(float(q.get("rms", 0.0)))
        snr.append(float(q.get("snr_est", 0.0)))
    return np.asarray(rms, dtype=np.float64), np.asarray(snr, dtype=np.float64)


def derive_calibration(*, limit: int | None = None, source: str = "manual") -> dict | None:
    """Derive a calibration payload from the machine's own audio, or None if there
    isn't enough to be trustworthy. Every value is clamped to a safe band around
    the shipped default (the cheap safety gate)."""
    rms, snr = _score_distribution(limit)
    n = int(rms.size)
    if n < 20:                       # far too few to trust any percentile
        return None

    aq = settings.audio_quality
    env = settings.speaker_env

    # --- audio_quality floors: percentiles of the user's own distribution ---
    # silence_rms sits just under the quietest clips; clamped so it can never rise
    # far enough to start dropping real quiet-but-clean speech (the documented risk).
    silence_rms = _clamp(float(np.percentile(rms, 5)) * 0.8,
                         aq.silence_rms * 0.25, aq.silence_rms * 3.0)
    bad_snr_db = _clamp(float(np.percentile(snr, 10)),
                        aq.bad_snr_db - 3.0, aq.bad_snr_db + 3.0)
    noisy_snr_db = _clamp(float(np.percentile(snr, 30)),
                          aq.noisy_snr_db - 5.0, aq.noisy_snr_db + 5.0)
    # keep the ordering sane: bad < noisy
    if bad_snr_db >= noisy_snr_db:
        bad_snr_db = noisy_snr_db - 1.0

    # --- speaker-env cutoffs: quantile splits of the same distributions -----
    env_noisy_snr = _clamp(float(np.percentile(snr, 25)),
                           env.noisy_snr - 4.0, env.noisy_snr + 4.0)
    env_farfield_snr = _clamp(float(np.percentile(snr, 60)),
                              max(env_noisy_snr + 1.0, env.farfield_snr - 5.0),
                              env.farfield_snr + 5.0)
    env_close_rms = _clamp(float(np.percentile(rms, 75)),
                           env.close_rms * 0.5, env.close_rms * 2.0)
    env_farfield_rms = _clamp(float(np.percentile(rms, 30)),
                              env.farfield_rms * 0.5,
                              min(env_close_rms * 0.9, env.farfield_rms * 2.0))

    return {
        "source": source,
        "n_clips": n,
        "audio_quality": {
            "silence_rms": round(silence_rms, 6),
            "bad_snr_db": round(bad_snr_db, 2),
            "noisy_snr_db": round(noisy_snr_db, 2),
        },
        "speaker_env": {
            "noisy_snr": round(env_noisy_snr, 2),
            "farfield_snr": round(env_farfield_snr, 2),
            "close_rms": round(env_close_rms, 4),
            "farfield_rms": round(env_farfield_rms, 4),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-calibrate audio thresholds from stored utterances")
    ap.add_argument("--limit", type=int, default=None, help="max clips to score")
    ap.add_argument("--dry-run", action="store_true", help="print derived values, don't write")
    ap.add_argument("--force", action="store_true", help="overwrite an existing calibration.json")
    args = ap.parse_args()

    n = count_clips()
    print(f"[calibrate] {n} clips in {_audio_dir()}")
    if n == 0:
        print("[calibrate] no audio yet — nothing to calibrate. Shipped defaults stay in effect.")
        return 1

    path = _calib.calibration_path()
    if path.is_file() and not args.force and not args.dry_run:
        print(f"[calibrate] {path} already exists — use --force to overwrite "
              "(delete it after a mic change and re-run).")
        return 1

    payload = derive_calibration(limit=args.limit, source="manual")
    if not payload:
        print("[calibrate] too few scorable clips to derive trustworthy thresholds.")
        return 1

    import json
    print(json.dumps(payload, indent=2))
    if args.dry_run:
        print("[calibrate] --dry-run: not written.")
        return 0
    written = _calib.write_calibration(payload)
    print(f"[calibrate] wrote {written}")
    print("[calibrate] Precedence: explicit QUILL_AQ_*/QUILL_SPK_ENV_* env var > "
          "this file > shipped literal.")
    print("[calibrate] Validate on a new mic with: python scripts/eval_voice.py compare "
          "baseline.json calibrated.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
