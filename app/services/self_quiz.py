"""Self-quiz — the flywheel stage that turns trusted memories into training data.

The system tests its LOCAL model against its own human-verified knowledge: each
approved/edited fact becomes a question (written by the local model itself), is
answered through the same RAG shape as live chat, and is auto-scored against
the stored fact with the bench's embedding-similarity measure. Failures become
distill rows whose gold already exists — "learning from its mistakes" with no
human labeling session, ever (see onboarding constraint: day-one users train
nothing; this runs idle on whatever their onboarding form + usage produced).

Guardrails (from the generalization review):
  * The stored FACT is always the gold — never the model's own output, so the
    model cannot distill on itself.
  * Only FAILURES write training rows (auto-labeled `edited` with the fact as
    the corrected answer — trust is justified because a human approved the
    fact; `source="self_quiz"` + meta.auto=True mark provenance so curation
    can cap the auto share against human-verified rows).
  * Entirely local: question generation + answering on the Ollama model,
    scoring on the shared embedder. Zero Claude calls, zero cost. Harmless on
    an empty memory (asks nothing).

Generic code: every question, answer, and gold comes from THIS install's own
memory store. Nothing user-specific lives here.
"""
from __future__ import annotations

import json
from typing import Any, Callable

_QGEN_SYSTEM = (
    "You write quiz questions. Given a personal note from the user's memory, "
    "reply with ONE short question the note's OWNER might later ask their "
    "assistant, answerable by that note. The question must NOT contain the "
    "note's key details (no names, numbers, or outcomes from it — those are "
    "the answer). Reply with ONLY the question."
)

# Guard against qgen inventing questions the fact CANNOT answer (observed
# live: fact "The user's name is Justin Adorante" produced "What type of
# vehicle did Justin request?" — grading against a mismatched gold writes
# non-sequitur training pairs). Before quizzing, ask the model to answer the
# question FROM THE NOTE ALONE; an UNANSWERABLE verdict skips the question.
_ANSWERABLE_SYSTEM = (
    "Given a note and a question, answer the question using ONLY the note. "
    "If the note does not actually contain the answer, reply with exactly "
    "one word: UNANSWERABLE."
)

# A question that echoes the fact leaks the answer — the quiz would measure
# copying, not grounding. Normalized-substring check; cheap and sufficient.
def _leaks(question: str, fact: str) -> bool:
    q = " ".join((question or "").lower().split())
    f = " ".join((fact or "").lower().split())
    return bool(f) and (f in q or q in f)


def _sim(a: str, b: str) -> float:
    import numpy as np
    from app.services.embeddings import embedder
    v = embedder.encode_many([a or "", b or ""])
    return float(np.dot(v[0], v[1]))


def _trusted_facts(limit: int) -> list[dict]:
    """Human-verified facts, newest first (onboarding claims land here too)."""
    from app.services.memory import memory
    store = memory._ensure_store()
    rows = (store.list_facts(review="approved", limit=limit)
            + store.list_facts(review="edited", limit=limit))
    rows.sort(key=lambda r: r.get("extracted_at") or 0, reverse=True)
    return rows[:limit]


def _quizzed_fact_ids() -> set:
    """Fact ids with a LIVE self-quiz row — re-quizzing those writes
    near-duplicate lessons. Rejected quiz rows don't count: rejection voids
    the row (e.g. a bad generated question), so the fact is fair game again."""
    from app.services.escalate_log import escalate_log
    ids: set = set()
    try:
        path = escalate_log.path
        if not path.is_file():
            return ids
        for ln in path.read_text(encoding="utf-8-sig").splitlines():
            if '"self_quiz' not in ln:
                continue
            try:
                row = json.loads(ln)
            except Exception:
                continue
            if row.get("user_outcome") == "rejected":
                continue
            fid = ((row.get("meta") or {}).get("quiz") or {}).get("fact_id")
            if fid is not None:
                ids.add(fid)
    except Exception as exc:
        print(f"[self_quiz] quizzed-id scan skipped ({exc}).")
    return ids


def _rag_call(local, question: str) -> tuple[str, float | None, str, list]:
    """Answer `question` exactly the way live chat would (same system, same
    structured grounding) on the LOCAL model. Returns (text, conf, system,
    messages)."""
    from app.services.grounding import compose
    from app.services.llm import _SYSTEM
    # record_attention=False: quiz questions are machine-generated — they must
    # not write attention impressions or count as field misses.
    context = compose(question, semantic_limit=8, record_attention=False)["block"]
    messages = [{"role": "user", "content":
                 f"Retrieved memories:\n{context or '(none)'}\n\n"
                 f"Question: {question}"}]
    res = local.complete("self_quiz", system=_SYSTEM, messages=messages)
    return res.get("text") or "", res.get("confidence"), _SYSTEM, messages


def _record_failure(*, fact: dict, question: str, answer: str,
                    conf: float | None, sim: float,
                    system: str, messages: list) -> str | None:
    """One training row for a failed quiz item: gold = the trusted fact."""
    from app.services.escalate_log import escalate_log
    from app.services.ollama_text import _flatten
    row = escalate_log.record(
        task="chat",                      # chat-shaped → retrievable as chat few-shot
        reason="self_quiz_failure",
        local={"text": answer, "json": None, "confidence": conf},
        parent={"text": str(fact.get("text") or "")},
        local_model=getattr(local_model_holder, "model", None) or "",
        parent_model="memory_store",      # the answer key, not an LLM
        source="self_quiz",
        modality="text",
        meta={
            "prompt_head": question[:500],
            "system": system,
            "messages": [{"role": m.get("role", "user"),
                          "text": _flatten(m.get("content"))} for m in messages],
            "auto": True,
            "quiz": {"sim": round(sim, 4), "fact_id": fact.get("id")},
        },
    )
    if not row:
        return None
    # Auto-verdict: the fact is human-approved, so the corrected answer is
    # trusted by construction. meta.auto lets curation cap the auto share.
    escalate_log.set_user_outcome("edited", row_id=row["id"],
                                  edited_text=str(fact.get("text") or ""))
    return row["id"]


class _LocalHolder:
    model: str | None = None


local_model_holder = _LocalHolder()   # lets _record_failure name the model


def run_quiz(*, limit: int = 20, pass_sim: float = 0.6, model: str | None = None,
             dry_run: bool = False,
             facts: list[dict] | None = None,
             local=None,
             sim_fn: Callable[[str, str], float] | None = None) -> dict[str, Any]:
    """Quiz the local model on trusted memories; write failure training rows.

    `facts`/`local`/`sim_fn` are injectable for tests; defaults use the real
    store, the configured Ollama model, and the shared embedder. Never raises
    for per-item failures — a broken item is skipped and counted."""
    sim_fn = sim_fn or _sim
    if local is None:
        from app.services.ollama_text import OllamaText
        local = OllamaText(model=model)
        if not local.available():
            return {"ok": False, "reason": "local_unavailable",
                    "model": local.model}
    local_model_holder.model = getattr(local, "model", None)
    if facts is None:
        facts = _trusted_facts(limit)
    facts = facts[:limit]
    quizzed = _quizzed_fact_ids()
    stats: dict[str, Any] = {"ok": True, "model": getattr(local, "model", "?"),
                             "facts": len(facts), "asked": 0, "passed": 0,
                             "failed": 0, "rows_written": 0,
                             "skipped_qgen": 0, "skipped_quizzed": 0,
                             "errors": 0, "sims": [], "dry_run": dry_run}
    consecutive_errors = 0
    for fact in facts:
        fact_text = str(fact.get("text") or "").strip()
        if not fact_text:
            continue
        if fact.get("id") in quizzed:
            stats["skipped_quizzed"] += 1     # its lesson row already exists
            continue
        try:
            q_res = local.complete(
                "self_quiz", system=_QGEN_SYSTEM,
                messages=[{"role": "user", "content": "Note: " + fact_text}])
            question = (q_res.get("text") or "").strip().splitlines()[0].strip() \
                if (q_res.get("text") or "").strip() else ""
            if not question or _leaks(question, fact_text):
                stats["skipped_qgen"] += 1
                continue
            # Answerability probe: the note must actually answer the question,
            # or the graded gold would be a non-sequitur training pair.
            p_res = local.complete(
                "self_quiz", system=_ANSWERABLE_SYSTEM,
                messages=[{"role": "user", "content":
                           f"Note: {fact_text}\n\nQuestion: {question}"}])
            p_text = (p_res.get("text") or "").strip()
            if not p_text or "UNANSWERABLE" in p_text.upper():
                stats["skipped_qgen"] += 1
                continue
            answer, conf, system, messages = _rag_call(local, question)
            sim = sim_fn(answer, fact_text)
            consecutive_errors = 0
        except Exception as exc:
            stats["errors"] += 1
            consecutive_errors += 1
            print(f"[self_quiz] item skipped ({exc}).")
            if consecutive_errors >= 3:
                stats["ok"] = False
                stats["reason"] = "too_many_errors"
                break
            continue
        stats["asked"] += 1
        stats["sims"].append(round(sim, 4))
        if sim >= pass_sim:
            stats["passed"] += 1
            continue                       # successes are stats, never data
        stats["failed"] += 1
        if not dry_run:
            rid = _record_failure(fact=fact, question=question, answer=answer,
                                  conf=conf, sim=sim, system=system,
                                  messages=messages)
            if rid:
                stats["rows_written"] += 1
    if stats["sims"]:
        stats["mean_sim"] = round(sum(stats["sims"]) / len(stats["sims"]), 4)
    return stats
