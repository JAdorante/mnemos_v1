"""Cognition telemetry — the measurement layer over Mnemos's *judgement*.

`model_log` measures the model calls (latency/tokens/cost) and `audio_telemetry`
measures the capture pipeline (SNR / drop reasons). Neither measures whether the
*thinking* is trustworthy. This does — the three rates the roadmap (#9) asks for
that nothing else records:

  fact_faithfulness  every extracted fact's `source_span` must be a verbatim
                     quote of the speech it came from. A span that isn't there
                     is a hallucinated provenance pointer -> a label-free
                     hallucination rate, recorded on every persisted fact.
  source_grounding   when the agent renders an approval packet, did the Source
                     line come from the authoritative fact/clip (grounded) or
                     from the model's own paraphrase (ungrounded)? This is the
                     payoff of the fact_id->packet bridge, made measurable.
  proactive_offer    of the heard tasks considered for a chat offer, how many
                     were actually surfaced (vs suppressed by the confidence /
                     cooldown gate). A rising surfaced-rate = getting chatty.

Same shape as `model_log`: a per-event JSONL trail (data/cognition.jsonl) plus a
rolling in-memory aggregate the console reads via /console/cognition. Recording
is best-effort and never raises into the caller — telemetry must not break the
thing it measures.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.config import settings

# Metric names — constants so the recording sites and the console agree on keys.
FAITHFULNESS = "fact_faithfulness"
GROUNDING = "source_grounding"
OFFER = "proactive_offer"
OFFER_OUTCOME = "offer_outcome"     # of surfaced offers, how many were ACCEPTED
REASONER_OFFER = "reasoner_offer"   # Track D: surfaced vs suppressed reasoner offers
TRIGGER_OFFER = "trigger_offer"     # standing triggers: surfaced vs suppressed fires
OPEN_LOOP = "open_loop"             # plan 4.3: open-loop chip surfaced
OPEN_LOOP_DISMISS = "open_loop_dismiss"  # plan 4.3: user snoozed / dismissed a loop

_WORD = re.compile(r"[a-z0-9$]+")


def _norm(s: str) -> str:
    return " ".join(_WORD.findall((s or "").lower()))


def span_is_faithful(span: str, source_text: str) -> bool:
    """A fact's `source_span` is faithful when it's a verbatim quote of the
    speech it was extracted from. Compared on normalized text (lowercased,
    whitespace/punctuation-collapsed) so trivial casing/spacing differences
    don't read as hallucinations, while a span quoting words never said fails.
    This is the single definition the live extractor, the golden eval, and the
    offline DB audit all share — so the eval actually validates the live check."""
    span = (span or "").strip()
    if not span:
        return False
    return _norm(span) in _norm(source_text)

# For each metric, whether a high `hit` rate is good (True) or a signal to watch
# (False) — lets the console flag "getting chatty" without hardcoding names.
_HIGHER_IS_BETTER = {FAITHFULNESS: True, GROUNDING: True, OFFER: False,
                     OFFER_OUTCOME: True, REASONER_OFFER: False,
                     TRIGGER_OFFER: False,
                     OPEN_LOOP: False,          # more surfaces → watch chatty
                     OPEN_LOOP_DISMISS: False}  # dismiss rate up → precision down


class CogTelemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = Path(settings.storage.data_dir) / "cognition.jsonl"
        # metric -> {"total": n, "hits": n}. `hits`/`total` is the rate.
        self._agg: dict[str, dict[str, int]] = defaultdict(
            lambda: {"total": 0, "hits": 0})

    def record(self, metric: str, hit: bool, **meta: Any) -> None:
        """Record one judgement outcome for `metric`. `hit` is the numerator
        (a faithful span / a grounded packet / a surfaced offer). `meta` is
        drill-down context written to the trail only. Never raises."""
        try:
            with self._lock:
                a = self._agg[metric]
                a["total"] += 1
                a["hits"] += 1 if hit else 0
            row = {"ts": round(time.time(), 3), "metric": metric,
                   "hit": bool(hit), **meta}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception as exc:  # telemetry must never break the caller
            print(f"[cog_telemetry] record skipped ({exc}).")

    def rates(self) -> dict[str, Any]:
        """Session aggregate for the console: per-metric total/hits/rate. For
        faithfulness/grounding, `rate` is the good rate; the paired `bad_rate`
        surfaces the hallucination / ungrounded / surfaced fraction directly."""
        with self._lock:
            metrics = {}
            for metric, a in sorted(self._agg.items()):
                total = a["total"]
                rate = (a["hits"] / total) if total else None
                metrics[metric] = {
                    "total": total,
                    "hits": a["hits"],
                    "rate": round(rate, 4) if rate is not None else None,
                    "bad_rate": round(1 - rate, 4) if rate is not None else None,
                    "higher_is_better": _HIGHER_IS_BETTER.get(metric, True),
                }
        return {"metrics": metrics}


cog_telemetry = CogTelemetry()
