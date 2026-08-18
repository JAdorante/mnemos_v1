"""Unified verdict harvesting — every human judgment becomes a LearningPair.

Workstream A of the learning-loop redesign: the product already collects
ground-truth verdicts on many surfaces (fact review approve/edit/dismiss, chat
👍/👎/✏️, reflection/meta-memory audit items, person merges, KG evidence
adjudications), but only chat escalations ever reached the distill trail. This
module gives every surface one thin call — `learning_store.record*(...)` — that
lands a canonical row in SQLite `learning_pairs` (storage.py), the substrate
the exemplar store (C), shadow eval (B), escalation router (D), and LoRA
curation (E) all read.

Hygiene is enforced at record() time, not by readers:
  * stub-drop      — accepted/edited targets below a per-task-type minimum
  * dedupe         — UNIQUE(task_type, content_hash) in the DB
  * redaction      — same TIER_LOG pass the distill trail uses (no secrets/PII)
  * privacy class  — personal/sensitive/never-send rows are stamped
                     shadow_eligible=0 so Workstream B can never send them
                     to the cloud grader

Invariants: rows carry source_refs (no orphan training data); nothing here is
read by the decide/approval layer (learning affects proposal quality, never
authority — tests/test_learning_store.py asserts the import ban); record()
never raises — a broken learning write must never break a verdict endpoint.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from app.config import settings

# Canonical task types (enum-by-convention; new surfaces add here).
TASK_TYPES = (
    "extraction.task", "extraction.commitment", "extraction.claim",
    "audit.stale_fact", "audit.forget", "brief.section",
    "person.resolution", "escalation.text", "escalation.vision",
)

_VERDICTS = frozenset(
    {"accepted", "edited", "rejected", "dismissed", "shadow_disagree"})

# Verdicts that carry a positive training target (stub-drop applies).
_POSITIVE = frozenset({"accepted", "edited"})

# Per-task-type minimum final_target length for positive verdicts; shorter is
# a stub (a bare "ok", an empty edit) that would teach nothing.
_MIN_TARGET_CHARS: dict[str, int] = {
    "escalation.text": 10,
    "escalation.vision": 10,
    "brief.section": 10,
    "extraction.task": 6,
    "extraction.commitment": 6,
    "extraction.claim": 6,
}
_MIN_TARGET_DEFAULT = 4

# Privacy classes barred from the shadow-eval cloud path (B.2).
_SHADOW_BARRED = frozenset({"personal", "sensitive", "never-send"})


def enabled() -> bool:
    """Env read at call time (meta_memory idiom) so tests and a live console
    toggle take effect without a restart; settings supplies the default."""
    import os
    v = os.environ.get("QUILL_LEARNING")
    if v is not None:
        return v not in ("0", "false", "False")
    return bool(settings.learning.enabled)


def _store(store=None):
    if store is not None:
        return store
    from app.storage import get_store
    return get_store()


def _redact(text: str | None) -> str:
    """TIER_LOG redaction — the same boundary the distill trail enforces."""
    if not text:
        return ""
    from app.perception import redaction
    clean, _hits = redaction.redact_text(str(text), redaction.TIER_LOG)
    return clean


def _privacy(*texts: str | None) -> tuple[str, bool]:
    """(max privacy class over texts, shadow_eligible). Classification failure
    fails CLOSED for the cloud path: unknown → not shadow-eligible."""
    try:
        from app.services.privacy_class import classify_text, max_class
        cls = "internal"
        for t in texts:
            if t:
                cls = max_class(cls, classify_text(str(t)))
        return cls, cls not in _SHADOW_BARRED
    except Exception as exc:
        print(f"[learning_store] privacy classing failed ({exc}) — "
              "marking shadow-ineligible.")
        return "internal", False


def content_hash(task_type: str, input_text: str, final_target: str) -> str:
    h = hashlib.sha256()
    h.update((task_type or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((input_text or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((final_target or "").encode("utf-8"))
    return h.hexdigest()


def record(*, task_type: str, input_text: str, verdict: str,
           verdict_source: str, final_target: str = "",
           local_output: str | None = None, parent_output: str | None = None,
           source_refs: dict | None = None, model_tag: str | None = None,
           human_confirmed: bool = True, created_at: float | None = None,
           store=None) -> str | None:
    """Persist one canonical LearningPair. Returns the pair id, or None when
    disabled / dropped by hygiene / deduped. NEVER raises."""
    try:
        if not enabled():
            return None
        if verdict not in _VERDICTS:
            print(f"[learning_store] unknown verdict {verdict!r} — skipped.")
            return None
        if not (task_type or "").strip() or not (input_text or "").strip():
            return None
        # Stub-drop: a positive verdict must carry a teachable target.
        target = (final_target or "").strip()
        if verdict in _POSITIVE:
            floor = _MIN_TARGET_CHARS.get(task_type, _MIN_TARGET_DEFAULT)
            if len(target) < floor:
                return None
        # Classify on the RAW text FIRST — TIER_LOG redaction strips the very
        # PII the classifier keys on, so classifying afterwards would launder
        # personal rows into shadow eligibility. Then redact at the write
        # boundary (secrets + PII never persist here).
        privacy_cls, shadow_ok = _privacy(input_text, target)
        input_clean = _redact(input_text)
        target_clean = _redact(target)
        row = {
            "id": uuid.uuid4().hex,
            "created_at": float(created_at if created_at is not None
                                else time.time()),
            "task_type": str(task_type),
            "input_text": input_clean,
            "local_output": _redact(local_output) if local_output else None,
            "parent_output": _redact(parent_output) if parent_output else None,
            "final_target": target_clean,
            "verdict": verdict,
            "verdict_source": str(verdict_source or "unknown"),
            "source_refs": dict(source_refs or {}),
            "model_tag": model_tag,
            "embedding_id": None,
            "content_hash": content_hash(task_type, input_clean, target_clean),
            "shadow_eligible": shadow_ok,
            "human_confirmed": bool(human_confirmed),
            "privacy_class": privacy_cls,
        }
        pair_id = _store(store).add_learning_pair(row)
        if pair_id:
            _notify_exemplars(row, store=store)
        return pair_id
    except Exception as exc:
        print(f"[learning_store] record skipped ({exc}).")
        return None


def _notify_exemplars(row: dict, store=None) -> None:
    """Confirmed positive pairs flow straight into the exemplar store (C.2);
    shadow disagreements are offered too and the exemplar store applies its
    own confirmed/autotrust gate (B.4). Best-effort and lazily imported —
    Workstream A works without C."""
    try:
        if row.get("verdict") not in (*_POSITIVE, "shadow_disagree"):
            return
        from app.services import exemplar_store
        exemplar_store.ingest_pair(row, store=store)
    except ImportError:
        pass
    except Exception as exc:
        print(f"[learning_store] exemplar ingest skipped ({exc}).")


def delete(pair_id: str, store=None) -> bool:
    """Hard delete a pair and cascade to its exemplar (Console Delete)."""
    try:
        st = _store(store)
        try:
            from app.services import exemplar_store
            exemplar_store.delete_for_pair(pair_id)
        except ImportError:
            pass
        except Exception as exc:
            print(f"[learning_store] exemplar cascade skipped ({exc}).")
        return st.delete_learning_pair(pair_id)
    except Exception as exc:
        print(f"[learning_store] delete skipped ({exc}).")
        return False


def confirm(pair_id: str, store=None) -> bool:
    """Human confirms a shadow-derived pair (B.4 review card): flips
    human_confirmed and lets it flow to the exemplar store."""
    try:
        st = _store(store)
        ok = st.confirm_learning_pair(pair_id)
        if ok:
            row = st.get_learning_pair(pair_id)
            if row:
                _notify_exemplars(row, store=store)
        return ok
    except Exception as exc:
        print(f"[learning_store] confirm skipped ({exc}).")
        return False


def counts(store=None, *, week_s: float = 7 * 86400.0) -> dict:
    """Learning-tab counter widget: totals + this-week by task_type."""
    try:
        st = _store(store)
        return {
            "total": st.learning_pair_counts(),
            "week": st.learning_pair_counts(since=time.time() - week_s),
        }
    except Exception as exc:
        print(f"[learning_store] counts skipped ({exc}).")
        return {"total": {}, "week": {}}


# --------------------------------------------------------------------------
# Thin per-surface adapters — one call per verdict handler (A.2). Each maps a
# surface's native objects onto the canonical record shape; handlers stay
# logic-free.
# --------------------------------------------------------------------------

def _fact_task_type(fact: dict) -> str:
    kind = str((fact or {}).get("kind") or "").lower()
    if kind == "task":
        return "extraction.task"
    if kind == "commitment":
        return "extraction.commitment"
    return "extraction.claim"


def _fact_input(fact: dict, store) -> str:
    """The extractor's input for this fact: the source event's raw text when
    it survives (reproducible pointer lives in source_refs), else the span."""
    try:
        sev = fact.get("source_event_id")
        if sev and store is not None:
            ev = store.by_ids_map([int(sev)]).get(int(sev))
            if ev is not None:
                raw = getattr(ev, "raw", None) or ""
                if raw:
                    return str(raw)
    except Exception:
        pass
    return str(fact.get("source_span") or fact.get("text") or "")


def record_fact_verdict(fact: dict, verdict: str, *,
                        edited_text: str | None = None, store=None) -> str | None:
    """facts review queue: approve/dismiss/edit on extracted tasks/commitments."""
    st = None
    try:
        st = _store(store)
    except Exception:
        pass
    text = str(fact.get("text") or fact.get("source_span") or "")
    target = (edited_text if verdict == "edited" else
              text if verdict == "accepted" else "")
    return record(
        task_type=_fact_task_type(fact),
        input_text=_fact_input(fact, st),
        local_output=text,
        final_target=target or "",
        verdict=verdict,
        verdict_source="facts.review",
        source_refs={"fact_id": fact.get("id"),
                     "source_event_id": fact.get("source_event_id")},
        store=st,
    )


# Reflection-item kinds → task types. Meta-memory audit kinds get their own
# labels; everything else on that surface is a brief/insight correction.
_REFLECTION_KINDS = {
    "stale_fact": "audit.stale_fact",
    "forget_candidate": "audit.forget",
}


def record_reflection_verdict(item: dict, verdict: str, *,
                              edited_text: str | None = None,
                              store=None) -> str | None:
    """reflection/audit surface: approve/dismiss/edit on insight items."""
    text = str(item.get("text") or "")
    detail = str(item.get("detail") or "")
    target = (edited_text if verdict == "edited" else
              text if verdict == "accepted" else "")
    return record(
        task_type=_REFLECTION_KINDS.get(str(item.get("kind") or ""),
                                        "brief.section"),
        input_text=(text + ("\n" + detail if detail else "")),
        local_output=text,
        final_target=target or "",
        verdict=verdict,
        verdict_source="reflection.review",
        source_refs={"reflection_item_id": item.get("id"),
                     "reflection_id": item.get("reflection_id"),
                     "source_fact_ids": item.get("source_fact_ids") or []},
        store=store,
    )


def record_person_merge(survivor: dict, absorbed: dict, *,
                        merge_id: int | None = None, store=None) -> str | None:
    """People surface: a human-approved soft-merge is ground truth for the
    identity-resolution task."""
    s_name = str((survivor or {}).get("name") or "")
    a_name = str((absorbed or {}).get("name") or "")
    if not s_name or not a_name:
        return None
    return record(
        task_type="person.resolution",
        input_text=f"Are '{a_name}' and '{s_name}' the same person?",
        final_target=f"same_person: '{a_name}' merges into '{s_name}'",
        verdict="accepted",
        verdict_source="people.soft_merge",
        source_refs={"merge_id": merge_id,
                     "survivor_id": (survivor or {}).get("id"),
                     "absorbed_id": (absorbed or {}).get("id")},
        store=store,
    )


def record_kg_evidence_verdict(evidence: dict, predicate: dict, verdict: str,
                               *, store=None) -> str | None:
    """KG evidence drawer: confirm/reject on a claim's evidence row."""
    v = "accepted" if verdict == "confirm" else "rejected"
    pred_text = str((predicate or {}).get("predicate") or "")
    quote = str((evidence or {}).get("quote")
                or (evidence or {}).get("text") or "")
    if not (pred_text or quote):
        return None
    return record(
        task_type="extraction.claim",
        input_text=quote or pred_text,
        local_output=pred_text,
        final_target=pred_text if v == "accepted" else "",
        verdict=v,
        verdict_source="kg.evidence",
        source_refs={"evidence_id": (evidence or {}).get("id"),
                     "predicate_id": (evidence or {}).get("predicate_id")},
        store=store,
    )


def _distill_input(row: dict) -> str:
    """Last user message from a distill row's full-fidelity meta, else the
    truncated prompt head older rows carry."""
    meta = row.get("meta") or {}
    msgs = meta.get("messages")
    if isinstance(msgs, list):
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role", "user") == "user" \
                    and m.get("text"):
                return str(m["text"])
    return str(meta.get("prompt_head") or "")


def record_from_distill(row: dict, outcome: str, *,
                        edited_text: str | None = None,
                        verdict_source: str = "chat.outcome",
                        store=None) -> str | None:
    """Escalation surface: a labeled distill row (chat verdict buttons, or the
    legacy-JSONL backfill) becomes an escalation.* pair."""
    if not row:
        return None
    modality = str(row.get("modality") or "text")
    local_text = str((row.get("local") or {}).get("text") or "")
    parent_text = str((row.get("parent") or {}).get("text") or "")
    if outcome == "edited":
        target = str(edited_text or row.get("edited") or "")
    elif outcome == "accepted":
        target = parent_text or local_text
    else:
        target = ""
    return record(
        task_type=("escalation.vision" if modality == "vision"
                   else "escalation.text"),
        input_text=_distill_input(row),
        local_output=local_text or None,
        parent_output=parent_text or None,
        final_target=target,
        verdict=outcome,
        verdict_source=verdict_source,
        source_refs={"distill_id": row.get("id"),
                     "task": row.get("task"),
                     "reason": row.get("reason"),
                     "frame_path": row.get("frame_path") or None},
        model_tag=row.get("local_model"),
        created_at=row.get("time"),
        store=store,
    )
