"""Write-time quality gates + lifecycle for extracted facts — memory hygiene.

Every fact the extractor wants to persist passes through gate_fact() first:

  1. confidence floor    — a guess below QUILL_FACT_MIN_CONF never enters the
                           store (confidence used to be stored but never enforced)
  2. span faithfulness   — a fact whose source_span is not a (normalized)
                           verbatim quote of the speech it cites is treated as a
                           hallucination and dropped (the telemetry check from
                           cog_telemetry.span_is_faithful, promoted to a gate)
  3. span overlap        — structural dedup (People v3 WS-F, flag
                           QUILL_FACT_DEDUP_V2): a same-kind ACTIVE fact whose
                           source event range overlaps this one's >= 50% and
                           whose text shares tokens is the same fact
                           re-extracted from an overlapping window — collapsed
                           deterministically, before embeddings ever run
  4. near-duplicate      — cosine >= auto_dup_sim vs an ACTIVE fact of the same
                           kind: refresh that fact (touch_fact) instead of
                           inserting a twin row
  5. update/contradiction — cosine in [adjudicate_sim, auto_dup_sim): a small
                           local-model call decides duplicate / update /
                           unrelated; 'update' inserts the new fact and marks
                           the old one superseded ("meeting moved to 3pm"
                           replaces "meeting at 2pm" instead of coexisting)
  6. field validation     — a malformed email/phone/price/URL/due date is a
                           drop, deterministic and model-free (plan 1.4)
  7. assertion class      — quoted/hypothetical speech is routed to `review`
                           instead of auto-inserted (plan 1.3)

Best-effort by design: when the vector index or the adjudicator model is
unavailable the gate degrades to plain insert — capture must never lose facts
to hygiene infrastructure. Every non-insert verdict is logged to cognition
telemetry (metric: fact_hygiene) so the Console can show the drop/merge rate.

Generic code: thresholds are config; nothing user-specific lives here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.config import settings


@dataclass
class Verdict:
    """What to do with a candidate fact. `action` is one of:
    insert | drop | dedup (refresh dup_fact_id) | supersede (insert, then
    mark each id in supersede_ids replaced by the new row) | review (never
    auto-inserted; a quoted/hypothetical assertion needs a human verdict)."""
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


def _token_jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"\w{3,}", (a or "").lower()))
    tb = set(re.findall(r"\w{3,}", (b or "").lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _range_overlap_frac(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> float:
    """Overlap of two inclusive event-id ranges as a fraction of the SHORTER
    range — a window fully containing a one-event legacy row scores 1.0."""
    inter = min(a_hi, b_hi) - max(a_lo, b_lo) + 1
    if inter <= 0:
        return 0.0
    return inter / min(a_hi - a_lo + 1, b_hi - b_lo + 1)


def _overlap_dup(kind: str, text: str, event_range: tuple[int, int],
                 store) -> Verdict | None:
    """People v3 WS-F: structural dedup across overlapping extraction windows.

    Overlapping source-event ranges mean the two facts cite the same stretch
    of speech (which also implies same session — event ids are shared). The
    token-similarity guard keeps two DIFFERENT facts born from one turn
    ("send the deck" / "book the room" — identical ranges) apart; range
    overlap alone would collapse them. Best-effort: any failure returns None
    and the gate falls through to the embedding check."""
    cfg = settings.facts
    try:
        s = store
        if s is None:
            from app.storage import get_store
            s = get_store()
        lo, hi = int(event_range[0]), int(event_range[1])
        if hi < lo:
            lo, hi = hi, lo
        min_frac = getattr(cfg, "overlap_frac", 0.5)
        min_tok = getattr(cfg, "overlap_token_sim", 0.5)
        for row in s.overlap_fact_candidates(kind, lo, hi):
            frac = _range_overlap_frac(lo, hi, int(row["lo"]), int(row["hi"]))
            if frac < min_frac:
                continue
            if _token_jaccard(text, row.get("text") or "") < min_tok:
                continue
            return Verdict(
                "dedup",
                f"event-range overlap {frac:.2f} vs fact {row['id']}",
                dup_fact_id=int(row["id"]))
    except Exception:
        return None
    return None


# Capitalized runs up to 4 words — candidate entity anchors in a fact text.
_ANCHOR_RE = re.compile(r"\b[A-Z][\w&.-]*(?:\s+[A-Z][\w&.-]*){0,3}")

# Kinds an anchor may resolve to — junk kinds (idea/thing) never anchor.
_ANCHOR_KINDS = ("org", "project", "tool")


def _entity_anchors(text: str, store, limit: int = 2) -> list[int]:
    """Entity ids for known orgs/projects/tools named in `text`.

    Probes possessive-stripped capitalized n-grams (longest first) against
    canonical names + aliases, so "Boost Run's compute" anchors to the
    Boostrun org. Bounded and index-backed — no vocabulary scan."""
    if store is None:
        return []
    seen: list[int] = []
    for run in _ANCHOR_RE.findall(text or ""):
        words = run.split()
        for n in range(min(4, len(words)), 0, -1):
            gram = " ".join(words[:n]).rstrip(".,;:")
            if gram.endswith("'s") or gram.endswith("’s"):
                gram = gram[:-2].rstrip()
            if len(gram) < 3:
                continue
            hit = store.find_entity_matching_name(gram, kinds=_ANCHOR_KINDS)
            if hit is not None:
                if hit["id"] not in seen:
                    seen.append(hit["id"])
                break
        if len(seen) >= limit:
            break
    return seen


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
        from app.services.cog_telemetry import FACT_HYGIENE, cog_telemetry
        cog_telemetry.record(FACT_HYGIENE, action == "insert",
                             action=action, reason=reason, kind=kind,
                             text=(text or "")[:120])
    except Exception:
        pass


_REVIEW_ASSERTIONS = ("quoted", "hypothetical")


def gate_fact(kind: str, text: str, confidence: float | None,
              span: str, source_text: str, *,
              assertion: str | None = None,
              payload: dict | None = None,
              event_range: tuple[int, int] | None = None,
              store=None) -> Verdict:
    """Decide what to do with one candidate fact BEFORE it is persisted.
    Pure decision — the caller (extractor._persist) applies it.

    `event_range` is the (min, max) source event id of the extraction window
    (People v3 WS-F); `store` is only consulted for that structural check and
    falls back to the process store when omitted."""
    cfg = settings.facts
    text = (text or "").strip()
    if not text:
        v = Verdict("drop", "empty text")
        _telemetry(v.action, v.reason, kind, text)
        return v

    if cfg.min_conf > 0 and confidence is not None and confidence < cfg.min_conf:
        v = Verdict("drop", f"confidence {confidence:.2f} < floor {cfg.min_conf}")
        _telemetry(v.action, v.reason, kind, text)
        return v

    if cfg.span_gate and (source_text or "").strip():
        from app.services.cog_telemetry import span_is_faithful
        if not (span or "").strip():
            # Distinct reason: a missing span is an extraction defect (nothing
            # to verify), not a failed verification — keep the two queryable.
            v = Verdict("drop", "empty source_span")
            _telemetry(v.action, v.reason, kind, text)
            return v
        if not span_is_faithful(span, source_text):
            v = Verdict("drop", "source_span is not a verbatim quote")
            _telemetry(v.action, v.reason, kind, text)
            return v

    # Plan 1.4: deterministic field validation (email/phone/price/URL/due)
    # runs before any subjective classing — an objectively malformed field is
    # dropped regardless of assertion class.
    from app.services.validators import validate_fact_fields
    bad_field = validate_fact_fields(kind, text, payload)
    if bad_field:
        v = Verdict("drop", bad_field)
        _telemetry(v.action, v.reason, kind, text)
        return v

    # Plan 1.3: someone else's quote, or a hypothetical, is never auto-inserted
    # as if it were a stated fact — a human has to bless it.
    if assertion in _REVIEW_ASSERTIONS:
        v = Verdict("review", f"assertion={assertion} requires human review")
        _telemetry(v.action, v.reason, kind, text)
        return v

    # People v3 WS-F: structural overlap dedup runs before the embedding
    # check, so the embedding check only sees genuinely distinct candidates.
    # getattr: older test configs (SimpleNamespace) predate these fields.
    if getattr(cfg, "dedup_overlap", False) and event_range is not None:
        v = _overlap_dup(kind, text, event_range, store)
        if v is not None:
            _telemetry(v.action, v.reason, kind, text)
            return v

    # The insert (pass) verdicts are recorded too — telemetry used to fire
    # only on the drop/dedup/review paths, which made fact_hygiene a
    # rejection counter mislabeled as a pass rate: the numerator could never
    # increment, so the console pinned it at 0% forever.
    if not cfg.dedup:
        v = Verdict("insert")
        _telemetry(v.action, "dedup disabled", kind, text)
        return v

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

    # Entity-anchored pass (eval 2026-09-06): a correction can share an
    # entity but almost no wording with the fact it replaces ("letting us
    # use Boost Run's compute … free" vs "Boost Run's free compute is only
    # through November" — cosine below adjudicate_sim, so the adjudicator
    # never saw the pair and the graph kept both). When the new claim names
    # a known org/project/tool, adjudicate its newest active claims too.
    # Bounded: <=2 anchors, <=2 extra adjudications, claims only.
    if kind == "claim" and not superseded and store is not None:
        try:
            already = {fid for fid, _, _ in cands}
            checked = 0
            for eid in _entity_anchors(text, store):
                if checked >= 2:
                    break
                for row in store.recent_claims_about_entity(eid, limit=2):
                    fid = int(row["fact_id"])
                    if fid in already or checked >= 2:
                        continue
                    already.add(fid)
                    checked += 1
                    rel = _adjudicate(kind, row["text"], text)
                    if rel == "duplicate":
                        v = Verdict(
                            "dedup",
                            f"entity-anchored duplicate of fact {fid}",
                            dup_fact_id=fid)
                        _telemetry(v.action, v.reason, kind, text)
                        return v
                    if rel == "update":
                        superseded.append(fid)
        except Exception:
            pass  # anchoring is best-effort; the plain verdict stands

    if superseded:
        v = Verdict("supersede", f"updates fact(s) {superseded}",
                    supersede_ids=tuple(superseded))
        _telemetry(v.action, v.reason, kind, text)
        return v
    v = Verdict("insert")
    _telemetry(v.action, "passed all gates", kind, text)
    return v
