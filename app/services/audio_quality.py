"""Pre-ASR audio quality scoring — judge the utterance *before* Whisper.

The ingest filter (ingest_filter.py) judges the *transcript* after Whisper. This
module judges the *audio* first, from the raw utterance waveform alone — pure
numpy, no model, no network, sub-millisecond. It gives the rest of Mnemos a
reason to trust, flag, or skip a transcript, and — crucially for debugging — lets
us tell "the audio was bad" apart from "Whisper failed".

    q = score(utterance, sample_rate)
    event.meta["audio_quality"] = q
    # {"duration_ms": 1840, "rms": 0.034, "snr_est": 14.2,
    #  "vad_speech_ratio": 0.81, "clipping_pct": 0.0, "quality": "good", ...}

It is the routing signal the rest of the roadmap keys off:
  * #2 denoise-only-when-weak  -> route on `quality == "noisy"`
  * #7 structured ingest verdict -> fold these signals in
  * #9 telemetry / Audio Health -> chart snr / clipping / low-quality rate

Signals (all derived from the waveform, so they're independent of the VAD/ASR):
  duration_ms           utterance length
  rms / peak            overall loudness and headroom (full-scale float audio)
  clipping_pct          % of samples pinned at the clip ceiling (distortion)
  snr_est               speech-frame power vs noise-frame power, in dB
  vad_speech_ratio      fraction of frames that read as speech (energy gate)
  speech_silence_ratio  speech frames / silence frames
  vad_flips             speech<->silence transitions (chattery = noisy/fragmented)
  abrupt_start/_end     boundary frame already hot -> a word was likely clipped
  quality               good | noisy | bad   (single label the pipeline routes on)

Design notes
------------
* Speech detection is a hybrid energy gate: a frame is "speech" when it sits both
  `speech_margin_db` above the utterance's own noise floor AND within
  `speech_range_db` of its loudest frame. The floor term catches near-silence
  (VAD over-segmentation, padding); the peak term keeps *continuous* speech from
  being scored as silence just because it lacks quiet reference frames.
* SNR is speech-power / noise-power in dB when both classes exist; when the
  utterance is all-speech or all-silence we fall back to the 90th/10th-percentile
  energy spread, which stays in the same dB units.
* Everything is best-effort and pure: a degenerate input returns a `bad` verdict
  with a reason rather than raising, so the capture loop never breaks on it.
"""
from __future__ import annotations

import math

import numpy as np

from app.config import settings

_EPS = 1e-10


def _empty(duration_ms: float, reason: str) -> dict:
    return {
        "duration_ms": int(round(duration_ms)),
        "rms": 0.0, "peak": 0.0, "clipping_pct": 0.0, "snr_est": 0.0,
        "vad_speech_ratio": 0.0, "speech_silence_ratio": 0.0,
        "vad_flips": 0, "abrupt_start": False, "abrupt_end": False,
        "quality": "bad", "reasons": [reason],
    }


def score(utterance, sample_rate: int, cfg=None) -> dict:
    """Score one utterance waveform. `utterance` is a mono float32 vector in
    [-1, 1] (what the capture loop already holds). Returns the audio_quality dict;
    never raises on bad input — returns a `bad` verdict with a reason instead."""
    cfg = cfg or settings.audio_quality
    x = np.asarray(utterance, dtype=np.float32).reshape(-1)
    n = int(x.size)
    sr = int(sample_rate) or 1
    duration_ms = 1000.0 * n / sr
    if n == 0:
        return _empty(0.0, "empty")

    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x * x)))
    clipping_pct = 100.0 * float(np.mean(np.abs(x) >= cfg.clip_ceiling))

    # --- frame the signal and take per-frame power (in dB) ------------------
    fs = max(1, int(sr * cfg.frame_ms / 1000))
    n_frames = max(1, n // fs)
    frames = x[: n_frames * fs].reshape(n_frames, fs)
    frame_pow = np.mean(frames * frames, axis=1)            # linear power
    frame_db = 10.0 * np.log10(frame_pow + _EPS)

    noise_floor_db = float(np.percentile(frame_db, 10))     # quietest frames
    speech_ref_db = float(np.percentile(frame_db, 90))      # loudest frames
    peak_db = float(np.max(frame_db))

    # Hybrid speech gate: above the noise floor *and* near the peak (see notes).
    thr_db = max(noise_floor_db + cfg.speech_margin_db, peak_db - cfg.speech_range_db)
    speech_mask = frame_db > thr_db
    speech_frames = int(np.count_nonzero(speech_mask))
    silence_frames = n_frames - speech_frames

    vad_speech_ratio = speech_frames / n_frames
    speech_silence_ratio = (speech_frames / silence_frames
                            if silence_frames else float(speech_frames))

    if speech_frames and silence_frames:
        speech_pow = float(np.mean(frame_pow[speech_mask]))
        noise_pow = float(np.mean(frame_pow[~speech_mask]))
        snr_est = 10.0 * math.log10((speech_pow + _EPS) / (noise_pow + _EPS))
    else:
        # All-speech or all-silence: no clean noise reference -> use the spread.
        snr_est = speech_ref_db - noise_floor_db

    vad_flips = int(np.count_nonzero(np.diff(speech_mask.astype(np.int8)) != 0))
    abrupt_start = bool(speech_mask[0])
    abrupt_end = bool(speech_mask[-1])

    out = {
        "duration_ms": int(round(duration_ms)),
        "rms": round(rms, 4),
        "peak": round(peak, 4),
        "clipping_pct": round(clipping_pct, 2),
        "snr_est": round(float(snr_est), 1),
        "vad_speech_ratio": round(vad_speech_ratio, 2),
        "speech_silence_ratio": round(float(speech_silence_ratio), 2),
        "vad_flips": vad_flips,
        "abrupt_start": abrupt_start,
        "abrupt_end": abrupt_end,
    }
    out["quality"], out["reasons"] = _classify(out, cfg)
    return out


def _classify(m: dict, cfg) -> tuple[str, list[str]]:
    """Fold the signals into good | noisy | bad + human-readable reasons.

    `bad`  -> the audio can't be trusted (too short/quiet/noisy/clipped). A
              downstream router (#2) may skip it or store it audio-only.
    `noisy`-> intelligible but degraded; a candidate for denoise/enhancement.
    `good` -> send straight to Whisper.
    """
    reasons: list[str] = []

    # --- bad = unusable (skip-ASR / audio-only candidate) ------------------
    # Loudness alone is NOT here: real quiet-but-clean speech transcribes fine
    # (verified on 1400 past utterances). Only genuine unusability qualifies.
    if m["duration_ms"] < cfg.min_duration_ms:
        reasons.append(f"too_short({m['duration_ms']}ms)")
    if m["snr_est"] < cfg.bad_snr_db:
        reasons.append(f"low_snr({m['snr_est']}dB)")
    if m["clipping_pct"] > cfg.bad_clipping_pct:
        reasons.append(f"heavy_clipping({m['clipping_pct']}%)")
    if (m["rms"] < cfg.silence_rms
            and m["vad_speech_ratio"] < cfg.silence_speech_ratio):
        reasons.append(f"near_silence(rms={m['rms']},speech={m['vad_speech_ratio']})")
    if reasons:
        return "bad", reasons

    # --- noisy = degraded but usable (a denoise/enhance candidate, #2) ------
    if m["snr_est"] < cfg.noisy_snr_db:
        reasons.append(f"snr({m['snr_est']}dB)")
    if m["clipping_pct"] > cfg.noisy_clipping_pct:
        reasons.append(f"clipping({m['clipping_pct']}%)")
    if m["vad_speech_ratio"] < cfg.noisy_speech_ratio:
        reasons.append(f"mostly_silence({m['vad_speech_ratio']})")
    # vad_flips is intentionally NOT a trigger — flip count tracks utterance
    # length and syllable rate, so it flags normal speech. It's kept in the
    # output for telemetry / future rate-normalized fragmentation heuristics.
    if reasons:
        return "noisy", reasons

    return "good", []
