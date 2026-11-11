"""The local section classifier for brains (step 5 of the brains spec).

Two ways a fact gets a section hint: the extractor fills `section_hint`
on new claims when QUILL_EXTRACT_SECTION_HINT=1, and `backlog_pass()`
labels what is already stored. Both write `facts.section_hint`; the
compiler (services/brains.py) honours the hint only after every
deterministic rule had its turn, and a user move always outranks it.

This module talks to the local text model directly (OllamaText), never
through the router, so a classification can never escalate to a cloud
model: when the local model is down the pass reports `unavailable` and
writes nothing.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services.extractor import SECTION_HINTS

TASK = "brain_section"
BATCH = 12
_SYSTEM = (
    "You file short facts about a business subject into one section each. "
    "Sections: thesis (why the deal or company is attractive: upside, moat, "
    "economics), status (where things stand right now), commitment (someone "
    "owes someone a deliverable), risk (a concern, objection or exposure), "
    "competitor (a rival or alternative), decision (a choice that was made), "
    "question (something still open or unknown), role (a person's title or "
    "employer), relationship (who works with whom), performance (numbers: "
    "revenue, growth, margins, headcount), other (none of these). "
    "Answer with JSON {\"labels\": [...]} giving exactly one label per fact, "
    "in order. Use only the facts given; never add text."
)
_SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array",
                              "items": {"type": "string", "enum": list(SECTION_HINTS)}}},
    "required": ["labels"],
    "additionalProperties": False,
}

_client: Any = None


def _local():
    """The local text client (module seam so tests can hand in a fake)."""
    global _client
    if _client is None:
        from app.services.ollama_text import OllamaText
        _client = OllamaText()
    return _client


def available() -> bool:
    try:
        return bool(_local().available())
    except Exception:
        return False


def classify(texts: list[str], *, subject: str = "", btype: str = "") -> list[str | None]:
    """One label per text (None when the model gave nothing usable)."""
    if not texts:
        return []
    numbered = "\n".join(f"{i + 1}. {t.strip()}" for i, t in enumerate(texts))
    head = f"Subject: {subject} ({btype}).\n" if subject else ""
    out: list[str | None] = [None] * len(texts)
    try:
        res = _local().complete(
            TASK, system=_SYSTEM,
            messages=[{"role": "user", "content": head + "Facts:\n" + numbered}],
            max_tokens=48 + 8 * len(texts), schema=_SCHEMA)
    except Exception as exc:
        print(f"[brain_classifier] local call failed ({exc}).")
        return out
    labels = None
    if isinstance(res, dict):
        j = res.get("json")
        if isinstance(j, dict):
            labels = j.get("labels")
        if labels is None and res.get("text"):
            try:
                labels = (json.loads(res["text"]) or {}).get("labels")
            except Exception:
                labels = None
    if not isinstance(labels, list):
        return out
    for i, lab in enumerate(labels[:len(texts)]):
        lab = str(lab or "").strip().lower()
        out[i] = lab if lab in SECTION_HINTS else None
    return out


def backlog_pass(store=None, *, limit: int = 200, batch: int = BATCH,
                 now: float | None = None) -> dict:
    """Label up to `limit` unassigned facts across every brain with the
    local model, write the hints, recompile the brains they belong to."""
    from app.services import brains
    from app.storage import get_store
    store = store or get_store()
    now = float(now if now is not None else time.time())
    if not available():
        return {"ok": False, "unavailable": True, "classified": 0,
                "considered": 0, "subjects": 0}
    idx = brains._Index(store, now)
    todo = brains.unassigned_facts(store, idx)
    hints: dict[int, str] = {}
    considered = 0
    touched: set[tuple[str, int]] = set()
    seen_facts: set[int] = set()
    for key, items in sorted(todo.items()):
        label = idx.label(*key)
        btype = brains.brain_type(idx, *key) or ""
        pending = [it for it in items if int(it["item_id"]) not in seen_facts
                   and not it["tags"].get("section_hint")]
        for i in range(0, len(pending), batch):
            if considered >= limit:
                break
            chunk = pending[i:i + batch][:max(0, limit - considered)]
            considered += len(chunk)
            labels = classify([it["text"] for it in chunk], subject=label, btype=btype)
            for it, lab in zip(chunk, labels):
                fid = int(it["item_id"])
                seen_facts.add(fid)
                if lab:
                    hints[fid] = lab
                    touched.add(key)
        if considered >= limit:
            break
    written = store.set_fact_section_hints(hints) if hints else 0
    if touched:
        brains.rebuild(store, scope="dirty", subjects_only=sorted(touched))
    remaining = sum(len(v) for v in todo.values()) - considered
    return {"ok": True, "unavailable": False, "classified": written,
            "considered": considered, "subjects": len(touched),
            "remaining": max(0, remaining)}
