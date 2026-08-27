"""M4 — the Brain. RAG over the personal memory timeline.

    User -> Retriever -> Memory Search -> Relevant Memories -> LLM -> Response

PERSONAL facts always come from retrieved memory, never from the model's
weights; general world knowledge (facts, definitions, conversions) is answered
directly — refusing "what's the capital of France?" for lack of a memory
taught nothing and read as broken.

Generation is opt-in via the local-first text tier: with QUILL_TEXT_LOCAL=1 the
answer is drafted by the ModelRouter under task="chat" — local Ollama model
first, Claude on escalation (see model_router.py). With it off (the default,
and always under QUILL_AGENT=0 without it) this stays the retrieval-only stub
and makes ZERO LLM calls.
"""
from __future__ import annotations

from app.services.memory import memory

_SYSTEM = (
    "You are Mnemos, the user's personal AI memory assistant: you observe, "
    "remember, and help act on their life and work, grounded in their own memory "
    "— not a generic chatbot. If asked who or what you are, say so plainly. "
    "The context begins with an 'ABOUT YOU AND THE USER' block that names the "
    "user; use it to answer 'who am I?' and to address them by name when natural. "
    "For questions about the user's life, work, plans, or people they know, "
    "answer ONLY from the retrieved memories — never invent personal facts; if "
    "the memories don't cover it, say you don't have a memory of it. For general "
    "knowledge (world facts, definitions, conversions, how-things-work), answer "
    "directly and briefly — no memory is needed for those. Be concise and factual. "
    "When the context includes RIGHT NOW (user's local time) and task/commitment "
    "due dates, use that clock to judge overdue / due today / this week — do not "
    "guess the date."
)


def answer(question: str) -> dict:
    """Retrieve (structured layers first — see services/grounding.py), then
    (when text routing is enabled) generate local-first; any failure degrades
    to the retrieval-only placeholder."""
    sources: list = []
    retrieval: dict | None = None
    try:
        from app.services.grounding import compose
        g = compose(question, semantic_limit=8)
        context, hits = g["block"], g["hits"]
        sources = g.get("sources") or []
    except Exception as exc:
        print(f"[llm] structured grounding skipped ({exc}); flat search.")
        hits = memory.search(question, limit=8)
        context = "\n".join(f"- {h['raw']}" for h in hits)
    # D.2b escalation-router features: "did retrieval find the answer?" is the
    # strongest predictor of whether a grounded LOCAL answer can succeed, and
    # it is only measurable here, before the call. Best-effort — a routing
    # feature must never be able to break the answer path.
    try:
        from app.services.router_train import retrieval_stats
        retrieval = retrieval_stats(question, hits=hits, block=context)
    except Exception as exc:
        print(f"[llm] retrieval stats skipped ({exc}).")
    out = {
        "question": question,
        "retrieved": hits,
        "sources": sources,
        "answer": (
            "[LLM not wired yet] Retrieved "
            f"{len(hits)} memories that match. Context:\n{context}"
        ),
    }
    try:
        from app.config import settings

        if settings.text_local.enabled:
            from app.services.model_router import router

            system = _SYSTEM
            try:
                from app.services.clock import clock_instruction
                system = system + "\n\n" + clock_instruction()
            except Exception:
                pass
            reply = router.complete(
                "chat", system=system,
                messages=[{"role": "user", "content":
                           f"Retrieved memories:\n{context or '(none)'}\n\n"
                           f"Question: {question}"}],
                max_tokens=1024,
                retrieval=retrieval,
            ).strip()
            if reply:
                out["answer"] = reply
    except Exception as exc:
        print(f"[llm] generation skipped ({exc}); returning retrieval-only answer.")
    # Deterministic answer-check (plan 3.2) — fabricated name/date/price tokens
    # against the retrieval block downgrade to an evidence dump.
    try:
        from app.services.answer_check import check_answer
        checked = check_answer(
            out.get("answer") or "",
            context or "",
            question=question,
            sources=sources,
        )
        out["answer"] = checked.text
        out["answer_check"] = checked.to_dict()
    except Exception as exc:
        print(f"[llm] answer_check skipped ({exc}).")
    return out
