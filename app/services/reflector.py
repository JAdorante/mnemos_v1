"""Reflection — turn stored facts into durable personal intelligence.

The extractor answers "what was said?"; reflection answers "what changed, what
matters, what is unresolved, and what should happen next?" over a period. It is a
NEW subsystem, not a better summarizer: it reads the facts/tasks/commitments the
pipeline already produced and emits structured, individually-reviewable insights.

Design choices that keep it honest (same posture as the facts layer):

  * Grounded. The model may only cite fact ids we hand it; any invented id is
    dropped before persistence (`_ground`). No ungrounded oracle.
  * Reviewable. Each insight is one `reflection_items` row — approve / edit /
    dismiss / convert-to-task, exactly like a fact. Nothing auto-mutates tasks;
    a recommendation becomes a task only when a human converts it.
  * Precomputed packet. We assemble a compact, high-signal packet (recent facts +
    open loops + prior summary) in code; the model interprets it. The store
    prepares context, the LLM reflects over it.

Model lives behind one swappable constant (`REFLECT_MODEL`) so the ModelRouter
can route this boundary later without touching the reflector.
"""
from __future__ import annotations

import os
import time

from app.storage import Store, get_store

# The one model boundary for reflection — route it here later (ModelRouter).
REFLECT_MODEL = os.environ.get("QUILL_REFLECT_MODEL", "claude-opus-4-8")

# Bounds so the packet stays compact (token-cheap) even on a busy day.
MAX_RECENT = int(os.environ.get("QUILL_REFLECT_MAX_RECENT", "120"))
MAX_OPEN = int(os.environ.get("QUILL_REFLECT_MAX_OPEN", "40"))

# The insight taxonomy. Wide from day one so weekly/monthly/project/person
# reflection is additive later; daily v1 emits mostly the first six.
_KINDS = {
    "change", "pattern", "risk", "open_loop",
    "project_update", "relationship_update", "policy", "recommendation",
    # Track A4 meta-memory audits (review-first except risk urgency auto-apply)
    "stale_fact", "forget_candidate", "dropped_thread", "fading_idea",
    "open_question", "weakening_relationship",
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One or two sentences: what this period was actually "
            "about and what shifted. Not a list of topics — a synthesis.",
        },
        "confidence": {
            "type": "number",
            "description": "0-1: overall confidence this reflection is grounded and useful.",
        },
        "items": {
            "type": "array",
            "description": "Distinct, grounded insights. Each MUST trace to the "
            "provided facts. Prefer a few sharp insights over many obvious ones.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": sorted(_KINDS),
                        "description": "change=something moved; pattern=a recurring "
                        "theme; risk=an unresolved/high-risk item; open_loop=an aging "
                        "commitment or task needing follow-up; project_update / "
                        "relationship_update=movement on a project or with a person; "
                        "policy=a candidate learned preference about how the user "
                        "works; recommendation=a concrete next action.",
                    },
                    "text": {"type": "string", "description": "The insight, stated plainly."},
                    "detail": {"type": "string", "description": "The 'why' or the recommended action. May be empty."},
                    "subject": {"type": "string", "description": "The person or project this is about, if any. Else ''."},
                    "confidence": {"type": "number", "description": "0-1 for this specific insight."},
                    "source_fact_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "The fact ids (the [bracketed] numbers) this "
                        "insight is grounded in. Cite only ids present in the packet.",
                    },
                },
                "required": ["kind", "text", "detail", "subject", "confidence", "source_fact_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "confidence", "items"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are vinceo.ai's reflection engine. You are given a packet of facts vinceo.ai "
    "learned over a period (each tagged with a [fact_id]), the currently open "
    "loops (tasks/commitments, some aging), and the prior reflection's summary. "
    "Produce a grounded reflection: what changed, what matters, what is "
    "unresolved, and what should happen next.\n\n"
    "Rules:\n"
    "- Ground every insight in the packet. `source_fact_ids` MUST be ids that "
    "appear in the packet — never invent an id. If an insight has no supporting "
    "fact id, do not emit it.\n"
    "- Synthesize, don't list. 'You discussed X, Y, Z' is not an insight. 'The "
    "recurring theme this week was local-first inference; the unresolved risk is "
    "whether the graph can represent long-running projects' is.\n"
    "- Flag aging open loops as `open_loop` items with a recommended follow-up.\n"
    "- A `policy` is a tentative learned preference about how the user works "
    "(e.g. 'prefers concise investor emails'). Mark it low-confidence — it is "
    "proposed, not accepted.\n"
    "- Prefer a few high-signal insights over many obvious ones. If the period "
    "was quiet, it is correct to return a short summary and few or no items.\n"
    "- Be specific and honest about uncertainty via the confidence fields."
)


def _fmt_when(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%b %d %I:%M%p", time.localtime(ts)).replace(" 0", " ")
    except (ValueError, TypeError, OSError):
        return ""


def _fmt_fact(f: dict) -> str:
    """One compact line for the packet: '[123] task: "…" — owner: Sam, due: Fri (Jul 07)'."""
    fid = f.get("fact_id")
    kind = f.get("kind") or "fact"
    text = (f.get("text") or f.get("source_span") or "").strip().replace("\n", " ")
    parts = []
    if kind == "commitment":
        who = " → ".join(x for x in (f.get("from_person"), f.get("to_person")) if x)
        if who:
            parts.append(who)
    elif f.get("owner"):
        parts.append(f"owner: {f['owner']}")
    if f.get("due"):
        parts.append(f"due: {f['due']}")
    when = _fmt_when(f.get("source_time") or f.get("extracted_at"))
    if when:
        parts.append(when)
    tail = (" — " + ", ".join(parts)) if parts else ""
    return f'[{fid}] {kind}: "{text}"{tail}'


class Reflector:
    def __init__(self, store: Store | None = None) -> None:
        self._store = store

    def _s(self) -> Store:
        if self._store is None:
            self._store = get_store()
        return self._store

    # --- packet assembly (the store prepares the context) -----------------
    def _gather(self, since: float, now: float) -> tuple[str, set[int], dict]:
        store = self._s()
        recent = store.facts_since(since, limit=MAX_RECENT)
        open_tasks = store.open_tasks(limit=MAX_OPEN)
        open_comms = store.list_facts(kind="commitment", status="open", limit=MAX_OPEN)

        allowed: set[int] = set()
        lines_recent, lines_open = [], []

        for f in recent:
            fid = f.get("fact_id")
            if fid:
                allowed.add(int(fid))
            lines_recent.append(_fmt_fact(f))

        def _age(f: dict) -> str:
            ex = f.get("extracted_at")
            if not ex:
                return ""
            days = (now - ex) / 86400.0
            return f"aging {days:.0f}d" if days >= 1 else "today"

        for t in open_tasks:
            fid = t.get("fact_id")
            if fid:
                allowed.add(int(fid))
            due = f", due: {t['due']}" if t.get("due") else ""
            lines_open.append(f'[{fid}] task (open, {_age(t)}): "{t.get("text","")}"{due}')
        for c in open_comms:
            fid = c.get("fact_id")
            if fid:
                allowed.add(int(fid))
            who = " → ".join(x for x in (c.get("from_person"), c.get("to_person")) if x)
            due = f", due: {c['due']}" if c.get("due") else ""
            lines_open.append(
                f'[{fid}] commitment (open, {_age(c)}): "{c.get("text","")}"'
                f'{(" — " + who) if who else ""}{due}')

        prior = store.latest_reflection("daily")
        prior_line = (prior.get("summary") or "").strip() if prior else ""

        packet = []
        window_h = round((now - since) / 3600)
        packet.append(f"Period: last {window_h}h. Facts learned this period: "
                      f"{len(lines_recent)}. Open loops: {len(lines_open)}.")
        if prior_line:
            packet.append(f"\nPrior reflection summary (for continuity):\n{prior_line}")
        packet.append("\nFACTS LEARNED THIS PERIOD:\n"
                      + ("\n".join(lines_recent) if lines_recent else "(none)"))
        packet.append("\nCURRENTLY OPEN LOOPS (tasks & commitments; may predate this period):\n"
                      + ("\n".join(lines_open) if lines_open else "(none)"))
        counts = {"recent_facts": len(lines_recent), "open_loops": len(lines_open)}
        return "\n".join(packet), allowed, counts

    def _ground(self, ids, allowed: set[int]) -> list[int]:
        """Keep only cited ids that were actually in the packet — drop any the
        model invented. This is what keeps a reflection auditable, not an oracle."""
        out = []
        for i in ids or []:
            try:
                n = int(i)
            except (TypeError, ValueError):
                continue
            if n in allowed and n not in out:
                out.append(n)
        return out

    # --- public: run one daily reflection ---------------------------------
    def reflect_daily(self, *, period_hours: float = 24.0,
                      verbose: bool = False) -> dict:
        """Reflect over the last `period_hours` of learning. Writes one
        `reflections` header + one `reflection_items` row per grounded insight.
        No-ops (no LLM call) when the period had no activity."""
        store = self._s()
        now = time.time()
        since = now - period_hours * 3600.0
        packet, allowed, counts = self._gather(since, now)

        if not allowed:
            return {"skipped": "no activity in period", "reflection_id": None,
                    "items": 0, **counts}

        from app.services.model_router import router
        out = router.complete_json(
            "reflect", system=_SYSTEM,
            messages=[{"role": "user", "content": packet}],
            schema=_SCHEMA, max_tokens=2048, model=REFLECT_MODEL,
        )

        summary = (out.get("summary") or "").strip()
        rid = store.add_reflection(
            scope="daily", subject_type="global", period_start=since,
            period_end=now, summary=summary, model=REFLECT_MODEL,
            confidence=out.get("confidence"), created_at=now,
        )
        n = kept = 0
        for it in out.get("items", []):
            text = (it.get("text") or "").strip()
            if not text:
                continue
            kind = it.get("kind") if it.get("kind") in _KINDS else "change"
            ids = self._ground(it.get("source_fact_ids"), allowed)
            store.add_reflection_item(
                rid, kind=kind, text=text, detail=(it.get("detail") or "").strip(),
                subject=(it.get("subject") or "").strip(),
                confidence=it.get("confidence"), source_fact_ids=ids, created_at=now,
            )
            n += 1
            kept += len(ids)
            if verbose:
                print(f"  [{kind}] {text[:80]!r}  cites={ids}")
        return {"reflection_id": rid, "summary": summary, "items": n,
                "grounded_citations": kept, **counts}

    # --- scheduling helper (time trigger; vinceo.ai has no cron yet) -----------
    def due_for(self, scope: str = "daily", max_age_h: float = 20.0) -> bool:
        """True if no reflection of this scope exists within `max_age_h` — the
        cheap 'is a nightly run due?' check used to auto-enqueue on startup."""
        last = self._s().latest_reflection(scope)
        if not last or not last.get("created_at"):
            return True
        return (time.time() - last["created_at"]) > max_age_h * 3600.0


reflector = Reflector()
