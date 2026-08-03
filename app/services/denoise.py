"""Speech enhancement — denoise an utterance, but only when it needs it (#2).

Routed by the pre-ASR audio_quality score: `good` audio goes to Whisper
untouched (denoising adds latency and can distort clean speech), `noisy` audio is
enhanced first, `bad` audio is skipped upstream. This lifts far-field / laptop-mic
performance without bloating every transcription path.

    mic -> VAD -> utterance -> audio_quality -> [enhance if noisy] -> Whisper

Backends, best-first (all lazy; degrade cleanly):
  * DeepFilterNet (`df`)  — learned real-time denoiser, best quality (optional dep)
  * spectral gate         — built-in numpy spectral subtraction, ALWAYS available

`enhance()` is best-effort: on any failure or when disabled it returns the
ORIGINAL audio with applied=False, so a bad denoise never blocks ASR. It never
mutates its input — the caller keeps the raw clip for provenance and transcribes
the returned (possibly enhanced) copy.
"""
from __future__ import annotations

import numpy as np

from app.config import settings

# DeepFilterNet is heavy + stateful; cache the model and give up permanently
# after the first failed import so we don't retry it on every utterance.
_df = None            # (model, state) once initialized
_df_failed = False


def enhance(audio, sample_rate: int, cfg=None,
            for_asr: bool = True) -> tuple[np.ndarray, dict]:
    """Return (audio_out, info). `info` = {applied, backend, note}. On disabled /
    too-short / failure, returns the original audio with applied=False.

    `for_asr` (default True): when the output feeds Whisper, the built-in numpy
    spectral gate is skipped unless explicitly enabled (it lifts SNR but hurts
    ASR — see DenoiseConfig.spectral_asr). Pass for_asr=False to always run the
    best available backend (e.g. to produce an enhanced provenance clip)."""
    cfg = cfg or settings.denoise
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    info = {"applied": False, "backend": "none", "note": ""}
    if not cfg.enabled or x.size < 320:      # < 20 ms: nothing to gain
        return x, info

    order = ([cfg.backend] if cfg.backend in ("deepfilternet", "spectral")
             else ["deepfilternet", "spectral"])
    # For the ASR path, drop the spectral gate unless opted in — proven to hurt
    # Whisper. A learned backend (DeepFilterNet) is always allowed.
    allow_spectral = (not for_asr) or cfg.spectral_asr or cfg.backend == "spectral"
    if not allow_spectral:
        order = [b for b in order if b != "spectral"]
    if not order:
        info["note"] = "spectral_disabled_for_asr"
        return x, info
    for name in order:
        try:
            y = (_deepfilternet(x, sample_rate) if name == "deepfilternet"
                 else _spectral_gate(x, sample_rate, cfg))
            if y is None:                    # backend unavailable -> try next
                continue
            y = np.clip(np.asarray(y, dtype=np.float32).reshape(-1), -1.0, 1.0)
            info.update(applied=True, backend=name)
            return y, info
        except Exception as exc:             # never let denoise break capture
            info["note"] = f"{name}: {exc}"
            continue
    return x, info


# ---------------------------------------------------------------------------
# Built-in denoiser (numpy only): a decision-directed Wiener filter.
#
# Naive spectral subtraction raises the SNR *metric* but injects "musical noise"
# — random isolated bins that survive the subtraction — which is out-of-
# distribution for Whisper and measurably HURTS transcription (verified: word
# overlap dropped ~0.68 -> 0.44). The Ephraim-Malah decision-directed a-priori
# SNR estimate is the standard cure: it smooths the gain across frames so the
# residual sounds like natural attenuated noise, not chirps. We use it with a
# Wiener gain and a conservative gain floor, so clean speech is barely touched
# and noisy speech is gently, artifact-free-ly cleaned.
# ---------------------------------------------------------------------------
def _spectral_gate(x, sr: int, cfg) -> np.ndarray:
    nfft = 512 if sr >= 16000 else 256
    hop = nfft // 4                          # 75% overlap
    win = np.hanning(nfft).astype(np.float32)

    pad = (-(len(x) - nfft)) % hop if len(x) > nfft else nfft - len(x)
    xp = np.concatenate([x, np.zeros(pad, dtype=np.float32)]) if pad > 0 else x
    starts = list(range(0, len(xp) - nfft + 1, hop))
    S = np.stack([np.fft.rfft(xp[s:s + nfft] * win) for s in starts])  # (T, F)
    if S.shape[0] < 2:
        return x                             # too short to estimate noise

    power = np.abs(S) ** 2                    # (T, F)
    # Per-bin noise power = quiet-frame percentile (stationary floor), mildly
    # over-estimated so we lean toward leaving speech intact.
    noise = np.maximum(
        np.percentile(power, cfg.noise_percentile, axis=0) * cfg.over_subtraction,
        1e-12)

    alpha = cfg.dd_alpha
    floor = cfg.spectral_floor               # gain floor (min attenuation)
    G = np.empty_like(power)
    g_prev = None
    gamma_prev = None
    for t in range(power.shape[0]):
        gamma = power[t] / noise             # a-posteriori SNR
        if g_prev is None:
            xi = alpha + (1.0 - alpha) * np.maximum(gamma - 1.0, 0.0)
        else:                                # decision-directed a-priori SNR
            xi = (alpha * (g_prev ** 2) * gamma_prev
                  + (1.0 - alpha) * np.maximum(gamma - 1.0, 0.0))
        g = np.maximum(xi / (xi + 1.0), floor)   # Wiener gain, floored
        G[t] = g
        g_prev, gamma_prev = g, gamma
    S2 = G * S

    out = np.zeros((S.shape[0] - 1) * hop + nfft, dtype=np.float32)
    wsum = np.zeros_like(out)
    w2 = win ** 2
    for i, s in enumerate(starts):
        out[s:s + nfft] += np.fft.irfft(S2[i], n=nfft).astype(np.float32) * win
        wsum[s:s + nfft] += w2
    out = out / np.maximum(wsum, 1e-8)
    return out[:len(x)]


# ---------------------------------------------------------------------------
# DeepFilterNet backend (optional). Lazy + heavily guarded: if `df` isn't
# installed or anything goes wrong, returns None so enhance() falls back.
# DF runs at 48 kHz, so we resample in/out with torchaudio when needed.
# ---------------------------------------------------------------------------
def _deepfilternet(x, sr: int):
    global _df, _df_failed
    if _df_failed:
        return None
    try:
        import torch
        from df.enhance import enhance as df_enhance, init_df
    except Exception:
        _df_failed = True                    # not installed — stop trying
        return None
    try:
        import torchaudio
        if _df is None:
            model, state, _ = init_df()
            _df = (model, state)
        model, state = _df
        df_sr = state.sr()                   # 48000
        t = torch.from_numpy(np.asarray(x, dtype=np.float32)).unsqueeze(0)
        if sr != df_sr:
            t = torchaudio.functional.resample(t, sr, df_sr)
        y = df_enhance(model, state, t)
        if sr != df_sr:
            y = torchaudio.functional.resample(y, df_sr, sr)
        return y.squeeze(0).detach().cpu().numpy()
    except Exception:
        # A runtime failure (bad model download, shape) — fall back this call but
        # keep the backend eligible; it may be a transient/one-off.
        return None
