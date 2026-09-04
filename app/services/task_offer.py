"""Proactive task offers — the hear-a-task -> ask-about-it trigger.

The vision to-do watcher (todo_watcher.py) offers to run to-do lists it *sees*;
this does the same for tasks Sparrow *hears*. When the extractor mints a task fact
from speech, it calls `offer_task`, which — if the task looks actionable enough —
surfaces a yes/no offer in the chat stream via the agent worker. 'yes' hands the
task to the agent (with its per-commit approval gate); 'no' just leaves it on the
Tasks board.

Gated so Sparrow isn't chatty by TWO independent signals (#10) — an offer must
clear BOTH before it surfaces:

  1. READINESS — the unified action-readiness score (services/readiness.py): a
     risk-aware score+band over capture quality × extraction confidence × risk.
     "Are we sure enough about *what was said*?"
  2. INTENT EVIDENCE — corroboration that this is a real transactional directive,
     not ambient talk: a concrete action verb tied to a real object (a known
     contact, a proper noun, or a time), decent confidence behind that verb, or a
     very-high-confidence single shot. "Is this actually a thing to *do*?"

One signal alone doesn't surface an offer. This is what kills the false-offer
class the eval harness (#8) flagged — "...anyway I should tell Kristi it's fine..."
is ready-enough but has no transactional verb, so it stays on the board silently
instead of interrupting. Disable the second gate with QUILL_OFFER_TWO_SIGNAL=0.

Also records the OUTCOME of every surfaced offer (accepted / dismissed via
`record_offer_outcome`, called from agent_bridge.resolve_todo) so a falling
accept-rate — offers that don't land — is visible in /console/cognition next to
the surfaced-rate. The same task text isn't re-offered within a cooldown window.
Disable everything with QUILL_TASK_OFFER=0 (or QUILL_AGENT=0). Tune the readiness
floors via QUILL_READINESS_* (see readiness.py).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time

_recent: dict[str, float] = {}      # task-text hash -> last offered time
_lock = threading.Lock()
_COOLDOWN_S = 300                    # don't re-offer the same task within 5 min

# Confidence at/above which a concrete action verb needs no further object
# corroboration (a clearly-stated "Renew the parking permit" shouldn't be held
# just because it names nothing). Below it, the verb must tie to a real object.
# Tuned to 0.65 on the synthetic A/B: the ambient false-offer cases carry NO
# concrete action verb, so this floor never admits them — lowering it only
# recovers real verb-tasks (recall 0.80 -> 1.00 at false-offer rate 0.00).
_VERB_CONF_FLOOR = float(os.environ.get("QUILL_OFFER_VERB_CONF", "0.65"))
# Confidence at/above which the extraction alone is strong enough to corroborate.
_STRONG_CONF = float(os.environ.get("QUILL_OFFER_STRONG_CONF", "0.90"))


def _enabled() -> bool:
    return (os.environ.get("QUILL_TASK_OFFER", "1") not in ("0", "false", "False")
            and os.environ.get("QUILL_AGENT") not in ("0", "false", "False"))


def _two_signal_enabled() -> bool:
    """The #10 second gate is ON by default; QUILL_OFFER_TWO_SIGNAL=0 restores the
    pre-#10 readiness-only behavior."""
    return os.environ.get("QUILL_OFFER_TWO_SIGNAL", "1") not in ("0", "false", "False")


def _names_known_entity(text: str) -> bool:
    """Does the task reference a person/project in the user's vocabulary? A known
    target is stronger evidence of a real task than a bare capitalized word."""
    try:
        from app.services.vocabulary import vocabulary
        import re as _re
        for tok in _re.findall(r"[A-Za-z][A-Za-z'\-]+", text or ""):
            if vocabulary.recognize(tok).get("known"):
                return True
    except Exception:
        pass
    return False


def _second_signal(text: str, confidence: float | None) -> tuple[bool, list[str]]:
    """Signal 2 (#10): is there corroborating evidence this is a real transactional
    directive? Returns (corroborated, signals). The concrete-verb list deliberately
    EXCLUDES conversational light verbs (tell/ask/say/go/do) — that lexical choice
    is what separates 'text Abby the deck' (transactional) from 'tell Kristi it's
    fine' (ambient), the exact false-offer the roadmap targets."""
    signals: list[str] = []
    try:
        from app.services import intent
        norm = intent._norm(text)
        wordset = set(norm.split())
        has_verb = bool(wordset & intent._ACTION_VERBS)
        has_temporal = bool(wordset & intent._TEMPORAL)
        has_proper = intent._has_proper_noun(text)
    except Exception:
        # Intent module unavailable — fall back to confidence only.
        has_verb = has_temporal = has_proper = False
    has_known = _names_known_entity(text)
    conf = confidence if isinstance(confidence, (int, float)) else None

    if has_verb:
        signals.append("action_verb")
    if has_known:
        signals.append("known_target")
    if has_proper:
        signals.append("named")
    if has_temporal:
        signals.append("due")
    if conf is not None and conf >= _STRONG_CONF:
        signals.append("high_conf")

    corroborated = (
        ("high_conf" in signals)
        # a concrete verb tied to a real object (known contact / proper noun / time)
        or (has_verb and (has_known or has_proper or has_temporal))
        # a concrete verb stated with decent confidence, even w/o a named object
        or (has_verb and conf is not None and conf >= _VERB_CONF_FLOOR)
    )
    return corroborated, signals


def _hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode()).hexdigest()


def offer_task(text: str, confidence: float | None, fact_id: int | None) -> bool:
    """Offer a heard task in chat if it clears the confidence/cooldown gates.

    Returns True if an offer was surfaced (or queued). Never raises — the
    extraction path must not break on a chatty side-effect.
    """
    try:
        text = (text or "").strip()
        if not text or not _enabled():
            return False
        # From here on the task was *considered* for an offer — record whether
        # it gets surfaced vs suppressed, so a rising surfaced-rate ("getting
        # chatty") is visible in /console/cognition (#9).
        def _tele(hit: bool, reason: str, **meta) -> None:
            try:
                from app.services.cog_telemetry import cog_telemetry, OFFER
                cog_telemetry.record(OFFER, hit, reason=reason,
                                     text=text[:80], **meta)
            except Exception:
                pass

        # Gate on the UNIFIED action-readiness decision (#10): one risk-aware
        # score + band, not a bare confidence threshold. Ordinary (low/medium-
        # risk) tasks keep the same effective floor as before; a risky action
        # (send/buy/pay) needs a higher score even to be offered. review/hold ->
        # keep it on the board but don't proactively surface (prefer silence).
        from app.services.readiness import for_task
        v = for_task(text, confidence)
        if not v.should_offer:
            _tele(False, f"readiness:{v.band}", score=v.score, risk=v.risk)
            return False

        # SIGNAL 2 (#10): readiness alone isn't enough — require corroborating
        # intent evidence that this is a real thing to DO. Suppresses the ambient
        # "I should tell Kristi it's fine" class (ready, but no transactional verb).
        if _two_signal_enabled():
            corroborated, sig2 = _second_signal(text, confidence)
            if not corroborated:
                _tele(False, "one_signal_only", score=v.score, risk=v.risk,
                      signals=",".join(sig2))
                return False

        h = _hash(text)
        now = time.time()
        with _lock:
            last = _recent.get(h)
            if last is not None and now - last < _COOLDOWN_S:
                _tele(False, "cooldown")
                return False
            _recent[h] = now

        from app.services.agent_bridge import worker

        offered = worker.propose_task(text, fact_id=fact_id, confidence=confidence)
        _tele(True, "surfaced", score=v.score, risk=v.risk)
        print(f"[task-offer] offered heard task in chat "
              f"({'shown' if offered else 'queued'}): {text[:60]!r}")
        return True
    except Exception as exc:  # never break the extraction path
        print(f"[task-offer] skipped ({exc}).")
        return False


def record_offer_outcome(text: str, accepted: bool, *, kind: str = "task",
                         fact_id: int | None = None) -> None:
    """Record whether a surfaced offer was ACCEPTED (yes) or DISMISSED (no), so a
    falling accept-rate — offers that keep getting waved off — is visible next to
    the surfaced-rate in /console/cognition. Called from agent_bridge.resolve_todo
    when the user answers a task/phone offer. Best-effort; never raises."""
    try:
        from app.services.cog_telemetry import cog_telemetry, OFFER_OUTCOME
        cog_telemetry.record(OFFER_OUTCOME, bool(accepted),
                             kind=kind, text=(text or "")[:80])
    except Exception:
        pass
    try:
        from app.services.attention_ledger import attention_ledger
        attention_ledger.close_offer(
            fact_id=fact_id, text=text, accepted=accepted, kind=kind)
    except Exception:
        pass
