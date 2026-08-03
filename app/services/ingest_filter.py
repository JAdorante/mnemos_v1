"""Ingest hygiene — a structured verdict on every transcribed utterance (#7).

faster-whisper (especially the CPU `small` model) hallucinates confident-looking
text on silence, breath, and room noise: "Thank you.", "Thanks for watching.",
"Bless you.", bare "You". Those fragments pollute the timeline and, worse, can be
picked up by the agent as if they were real intent. This module is the cheap
first line of defense: a pure `assess()` that scores one transcribed utterance
from the signals Whisper already hands back (per-segment `no_speech_prob` and
`avg_logprob`) plus a denylist of known hallucination phrases.

Rather than a bare keep/drop bit, `assess()` returns an `IngestVerdict` — an
`action`, a 0..1 `confidence`, and `reasons` / `warnings` — so the pipeline can
route intelligently and, above all, *never silently lose a real commitment*:

    keep                clean, trust it as memory
    keep_low_confidence intelligible but shaky — stored + flagged for the Console
    needs_user_review   real words at low confidence — kept, but surfaced to ask
    store_audio_only    no reliable text — keep the CLIP (provenance), drop the text
    drop_hallucination  a known ghost phrase on weak audio — safe to discard

For a memory product, deletion is dangerous: a borderline transcript should be
demoted, not vanish. Only confident hallucinations are truly dropped.

    verdict = assess(text, segments, cfg)
    if verdict.action == "drop_hallucination": ...     # discard
    if verdict.action == "store_audio_only":  ...       # keep clip, not text
    event.confidence = verdict.avg_logprob
    event.meta["quality"] = verdict.as_meta()
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from app.config import settings

# Whole-utterance phrases Whisper emits on near-silence. Matched after
# normalization (lowercased, punctuation/whitespace stripped), so "Thank you.",
# "thank you", and "THANK YOU!" all collapse to the same key.
HALLUCINATION_PHRASES = frozenset({
    "", "you", "thank you", "thanks", "thank you very much",
    "thanks for watching", "thank you for watching", "thanks for watching!",
    "please subscribe", "like and subscribe", "bless you", "bye", "bye bye",
    "okay", "ok", "so", "uh", "um", "hmm", "yeah", "the", "i",
    "subtitles by the amara.org community", "subtitles by amara.org",
    "transcription by castingwords", "www.mooji.org",
})

# Confidence mapping: avg_logprob typically runs -0.1 (crisp) .. -1.5 (garbage).
_GOOD_LOGPROB = -0.1
_MIN_LOGPROB = -1.5

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Actions that still yield a persisted transcript (vs. dropped / audio-only).
_KEEP_ACTIONS = ("keep", "keep_low_confidence", "needs_user_review")


def normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for denylist matching."""
    t = _PUNCT.sub(" ", (text or "").lower())
    return _WS.sub(" ", t).strip()


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


@dataclass
class IngestVerdict:
    action: str                    # keep | keep_low_confidence | needs_user_review
                                   # | store_audio_only | drop_hallucination
    confidence: float | None       # overall trust in this transcript, 0..1
    reasons: list                  # decisive signals behind the action
    warnings: list                 # non-fatal concerns to surface
    avg_logprob: float | None      # mean over segments (None if unavailable)
    no_speech_prob: float | None   # mean over segments
    n_segments: int

    # --- backward-compat shims (callers written against the old bool Verdict) --
    @property
    def keep(self) -> bool:
        return self.action in _KEEP_ACTIONS

    @property
    def low_confidence(self) -> bool:
        return self.action in ("keep_low_confidence", "needs_user_review")

    @property
    def needs_review(self) -> bool:
        return self.action == "needs_user_review"

    @property
    def reason(self) -> str:
        """A single-string reason (primary reason, else the action) — kept so old
        callers and the Console's `quality.reason` keep working."""
        return self.reasons[0] if self.reasons else self.action

    def as_meta(self) -> dict:
        return {
            "action": self.action,
            "confidence": self.confidence,
            "avg_logprob": (round(self.avg_logprob, 3)
                            if self.avg_logprob is not None else None),
            "no_speech_prob": (round(self.no_speech_prob, 3)
                               if self.no_speech_prob is not None else None),
            "low_confidence": self.low_confidence,
            "needs_review": self.needs_review,
            "reason": self.reason,
            "reasons": self.reasons,
            "warnings": self.warnings,
        }


# Old name kept as an alias so `from ...ingest_filter import Verdict` still binds.
Verdict = IngestVerdict


def _mean(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _confidence(logp: float | None, nsp: float | None) -> float | None:
    """Blend the two Whisper signals into a single 0..1 trust score."""
    parts = []
    if logp is not None:
        parts.append(_clamp01((logp - _MIN_LOGPROB) / (_GOOD_LOGPROB - _MIN_LOGPROB)))
    if nsp is not None:
        parts.append(_clamp01(1.0 - nsp))
    return round(sum(parts) / len(parts), 2) if parts else None


def assess(text: str, segments: Iterable, cfg=None) -> IngestVerdict:
    """Score one transcribed utterance into an IngestVerdict. `segments` are
    faster-whisper Segment objects (each with .avg_logprob and .no_speech_prob);
    an empty/degenerate list just means those signals are unavailable and we fall
    back to the text-only checks."""
    cfg = cfg or settings.ingest
    seg_list = list(segments or [])
    logp = _mean([getattr(s, "avg_logprob", None) for s in seg_list])
    nsp = _mean([getattr(s, "no_speech_prob", None) for s in seg_list])
    norm = normalize(text)
    words = (text or "").strip().split()
    conf = _confidence(logp, nsp)

    def v(action, reasons, warnings=None):
        return IngestVerdict(action, conf, reasons, warnings or [],
                             logp, nsp, len(seg_list))

    # 1. Nothing meaningful left after stripping — no text, but keep the clip.
    if len(norm) < cfg.min_chars:
        return v("store_audio_only", ["too_short"])

    # 2. A known hallucination phrase as the WHOLE utterance, on weak audio.
    #    Only drop when the confidence signals also look weak, so a genuinely-
    #    spoken "thank you" (high logprob, low no-speech) survives to step 5/6.
    weak = ((nsp is not None and nsp >= cfg.phrase_no_speech_prob)
            or (logp is not None and logp < cfg.phrase_avg_logprob)
            or (nsp is None and logp is None))
    if norm in HALLUCINATION_PHRASES and weak:
        return v("drop_hallucination", [f"hallucination_phrase:{norm!r}"])

    # 3. Whisper itself thinks this was probably silence — no reliable text.
    if nsp is not None and nsp >= cfg.max_no_speech_prob:
        return v("store_audio_only", [f"no_speech_prob={nsp:.2f}"])

    # 4. Very low token confidence. If there are real words, DON'T discard — a
    #    mis-heard sentence could be a real commitment; flag it for human review.
    #    A one/two-word low-confidence blip has no such stakes -> audio-only.
    if logp is not None and logp < cfg.min_avg_logprob:
        if len(words) >= cfg.review_min_words:
            return v("needs_user_review", [f"avg_logprob={logp:.2f}"],
                     ["low_transcription_confidence"])
        return v("store_audio_only", [f"avg_logprob={logp:.2f}"])

    # 5. Kept, but borderline — persist it and flag for the Console to surface.
    warnings = []
    if logp is not None and logp < cfg.low_conf_logprob:
        warnings.append(f"avg_logprob={logp:.2f}")
    if nsp is not None and nsp >= cfg.low_conf_no_speech:
        warnings.append(f"no_speech_prob={nsp:.2f}")
    if norm in HALLUCINATION_PHRASES:
        warnings.append("generic_phrase")
    if warnings:
        return v("keep_low_confidence", ["low_confidence"], warnings)

    # 6. Clean — trust it.
    return v("keep", ["ok"])
