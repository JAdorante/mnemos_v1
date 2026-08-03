"""#10 — unified action-readiness: ONE score + ONE decision for acting.

Everything the system learned to measure — separated confidence facets (#3),
capture quality (audio_quality / frame_quality, #6), source faithfulness (#2),
epistemic tags + human review (#3), and the planner's risk table — used to be
consulted piecemeal: task_offer looked at one number, the approval packet at
another, the vision to-do path at a third. This is the single seam. Given a
proposed action's signals AND its risk, it computes one 0..1 readiness score and
maps it to one decision BAND the whole system can act on consistently.

Bands — what to DO with the action:
  auto     confident + safe enough to do WITHOUT asking (low-risk only, opt-in)
  offer    surface a yes/no ("run this?") — the default for a solid task
  review   weak/uncertain — keep it as a reviewable item, but don't nag
  hold     too little to act on — record it, don't surface

Risk raises the bar (not just the score): a high-risk action (send/buy/pay)
never auto-acts and needs a higher score even to offer; a low-risk one
(read/draft/summarize) proceeds on less. Auto is OFF by default (QUILL_AUTO_ACT):
the score enables an unattended path, but the system stays ask-first until you opt in.

    v = for_task("email Justin the deck", confidence=0.7)
    v.band   # 'review'  (a 'send' is high-risk -> needs >= 0.75)
    v.score  # 0.63

Design: the SCORE reuses the #3 `confidence.readiness` (weakest-link facets ×
epistemic tier) so there's no second, divergent formula — this layer adds the
faithfulness modifier, the risk lookup, and the banding.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.services import confidence as _conf

# Minimum score to OFFER, per risk tier. The default (low/medium) is 0.60 — the
# same effective floor task_offer used before #10, so ordinary tasks behave
# exactly as they did; only riskier actions get a higher bar. `None` = never.
_OFFER_FLOOR = {"low": 0.60, "medium": 0.60, "high": 0.75, "blocked": None}
_REVIEW_FLOOR = float(os.environ.get("QUILL_READINESS_REVIEW_FLOOR", "0.30"))
_AUTO_FLOOR = float(os.environ.get("QUILL_READINESS_AUTO_FLOOR", "0.85"))


def _auto_enabled() -> bool:
    """Unattended action is OFF by default — the score can reach the `auto` band
    only when the user explicitly opts in. Everything stays ask-first until then."""
    return os.environ.get("QUILL_AUTO_ACT", "0") not in ("0", "false", "False")


@dataclass
class Verdict:
    score: float
    band: str                 # auto | offer | review | hold
    risk: str = "low"
    facets: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def should_offer(self) -> bool:
        """The action is ready enough to surface (or act, if auto)."""
        return self.band in ("auto", "offer")


def score(*, capture: float | None = None, model: float | None = None,
          semantic: float | None = None, epistemic: str = _conf.EXTRACTED,
          review: str | None = None, faithful: bool | None = None) -> float:
    """The unified 0..1 readiness score. Base = #3 facets-readiness (weakest-link
    × epistemic tier, with `review` promoting an accepted item to a higher tier),
    then modified by source faithfulness (#2): an unfaithful source_span is a
    hallucination signal -> hard penalty; a verified-verbatim one -> small boost.
    Nothing measured -> 0.0 (a missing signal never reads as tacit readiness)."""
    ep = _conf.fact_epistemic(review) if review is not None else epistemic
    fac = _conf.facets(capture=capture, model=model, semantic=semantic)
    base = _conf.readiness(fac, ep) if fac else 0.0
    if faithful is False:
        base *= 0.5
    elif faithful is True:
        base = min(1.0, base + 0.05)
    return round(max(0.0, min(1.0, base)), 4)


def band(s: float, risk: str = "low") -> str:
    """Map a readiness score + risk to a decision band."""
    floor = _OFFER_FLOOR.get(risk, 0.60)
    if floor is None:                       # blocked risk -> never surface to act
        return "hold"
    if _auto_enabled() and risk == "low" and s >= _AUTO_FLOOR:
        return "auto"
    if s >= floor:
        return "offer"
    if s >= _REVIEW_FLOOR:
        return "review"
    return "hold"


def decide(*, risk: str = "low", **signals) -> Verdict:
    """Score the signals and band them against `risk`. `signals` are the `score`
    kwargs (capture/model/semantic/epistemic/review/faithful)."""
    s = score(**signals)
    fac = _conf.facets(capture=signals.get("capture"), model=signals.get("model"),
                       semantic=signals.get("semantic"))
    reasons = [f"{k}={v}" for k, v in fac.items()]
    if signals.get("faithful") is False:
        reasons.append("unfaithful_source")
    if signals.get("review") == "approved":
        reasons.append("human_accepted")
    return Verdict(score=s, band=band(s, risk), risk=risk, facets=fac, reasons=reasons)


def for_task(text: str, confidence: float | None, *,
             capture: float | None = None, semantic: float | None = None,
             review: str | None = None, faithful: bool | None = None) -> Verdict:
    """Readiness for a heard/seen TASK: risk is inferred from the task text (the
    planner's single risk source), the model facet is the extractor's per-fact
    confidence. This is what the action gate (task_offer, vision to-do) keys off —
    one score, risk-aware, instead of a bare confidence threshold."""
    risk = "low"
    try:
        from app.services.agent_planner import risk_of
        risk = risk_of(text)[0]
    except Exception:
        pass
    return decide(risk=risk, model=confidence, capture=capture,
                  semantic=semantic, review=review, faithful=faithful)


def for_fact(fact: dict, *, capture: float | None = None,
             semantic: float | None = None, faithful: bool | None = None) -> Verdict:
    """Readiness for a stored fact row (Console/gate use): pulls text, confidence,
    and review status from the row and scores them."""
    return for_task(
        fact.get("text") or fact.get("source_span") or "",
        fact.get("confidence"),
        capture=capture, semantic=semantic,
        review=fact.get("review"), faithful=faithful)
