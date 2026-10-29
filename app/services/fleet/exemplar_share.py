"""Offer a learned exemplar to teammates over the relay (distillation, step 2).

When a person accepts or corrects a local model's answer, learning_store
records a LearningPair and the exemplar store ingests it. With
QUILL_FLEET_EXEMPLARS_OFFER=1 the same pair is also turned into an
`exemplar` signal and queued as an approval offer, so one person's correction
can improve a teammate's local model.

It is always an offer, never an automatic share:

  * the owner's route rules still decide whether `learn.<task_type>` may leave
    at all (no rule -> local, so nothing is offered)
  * a `share` rule is downgraded to `offer`: a training example is the
    person's own words plus a judgment, so a human approves each one
  * only human-confirmed accepted/edited pairs ride (no shadow verdicts), only
    for task types a receiving exemplar store can use
  * the ceiling is `internal`, stricter than the firm-wide egress ceiling
    (`personal`): an exemplar is copied verbatim into a teammate's store, the
    same reason learning_store bars personal rows from the cloud grader. The
    pair's stamped class is the floor, so a row classed personal from its RAW
    text cannot leave because redaction made the stored copy look harmless
  * one offer per pair: confirm() re-notifies, and that must not re-offer

Nothing here imports the exemplar store or storage; the receiving side is
exemplar_intake.py.
"""
from __future__ import annotations

import os
import time

from app.config import settings
from app.services.fleet import dedup, kinds, router, state
from app.services.fleet import envelope as env

KIND = "exemplar"
PRODUCER = "agent:sparrow-learning"
TOPIC_PREFIX = "learn."

# The task types the exemplar kind accepts (and a receiver can retrieve).
SHAREABLE_TYPES = tuple(env.BUILTIN_KINDS[KIND]["fields"]["task_type"]["enum"])
_TIERS = {"edited": "human_edited", "accepted": "human_accepted"}
_MAX_TEXT = env.BUILTIN_KINDS[KIND]["fields"]["input_text"]["maxLength"]


def offer_enabled() -> bool:
    """Env read at call time; needs the fleet on too."""
    return bool(settings.fleet.enabled) and os.environ.get(
        "QUILL_FLEET_EXEMPLARS_OFFER", "0") in ("1", "true", "True")


def privacy_refusal(text: str, *, source: str,
                    declared_class: str | None = None) -> str | None:
    """None when an exemplar's text may cross; otherwise the reason. Fails
    closed on a classifier error. Used on both sides of the relay."""
    from app.services import privacy_class as pc
    try:
        cls = pc.max_class(declared_class, pc.classify_text(text or "",
                                                            source=source))
    except Exception as exc:
        return f"privacy_class unavailable ({type(exc).__name__})"
    if pc.rank(cls) <= pc.rank(pc.INTERNAL):
        return None
    return f"privacy_class={cls}"


def topic_for(task_type: str) -> str:
    return TOPIC_PREFIX + task_type


def signal_id_for(pair_id: str) -> str:
    return f"lp-{pair_id}"


def _already_offered(signal_id: str) -> bool:
    return any((r.get("signal") or {}).get("signal_id") == signal_id
               for r in router.list_offers())


def build_signal(pair: dict, *, now: float | None = None) -> dict:
    """The exemplar signal for one LearningPair. Raises SignalError when the
    pair cannot ride (wrong type, too long, malformed)."""
    now = time.time() if now is None else now
    task_type = str(pair.get("task_type") or "")
    tier = _TIERS.get(str(pair.get("verdict") or ""), "")
    what = "corrected" if tier == "human_edited" else "accepted"
    sig = {
        "signal_id": signal_id_for(str(pair.get("id") or "")),
        "origin_id": env.new_id(state.instance_tag()),
        "producer": PRODUCER,
        "ts": now,
        # Approval can wait days; the relay refuses anything past the cap.
        "expires_at": now + env.MAX_TTL_S,
        "topic": topic_for(task_type),
        "kind": KIND,
        "summary": f"A teammate {what} an answer for {task_type}.",
        "body": {"task_type": task_type,
                 "input_text": str(pair.get("input_text") or ""),
                 "target_text": str(pair.get("final_target") or ""),
                 "quality_tier": tier},
        "sources": [],
        "hops": 0,
        "sig": "",
    }
    return env.validate(sig, kinds=kinds.registry(), now=now,
                        max_hops=settings.fleet.max_hops).to_dict()


def offer_pair(pair: dict) -> dict:
    """Queue one confirmed pair as an exemplar offer. Returns
    {"status": skipped|local|refused|duplicate|offered, "reason", ...}.
    Never raises: learning must not break because sharing did."""
    try:
        if not offer_enabled():
            return {"status": "skipped", "reason": "offer off"}
        if pair.get("verdict") not in _TIERS:
            return {"status": "skipped", "reason": "not a positive verdict"}
        if not pair.get("human_confirmed"):
            return {"status": "skipped", "reason": "not human-confirmed"}
        task_type = str(pair.get("task_type") or "")
        if task_type not in SHAREABLE_TYPES:
            return {"status": "skipped", "reason": f"{task_type} not shareable"}
        text, target = (str(pair.get("input_text") or ""),
                        str(pair.get("final_target") or ""))
        if not text or not target:
            return {"status": "skipped", "reason": "empty pair"}
        if len(text) > _MAX_TEXT or len(target) > _MAX_TEXT:
            # Truncating a verified answer would teach a wrong one.
            return {"status": "skipped", "reason": "pair too long to share"}
        sid = signal_id_for(str(pair.get("id") or ""))
        if _already_offered(sid):
            return {"status": "duplicate", "reason": "already offered"}

        refusal = privacy_refusal(f"{text}\n{target}",
                                  source="fleet.exemplar",
                                  declared_class=pair.get("privacy_class"))
        if refusal:
            return {"status": "refused", "reason": f"content_{refusal}"}
        try:
            signal = build_signal(pair)
        except env.SignalError as exc:
            return {"status": "refused", "reason": exc.code}

        decision = router.route(signal)
        if decision.action in ("local", "refused"):
            return {"status": decision.action, "reason": decision.reason}
        # share or offer: a human approves every exemplar.
        dedup.record_own(signal["origin_id"], signal["expires_at"])
        row = router.create_offer(signal, rule=decision.rule)
        return {"status": "offered", "offer_id": row["offer_id"],
                "reason": decision.reason}
    except Exception as exc:
        print(f"[fleet] exemplar offer skipped ({exc}).")
        return {"status": "skipped", "reason": f"error: {exc}"}
