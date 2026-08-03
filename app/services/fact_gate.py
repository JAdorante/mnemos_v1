"""Write-time quality gates + lifecycle for extracted facts — memory hygiene.

Every fact the extractor wants to persist passes through gate_fact() first:

  1. confidence floor    — a guess below QUILL_FACT_MIN_CONF never enters the
                           store (confidence used to be stored but never enforced)
  2. span faithfulness   — a fact whose source_span is not a (normalized)
                           verbatim quote of the speech it cites is treated as a
                           hallucination and dropped (the telemetry check from
                           cog_telemetry.span_is_faithful, promoted to a gate)
  3. near-duplicate      — cosine >= auto_dup_sim vs an ACTIVE fact of the same
                           kind: refresh that fact (touch_fact) instead of
                           inserting a twin row
  4. update/contradiction — cosine in [adjudicate_sim, auto_dup_sim): a small
                           local-model call decides duplicate / update /
                           unrelated; 'update' inserts the new fact and marks
                           the old one superseded ("meeting moved to 3pm"
                           replaces "meeting at 2pm" instead of coexisting)

Best-effort by design: when the vector index or the adjudicator model is
unavailable the gate degrades to plain insert — capture must never lose facts
to hygiene infrastructure. Every non-insert verdict is logged to cognition
telemetry (metric: fact_hygiene) so the Console can show the drop/merge rate.

Generic code: thresholds are config; nothing user-specific lives here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import settings


@dataclass
class Verdict:
    """What to do with a candidate fact. `action` is one of:
    insert | drop | dedup (refresh dup_fact_id) | supersede (insert, then
    mark each id in supersede_ids replaced by the new row)."""
    action: str
    reason: str = ""
    dup_fact_id: int | None = None
    supersede_ids: tuple[int, ...] = field(default_factory=tuple)


# JSON contract for the update/contradiction adjudicator.
_ADJ_SCHEMA = {
    "type": "object",
    "properties": {
        "relation": {"type": "string",
                     "enum": ["duplicate", "update", "unrelated"]},
    },
    "required": ["relation"],
}

_ADJ_SYSTEM = (
    "You maintain a personal memory store. Given an OLD stored fact and a NEW "
    "candidate fact of the same kind, classify their relationship:\n"
    "- duplicate: they assert the same thing (wording may differ)\n"
    "- update: NEW replaces or corrects OLD — a time/date moved, a detail was "
    "corrected, a plan changed, a status progressed\n"
    "- unrelated: different assertions that merely look similar\n"
    "Answer with JSON only."
)


def _adjudicate(kind: str, old_text: str, new_text: str) -> str:
    """duplicate | update | unrelated — via the local-first router (a rare
    escalation is pinned to Haiku, never Opus). Any failure -> 'unrelated',
    which degrades to plain insert (today's behavior)."""
    try:
        from app.services.model_router import router
        out = router.complete_json(
            "adjudicate", system=_ADJ_SYSTEM,
            messages=[{"role": "user", "content":
                       f"kind: {kind}\nOLD: {old_text}\nNEW: {new_text}"}],
            schema=_ADJ_SCHEMA, max_tokens=64, model="claude-haiku-4-5")
        rel = (out or {}).get("relation", "unrelated")
        return rel if rel in ("duplicate", "update", "unrelated") else "unrelated"
    except Exception:
        return "unrelated"


def _similar_active(kind: str, text: str, k: int = 4) -> list[tuple[int, float, str]]:
    """Nearest ACTIVE same-kind facts by cosine: [(fact_id, score, text)],
    best first. Empty when the vector index is unavailable."""
    try:
        from app.services.memory import memory
        return memory.similar_facts(kind, text, k=k)
    except Exception:
        return []


def _telemetry(action: str, reason: str, kind: str, text: str) -> None:
    try:
        from app.services.cog_telemetry import cog_telemetry
        cog_telemetry.record("fact_hygiene", action == "insert",
                             action=action, reason=reason, kind=kind,
                             text=(text or "")[:120])
    except Exception:
        pass


def gate_fact(kind: str, text: str, confidence: float | None,
              span: str, source_text: str) -> Verdict:
    """Decide what to do with one candidate fact BEFORE it is persisted.
    Pure decision — the caller (extractor._persist) applies it."""
    cfg = settings.facts
    text = (text or "").strip()
    if not text:
        return Verdict("drop", "empty text")

    if cfg.min_conf > 0 and confidence is not None and confidence < cfg.min_conf:
        v = Verdict("drop", f"confidence {confidence:.2f} < floor {cfg.min_conf}")
        _telemetry(v.action, v.reason, kind, text)
        return v

    if cfg.span_gate and (source_text or "").strip():
        from app.services.cog_telemetry import span_is_faithful
        if not span_is_faithful(span, source_text):
            v = Verdict("drop", "source_span is not a verbatim quote")
            _telemetry(v.action, v.reason, kind, text)
            return v

    if not cfg.dedup:
        return Verdict("insert")

    cands = _similar_active(kind, text)
    for fid, score, _old in cands:
        if score >= cfg.auto_dup_sim:
            v = Verdict("dedup", f"cos {score:.2f} vs fact {fid}",
                        dup_fact_id=fid)
            _telemetry(v.action, v.reason, kind, text)
            return v

    superseded: list[int] = []
    # Adjudicate at most the two strongest in-band candidates — enough to catch
    # a re-stated fact plus its own earlier version, cheap enough to run inline.
    for fid, score, old_text in [c for c in cands
                                 if c[1] >= cfg.adjudicate_sim][:2]:
        rel = _adjudicate(kind, old_text, text)
        if rel == "duplicate":
            v = Verdict("dedup", f"adjudicated duplicate of fact {fid}",
                        dup_fact_id=fid)
            _telemetry(v.action, v.reason, kind, text)
            return v
        if rel == "update":
            superseded.append(fid)

    if superseded:
        v = Verdict("supersede", f"updates fact(s) {superseded}",
                    supersede_ids=tuple(superseded))
        _telemetry(v.action, v.reason, kind, text)
        return v
    return Verdict("insert")
