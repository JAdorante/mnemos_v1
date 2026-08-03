"""Retrieval-based few-shot correction — learning without training (Phase 1).

Before a local text attempt, retrieve the most similar past escalations whose
parent answer a human verified (user_outcome accepted/edited — an `edited` row's
corrected text beats the parent's raw output) and inject them into the local
model's prompt as worked examples. Small models improve dramatically from
in-context examples of their own past failure modes, and the effect starts the
moment a mistake is labeled — no training stack, no retrain cycle.

Deliberately generic: this module is a "retrieve similar corrected examples"
step and nothing more. All user-specificity lives in the distill JSONL it reads;
none is encoded here. Retrieval is same-task only (different tasks have
different output formats), and every failure path degrades to "no examples" —
the local call must never break because recall did.

Measure the effect in /console/models: escalation-rate drop per task, joined
against the `fewshot_n` the distill rows now carry.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from app.config import settings

# Only rows a human vouched for are worth teaching from.
_TRUSTED = frozenset({"accepted", "edited"})

# Refusal-shaped answers are CORRECT responses but POISONOUS examples: injected
# as exemplars they teach the model to refuse regardless of what the context
# holds (observed live 2026-07-17 — accepted "I don't have a memory of that"
# rows made the model refuse questions whose grounding block contained the
# answer, and the consistency floor then CONFIRMED the refusal). The system
# prompt already teaches honest refusal; examples of it add nothing.
_REFUSALISH = re.compile(
    r"\b(?:(?:don'?t|do not|doesn'?t|does not) have (?:any|enough|that|a|"
    r"relevant|information|memor)|no (?:information|memor(?:y|ies)|record)|"
    r"i don'?t know|not (?:enough|sufficient) (?:information|context))",
    re.I)

# Per-side cap INSIDE the rendered block only (storage stays full-fidelity):
# a 3B model's context is the scarce resource the examples spend.
_MAX_EXAMPLE_CHARS = 1500


def _clip(text: str, limit: int = _MAX_EXAMPLE_CHARS) -> str:
    t = text or ""
    return t if len(t) <= limit else t[:limit] + "…"


def _embed_many(texts: list[str]):
    """Indirection over the shared embedder so tests can patch it."""
    from app.services.embeddings import embedder
    return embedder.encode_many(texts)


def query_text(messages: list | None) -> str:
    """The full last user message — what identifies the call (unclipped twin
    of model_router._prompt_head)."""
    if not messages:
        return ""
    from app.services.ollama_text import _flatten
    for m in reversed(messages):
        if m.get("role", "user") == "user":
            return _flatten(m.get("content"))
    return ""


# Line prefixes our RAG call sites use to introduce the actual ask after the
# injected context block (llm.py "Question:", the agent's "User:", planner
# "Current task:"). Lowercase; matched against line starts, last hit wins.
_QUERY_PREFIXES = ("question:", "user:", "current task:")


def query_focus(text: str) -> str:
    """The question part of a composed RAG prompt — the text from the LAST
    query-marker line onward. Callers prepend retrieved memories before the
    ask, and embedding the whole message let the CONTEXT dominate similarity
    (two unrelated questions over similar memories looked alike; the same
    question over different memories looked different). Prompts without a
    marker pass through whole, so plain callers are unaffected."""
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        low = lines[i].lstrip().lower()
        for p in _QUERY_PREFIXES:
            if low.startswith(p):
                first = lines[i].lstrip()[len(p):].strip()
                tail = "\n".join([first] + lines[i + 1:]).strip()
                return tail or (text or "")
    return text or ""


def _row_prompt(row: dict) -> str:
    """Best available prompt text for a row: full-fidelity messages when the
    row has them, else the truncated prompt_head older rows carry."""
    meta = row.get("meta") or {}
    msgs = meta.get("messages")
    if isinstance(msgs, list):
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role", "user") == "user" and m.get("text"):
                return str(m["text"])
    return str(meta.get("prompt_head") or "")


def _row_answer(row: dict) -> str:
    """The verified answer for a row: human-corrected text when present
    (strongest signal), else the parent output. Local-kept rows have no parent
    side — there a 👍 (accepted) verifies the LOCAL text itself, which is safe
    to teach from precisely because a human vouched for it."""
    edited = row.get("edited")
    if edited:
        return str(edited)
    parent = str((row.get("parent") or {}).get("text") or "")
    if parent:
        return parent
    if row.get("user_outcome") == "accepted":
        return str((row.get("local") or {}).get("text") or "")
    return ""


class FewShotRecall:
    """Similarity index over trusted (prompt → verified answer) distill pairs.

    Rebuilt lazily whenever the trail file changes (mtime+size stamp);
    embeddings are cached per row id so a rebuild only embeds new rows.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._entries: list[dict[str, Any]] = []
        self._vec_cache: dict[str, Any] = {}

    def _path(self) -> Path:
        return Path(settings.escalate_log.path)

    def _load(self) -> list[dict[str, Any]]:
        """Return indexed entries, rebuilding if the trail changed. Never raises."""
        path = self._path()
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return []
        with self._lock:
            if stamp == self._stamp:
                return self._entries
            entries: list[dict[str, Any]] = []
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                print(f"[few_shot] trail read skipped ({exc}).")
                return self._entries
            for ln in lines:
                if not ln.strip():
                    continue
                try:
                    row = json.loads(ln)
                except Exception:
                    continue
                if row.get("modality") != "text":
                    continue
                if row.get("user_outcome") not in _TRUSTED:
                    continue
                prompt, answer = _row_prompt(row), _row_answer(row)
                if not prompt or not answer:
                    continue
                if _REFUSALISH.search(answer):
                    continue        # correct answer, poisonous example
                entries.append({
                    "id": str(row.get("id") or f"line:{len(entries)}:{hash(ln)}"),
                    "task": str(row.get("task") or ""),
                    "prompt": prompt,
                    # The question alone drives similarity AND the rendered
                    # example — injected RAG context is noise on both counts.
                    "focus": query_focus(prompt),
                    "answer": answer,
                    "outcome": str(row.get("user_outcome")),
                })
            fresh = [e for e in entries if e["id"] not in self._vec_cache]
            if fresh:
                try:
                    vecs = _embed_many([e["focus"] for e in fresh])
                    for e, v in zip(fresh, vecs):
                        self._vec_cache[e["id"]] = v
                except Exception as exc:
                    print(f"[few_shot] embed skipped ({exc}).")
                    return self._entries
            for e in entries:
                e["vec"] = self._vec_cache[e["id"]]
            self._entries, self._stamp = entries, stamp
            return entries

    def examples(self, task: str, messages: list | None, *,
                 k: int, min_sim: float,
                 exclude_ids: frozenset[str] | set[str] = frozenset(),
                 ) -> list[dict[str, Any]]:
        """Top-k same-task trusted examples above the similarity floor, most
        similar first. Empty list on any miss/failure — never raises.

        `exclude_ids` bars specific rows from retrieval — the eval harness's
        leave-one-out/holdout guard (a row must never be its own worked
        example, or the benchmark scores contamination, not skill)."""
        if k <= 0:
            return []
        prompt = query_focus(query_text(messages))
        if not prompt:
            return []
        pool = [e for e in self._load()
                if e["task"] == task and e["id"] not in exclude_ids]
        if not pool:
            return []
        try:
            import numpy as np
            q = _embed_many([prompt])[0]
            scored = sorted(
                ((float(np.dot(q, e["vec"])), e) for e in pool),
                key=lambda t: t[0], reverse=True)
        except Exception as exc:
            print(f"[few_shot] recall skipped ({exc}).")
            return []
        return [
            {"id": e["id"], "prompt": e["focus"], "answer": e["answer"],
             "outcome": e["outcome"], "sim": round(sim, 4)}
            for sim, e in scored[:k] if sim >= min_sim
        ]

    def render(self, examples: list[dict[str, Any]], *,
               confidence_line: bool = False) -> str:
        """Prompt block to append to the LOCAL system prompt. Empty in → empty out.

        `confidence_line` (plain-text calls only, NOT schema mode): stamp each
        example with `CONFIDENCE: 0.9`. Small models imitate examples over
        instructions — bare examples silently taught the model to DROP its
        confidence trailer, which reads as "unsure" and forces an escalation
        even when the answer was right. A verified answer is an honest 0.9."""
        if not examples:
            return ""
        parts = [
            "\n\n---\nVERIFIED EXAMPLES: on similar past requests your answer "
            "needed correcting; each verified answer below was confirmed by the "
            "user. Match their approach, level of detail, and format.",
        ]
        for i, ex in enumerate(examples, 1):
            parts.append(
                f"\nExample {i}:\nREQUEST: {_clip(ex['prompt'])}\n"
                f"VERIFIED ANSWER: {_clip(ex['answer'])}"
                + ("\nCONFIDENCE: 0.9" if confidence_line else "")
            )
        return "\n".join(parts)


few_shot = FewShotRecall()
