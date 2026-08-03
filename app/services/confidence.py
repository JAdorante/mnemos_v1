"""Event confidence contract — epistemic tags + separated confidence facets (#3).

One scalar `Event.confidence` quietly conflated two very different questions:
"how well did we *capture* it?" and "how sure is the model of the *content*?".
A quiet-but-clean utterance and a loud-but-garbled one could carry the same
number for opposite reasons. This module splits confidence into named facets and
tags each piece of knowledge with HOW it was obtained — so the action layer can
reason about trust instead of squinting at one opaque float.

Epistemic tag — how a piece of knowledge came to be (rising order of scrutiny):
  observed   directly perceived: a waveform, a frame. No model in the loop.
  extracted  pulled from an observation by a model: a transcript, a fact.
  inferred   derived by reasoning, not stated: a graph edge, a reflection insight.
  accepted   confirmed by a human: an approved fact, an edited packet.

Confidence facets — each in 0..1, independent, any may be None (= "unknown"):
  capture_quality     fidelity of the raw signal (audio SNR/clip, frame sharpness).
  model_confidence    the model's own certainty in its output (ASR logprob, VLM).
  semantic_confidence how well it grounds against memory (retrieval/match score).
  action_confidence   how safe it is to ACT on unattended — the readiness gate.

The contract rides in `Event.meta` (persisted as JSON, so no schema migration):
  meta["epistemic"]        one of the tags above
  meta["confidence"]       {facet: value, ...} for the facets that are known
  meta["action_readiness"] the single combined 0..1 gate (see `readiness`)
Read them ergonomically off the Event via `event.epistemic` /
`event.confidence_facets` / `event.action_readiness` (properties on the dataclass).
"""
from __future__ import annotations

import math
from typing import Any

# --- epistemic tags ---------------------------------------------------------
OBSERVED = "observed"
EXTRACTED = "extracted"
INFERRED = "inferred"
ACCEPTED = "accepted"

# Trust multiplier per tier: a directly-observed or human-accepted fact carries
# no epistemic penalty; a model extraction is discounted a little; a pure
# inference more so. Applied on top of the measured facets in `readiness`.
_TIER = {OBSERVED: 1.0, ACCEPTED: 1.0, EXTRACTED: 0.9, INFERRED: 0.75}

_FACETS = ("capture_quality", "model_confidence", "semantic_confidence",
           "action_confidence")


def facets(*, capture: float | None = None, model: float | None = None,
           semantic: float | None = None, action: float | None = None) -> dict:
    """Build a facet dict, dropping unknown (None) facets and clamping to 0..1."""
    raw = {"capture_quality": capture, "model_confidence": model,
           "semantic_confidence": semantic, "action_confidence": action}
    return {k: round(max(0.0, min(1.0, float(v))), 4)
            for k, v in raw.items() if v is not None}


def readiness(fac: dict, epistemic: str = EXTRACTED) -> float:
    """Fold the known facets + epistemic tier into one 0..1 action-readiness.

    `action_confidence`, when set, is authoritative (an explicit gate wins).
    Otherwise readiness is weakest-link-biased: half the *minimum* known facet
    plus half their *mean*, so one shaky signal (bad capture, low ASR) caps how
    ready we are, without a single unknown facet zeroing an otherwise-solid item.
    The result is then scaled by the epistemic tier. This is the seam a future
    action-readiness score (#10) and policy router (#12) read from."""
    fac = fac or {}
    if fac.get("action_confidence") is not None:
        base = fac["action_confidence"]
    else:
        vals = [fac[k] for k in ("capture_quality", "model_confidence",
                                 "semantic_confidence") if fac.get(k) is not None]
        base = (0.5 * min(vals) + 0.5 * (sum(vals) / len(vals))) if vals else \
            _TIER.get(epistemic, 0.8)
    return round(max(0.0, min(1.0, base * _TIER.get(epistemic, 0.85))), 4)


def attach(event, epistemic: str, *, capture: float | None = None,
           model: float | None = None, semantic: float | None = None,
           action: float | None = None):
    """Stamp an Event with its epistemic tag + confidence facets (in `meta`, so
    it persists). Also backfills the legacy scalar `event.confidence` from
    `model` when it's unset, so nothing downstream that reads it regresses.
    Returns the event for chaining. Never raises — a telemetry-grade side effect."""
    try:
        fac = facets(capture=capture, model=model, semantic=semantic, action=action)
        m = event.meta if isinstance(getattr(event, "meta", None), dict) else {}
        m["epistemic"] = epistemic
        if fac:
            m["confidence"] = fac
        m["action_readiness"] = readiness(fac, epistemic)
        event.meta = m
        if getattr(event, "confidence", None) is None and model is not None:
            event.confidence = model
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[confidence] attach skipped ({exc}).")
    return event


# --- facet mappers (turn raw pipeline signals into 0..1 facets) -------------
_AQ_BASE = {"good": 0.95, "noisy": 0.6, "bad": 0.25}


def capture_from_audio_quality(aq: dict | None) -> float | None:
    """Map an audio_quality score (label + SNR) to a 0..1 capture_quality. The
    label sets the band; SNR nudges within it so two 'good' clips still differ."""
    if not aq:
        return None
    base = _AQ_BASE.get(aq.get("quality"), 0.5)
    snr = aq.get("snr_est")
    if isinstance(snr, (int, float)):
        # +/-0.05 across a ~20 dB useful range, centered near 15 dB.
        base += max(-0.05, min(0.05, (snr - 15.0) / 200.0))
    return round(max(0.0, min(1.0, base)), 4)


def conf_from_asr(value: float | None) -> float | None:
    """Normalize an ASR certainty to a 0..1 probability. Whisper's avg_logprob
    is a (negative) log-probability -> exp() recovers the probability; a value
    already in (0, 1] (e.g. language_probability fallback) passes through."""
    if value is None:
        return None
    if value > 0:
        return round(min(1.0, float(value)), 4)
    return round(max(0.0, min(1.0, math.exp(float(value)))), 4)


# --- fact-side helpers (derive the contract for stored facts, no migration) --
def fact_epistemic(review: str | None) -> str:
    """A stored fact is `accepted` once a human approves it, else `extracted`."""
    return ACCEPTED if review == "approved" else EXTRACTED


def fact_readiness(confidence: float | None, *, review: str | None = None,
                   capture: float | None = None,
                   semantic: float | None = None) -> float:
    """Action-readiness for a stored fact: its model confidence + optional
    capture/semantic facets, tiered by whether a human has accepted it. This is
    what the action gate (task offers, approval) should key off — not the raw
    per-fact confidence alone — so a human-approved fact clears a bar that an
    unreviewed same-confidence one doesn't.

    Unlike `readiness`, a fact with NOTHING measured is treated as not-ready
    (0.0), not given a tier prior: for an action gate a missing confidence must
    read as low ("prefer silence"), never as tacit permission to act."""
    fac = facets(model=confidence, capture=capture, semantic=semantic)
    if not fac:
        return 0.0
    return readiness(fac, fact_epistemic(review))
