"""Pre-VLM frame quality scoring — judge the FRAME before the vision model.

The vision loop's only guard today is a brightness floor: it skips near-black
frames. But the failure mode that actually burns money on this machine is the
opposite — a bright, uniform GREEN placeholder frame (a Windows pixel-format /
driver glitch) sails past a brightness gate and gets a full VLM call describing
nothing. This module is the vision twin of `audio_quality.py`: pure numpy, no
model, sub-millisecond, judging the raw pixels so the pipeline can tell "the
camera is broken" from "the model failed" and stop paying to describe garbage.

    q = score(frame_bgr)            # frame_bgr: HxWx3 uint8 (what OpenCV delivers)
    if not q["analyzable"]:         # dead/placeholder -> skip the VLM, flag the camera
        ...
    event.meta["frame_quality"] = q
    # {"brightness": 133.2, "detail_std": 1.1, "dominant": "green",
    #  "channel_dominance": 0.71, "sharpness": 3.0, "quality": "dead",
    #  "capture_quality": 0.08, "reasons": ["uniform", "green_cast"], "analyzable": False}

Signals (all from the pixels, so independent of the VLM):
  brightness          mean luminance 0-255 (dark lens / unlit room)
  detail_std          std-dev of luminance — near 0 == a flat/uniform frame
  channel_dominance   max channel share of total — a strong single-color cast
  dominant            which channel dominates (for the human-readable reason)
  sharpness           gradient-magnitude variance — low == blurry / out of focus
  quality             good | degraded | dead   (the label the pipeline routes on)
  capture_quality     0..1 fidelity facet for the #3 confidence contract
  analyzable          quality != dead  (whether it's worth a VLM call at all)

`dead` frames (uniform, near-black, or a low-detail single-color cast) are the
camera-broken class — never sent to the VLM. `degraded` (dim or soft-focus but
with real structure) still goes, but with a low capture_quality so downstream
trust is scaled accordingly. Never raises: a bad input returns a `dead` verdict.
"""
from __future__ import annotations

import os

import numpy as np

# Thresholds — env-overridable so they can be tuned per camera without code edits.
_VAR_MIN = float(os.environ.get("QUILL_FRAME_VAR_MIN", "6.0"))       # detail_std below -> uniform
_DARK_MIN = float(os.environ.get("QUILL_FRAME_DARK_MIN", "8.0"))     # brightness below -> dark
_BRIGHT_MAX = float(os.environ.get("QUILL_FRAME_BRIGHT_MAX", "250")) # brightness above -> blown out
_DOMINANCE_MAX = float(os.environ.get("QUILL_FRAME_DOMINANCE_MAX", "0.50"))  # channel share
_SHARP_MIN = float(os.environ.get("QUILL_FRAME_SHARP_MIN", "1.5"))   # gradient var below -> blurry
_DEGRADED_SHARP = float(os.environ.get("QUILL_FRAME_DEGRADED_SHARP", "12.0"))
_SMALL = 160   # downscale longest side to this before scoring (speed; detail survives)


def _empty(reason: str) -> dict:
    return {"brightness": 0.0, "detail_std": 0.0, "channel_dominance": 1.0,
            "dominant": "none", "sharpness": 0.0, "quality": "dead",
            "capture_quality": 0.05, "reasons": [reason], "analyzable": False}


def _downsample(x: np.ndarray) -> np.ndarray:
    h, w = x.shape[:2]
    step = max(1, int(max(h, w) / _SMALL))
    return x[::step, ::step]


def score(frame_bgr) -> dict:
    """Score one BGR frame (HxWx3 uint8, as OpenCV `read()` returns). Returns the
    frame_quality dict; never raises — a degenerate input is a `dead` verdict."""
    try:
        x = np.asarray(frame_bgr)
        if x.ndim == 2:                       # already grayscale
            x = np.stack([x, x, x], axis=-1)
        if x.ndim != 3 or x.shape[2] < 3 or x.size == 0:
            return _empty("bad_shape")
        x = _downsample(x[:, :, :3]).astype(np.float32)
    except Exception:
        return _empty("unreadable")

    # Luminance (BGR order): standard Rec.601 weights on B,G,R.
    b, g, r = x[:, :, 0], x[:, :, 1], x[:, :, 2]
    lum = 0.114 * b + 0.587 * g + 0.299 * r
    brightness = float(lum.mean())
    detail_std = float(lum.std())

    # Channel balance: a real scene spreads across channels; a placeholder frame
    # is dominated by one (the green glitch -> G ~0.7+ of the total).
    means = np.array([b.mean(), g.mean(), r.mean()], dtype=np.float64)
    total = float(means.sum()) or 1.0
    dom_idx = int(np.argmax(means))
    channel_dominance = float(means[dom_idx] / total)
    dominant = ("blue", "green", "red")[dom_idx]

    # Sharpness: variance of the gradient magnitude (a numpy Laplacian proxy).
    gy, gx = np.gradient(lum)
    sharpness = float((gx * gx + gy * gy).var())

    out = {
        "brightness": round(brightness, 1),
        "detail_std": round(detail_std, 2),
        "channel_dominance": round(channel_dominance, 3),
        "dominant": dominant,
        "sharpness": round(sharpness, 1),
    }
    out["quality"], out["reasons"] = _classify(out)
    out["analyzable"] = out["quality"] != "dead"
    out["capture_quality"] = _capture_quality(out)
    return out


def _classify(m: dict) -> tuple[str, list[str]]:
    """Fold the pixel signals into good | degraded | dead + reasons.

    dead     = camera-broken: uniform, near-black, or a low-detail single-color
               cast (the green placeholder). NEVER worth a VLM call.
    degraded = dim or soft-focus but with real structure — analyze, but distrust.
    good     = a normal frame.
    """
    reasons: list[str] = []
    # --- dead: don't spend a VLM call -------------------------------------
    if m["detail_std"] < _VAR_MIN:
        reasons.append("uniform")
    if m["brightness"] < _DARK_MIN:
        reasons.append("near_black")
    # A strong single-color cast WITH little structure = a placeholder/glitch
    # frame (a green wall in a real photo still has texture -> high detail_std).
    if m["channel_dominance"] > _DOMINANCE_MAX and m["detail_std"] < (2 * _VAR_MIN):
        reasons.append(f"{m['dominant']}_cast")
    if reasons:
        return "dead", reasons

    # --- degraded: analyze, but scale trust down --------------------------
    if m["brightness"] > _BRIGHT_MAX:
        reasons.append("overexposed")
    if m["sharpness"] < _DEGRADED_SHARP:
        reasons.append("soft_focus")
    if reasons:
        return "degraded", reasons

    return "good", []


def _capture_quality(m: dict) -> float:
    """Map the frame score to a 0..1 capture_quality facet (#3 contract): the
    label sets the band, sharpness/brightness nudge within it."""
    base = {"good": 0.92, "degraded": 0.5, "dead": 0.08}.get(m["quality"], 0.5)
    if m["quality"] == "good":
        # A crisper, well-lit frame edges higher; a soft one edges lower.
        if m["sharpness"] < 40:
            base -= 0.07
        if not (30 <= m["brightness"] <= 220):
            base -= 0.05
    return round(max(0.0, min(1.0, base)), 4)
