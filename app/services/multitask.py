"""MultiTask decomposition — split ONE chat message into independently-routed
atomic tasks, so a mixed request stops collapsing into a single surface.

The action path used to treat a whole message as one monolithic goal: one route,
one surface, one executor. "Text <name> and find Acme's careers page" then routed
to whichever intent dominated, and the other task was dropped or mis-surfaced.
This layer sits ABOVE routing so each intention is handled on its own surface:

    Goal -> Decompose -> Route each -> Plan each -> Execute (dep-ordered)

Tiered for cost, like the model router — most messages never pay for an LLM here:
  1. `looks_multi()` — a free rule gate. No multi-task marker -> skip decomposition
     entirely and run today's single-goal path.
  2. `decompose()` — a cheap router-tier LLM split only when markers appear, and it
     is instructed to return ONE task when the message is really single (precision
     over recall, the extractor's stance). Any failure fails SAFE to one task.

Dependencies are first-class: "find Acme's careers page and text it to <name>"
yields t1 (browser) and t2 (phone_link, depends_on t1); the orchestrator runs them
in dependency order and feeds t1's result into t2 as context.

General-code invariant: the markers are generic English connectives and the prompt
examples are neutral placeholders — no user data lives in this logic.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

VALID_SURFACES = ("browser", "desktop", "phone_link", "none")

# Cheap rule gate: connectives that *may* join independent intentions. Their
# presence only triggers the LLM split (which is the real decider); their absence
# skips the LLM entirely. Over-triggering costs one cheap call, never a wrong split.
_MARKERS = (" and ", " then ", " also ", " after that ", " as well as ",
            " while you're at it ", " plus ", " & ", " next ", " lastly ")
_LIST_LINE = re.compile(r"\s*([-*•]|\d+[.)])\s+\S")


@dataclass
class AtomicTask:
    """One independent intention pulled out of a message — routed, planned, and
    verified on its own. Mirrors the ActionPacket metadata the rest of the agent
    already speaks (surface / risk / approval / success criteria)."""
    id: str
    text: str
    intent: str = "unknown"
    surface_hint: str | None = None            # browser | desktop | phone_link | none
    depends_on: list[str] = field(default_factory=list)
    can_parallelize: bool = True
    risk: str = "low"
    requires_approval: bool = False
    success_criteria: list[str] = field(default_factory=list)


def enabled() -> bool:
    """Multi-task fan-out is ON by default (the requested new behavior); disable
    with QUILL_MULTITASK=0 to fall back to one-goal-per-message."""
    return os.environ.get("QUILL_MULTITASK", "1") not in ("0", "false", "False")


def looks_multi(text: str) -> bool:
    """Free pre-filter: could this message hold more than one intention? A miss
    here just runs the single-goal path (today's behavior); a false positive costs
    one cheap LLM call that returns a single task."""
    t = (text or "").strip().lower()
    if not t:
        return False
    # Strong signals fire regardless of length.
    if ";" in t:
        return True
    lines = [ln for ln in t.splitlines() if ln.strip()]
    if len(lines) >= 2 and sum(1 for ln in lines if _LIST_LINE.match(ln)) >= 2:
        return True
    # Soft connectives need a bit of body so "rock and roll" doesn't trip it.
    if len(t) < 12:
        return False
    return any(m in t for m in _MARKERS)


# ---------------------------------------------------------------------------
# LLM decomposition (router-tier, structured)
# ---------------------------------------------------------------------------
DECOMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "description": "The independent intentions in the message, in a sensible "
            "order. Return a SINGLE task when the message is really one request — do "
            "not over-split (a task with its own sub-steps, like 'find the pricing and "
            "features page', is ONE task).",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "short stable id, e.g. 't1'"},
                    "text": {"type": "string",
                             "description": "the single-intent task, self-contained"},
                    "intent": {"type": "string",
                               "description": "short verb_noun label, e.g. send_message, "
                               "web_research, open_app, memory_question"},
                    "surface_hint": {
                        "type": "string",
                        "enum": ["browser", "desktop", "phone_link", "none"],
                        "description": "where this task runs: 'browser' for a web "
                        "action/search, 'desktop' for local apps/files/build commands, "
                        "'phone_link' for iPhone SMS/calls, 'none' if answerable from "
                        "memory/conversation alone",
                    },
                    "depends_on": {
                        "type": "array", "items": {"type": "string"},
                        "description": "ids of tasks whose RESULT this one needs (e.g. "
                        "'text <name> the URL' depends on 'find the page'). Empty if "
                        "independent.",
                    },
                    "can_parallelize": {"type": "boolean"},
                    "risk": {"type": "string",
                             "enum": ["low", "medium", "high", "blocked"]},
                    "requires_approval": {
                        "type": "boolean",
                        "description": "true if carrying it out would send/submit/buy/"
                        "delete/change a record",
                    },
                    "success_criteria": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "text", "surface_hint", "depends_on"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tasks"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You split a user's chat message into the INDEPENDENT tasks inside it, so each "
    "can be routed to the right surface and executed on its own. You do NOT perform "
    "the tasks.\n\n"
    "Rules:\n"
    "- Precision over recall. If the message is really ONE request, return ONE task. "
    "Only split when there are genuinely separate intentions. A single task that has "
    "its own internal steps is still ONE task.\n"
    "- Give each task self-contained `text` (resolve pronouns so it stands alone), a "
    "`surface_hint` (browser / desktop / phone_link / none), and `depends_on`: the "
    "ids of tasks whose RESULT it needs. Independent tasks have empty depends_on.\n"
    "- Set `requires_approval` true for anything that would send, submit, buy, "
    "delete, or change a record; false for research, reading, and drafting.\n"
    "- Examples (illustrative shapes, not real data):\n"
    "  'text <name> I'm late and find Acme's careers page' -> two tasks: "
    "phone_link (send, approval), browser (research, no approval), independent.\n"
    "  'find Acme's careers page and text it to <name>' -> t1 browser (find), then "
    "t2 phone_link depends_on t1 (send the found URL).\n"
    "  'what do I owe <name>' -> one task, surface none (memory question)."
)


def _user_prompt(text: str, ctx: str = "") -> str:
    head = (ctx.strip() + "\n\n") if ctx and ctx.strip() else ""
    return head + "Message to split:\n" + text.strip()


def _single(text: str) -> AtomicTask:
    return AtomicTask(id="t1", text=text.strip())


def _shared_llm():
    """The shared router-tier LLM (browser_agent's Anthropic wrapper), or None."""
    try:
        from app.services.agent_planner import _llm
        return _llm()
    except Exception:
        return None


def _parse(raw: dict, original: str) -> list[AtomicTask]:
    items = (raw or {}).get("tasks") or []
    out: list[AtomicTask] = []
    seen_ids: set[str] = set()
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        tid = (it.get("id") or "").strip() or f"t{i + 1}"
        while tid in seen_ids:
            tid = tid + "_"
        seen_ids.add(tid)
        surf = it.get("surface_hint")
        if surf not in VALID_SURFACES:
            surf = None
        deps = [str(d).strip() for d in (it.get("depends_on") or []) if str(d).strip()]
        out.append(AtomicTask(
            id=tid, text=text, intent=(it.get("intent") or "unknown"),
            surface_hint=surf, depends_on=deps,
            can_parallelize=bool(it.get("can_parallelize", True)),
            risk=(it.get("risk") or "low"),
            requires_approval=bool(it.get("requires_approval", False)),
            success_criteria=list(it.get("success_criteria") or []),
        ))
    return out


def decompose(text: str, ctx: str = "", llm=None) -> list[AtomicTask]:
    """Split `text` into atomic tasks. Returns >=1 task ALWAYS: a single-element
    list is the correct answer for a single-intent message, and every failure path
    (disabled, no marker, no LLM, bad output) falls back to one task — so callers
    can treat this as a pure refinement of today's single-goal behavior."""
    text = (text or "").strip()
    if not text:
        return []
    if not enabled() or not looks_multi(text):
        return [_single(text)]
    llm = llm or _shared_llm()
    if llm is None:
        return [_single(text)]
    try:
        from browser_agent import config as bcfg
        raw = llm._json_call(
            bcfg.ROUTER_MODEL, _SYSTEM, _user_prompt(text, ctx),
            DECOMPOSE_SCHEMA, effort=getattr(bcfg, "ROUTER_EFFORT", None)) or {}
        tasks = _parse(raw, text)
        return tasks if tasks else [_single(text)]
    except Exception as exc:
        print(f"[multitask] decomposition fell back to a single task ({exc}).")
        return [_single(text)]


# ---------------------------------------------------------------------------
# ordering + dependency context + result summary (pure, testable offline)
# ---------------------------------------------------------------------------
def order_tasks(tasks: list[AtomicTask]) -> list[AtomicTask]:
    """Flatten into a dependency-respecting order (a task runs after everything it
    depends on). Dangling deps are dropped; cycles are broken gracefully so a bad
    graph never hangs — it just runs in a safe order."""
    by_id = {t.id: t for t in tasks}
    for t in tasks:                       # sanitize deps to real, non-self ids
        t.depends_on = [d for d in t.depends_on if d in by_id and d != t.id]
    ordered: list[AtomicTask] = []
    done: set[str] = set()
    active: set[str] = set()

    def visit(t: AtomicTask) -> None:
        if t.id in done or t.id in active:
            return
        active.add(t.id)
        for d in t.depends_on:
            visit(by_id[d])
        active.discard(t.id)
        done.add(t.id)
        ordered.append(t)

    for t in tasks:
        visit(t)
    return ordered


def dependency_context(task: AtomicTask, results: dict[str, str]) -> str:
    """Render the results of a task's dependencies as a context block to prepend to
    its goal, so 'text it to <name>' actually has the 'it' from the prior step."""
    if not task.depends_on:
        return ""
    lines = ["Results from the steps you just completed (use them to do this task):"]
    for d in task.depends_on:
        r = (results.get(d) or "").strip()
        if r:
            lines.append(f"- {d}: {r[:800]}")
    return "\n".join(lines) if len(lines) > 1 else ""


_OK_STATUSES = {"success", "answered_no_browser", "answered", "done", "completed"}


def status_ok(status: str) -> bool:
    s = (status or "").lower()
    return bool(s) and (s in _OK_STATUSES or s.startswith("success")
                        or s.startswith("answered"))


def summarize(ordered: list[AtomicTask], done_ids, failed_ids, skipped_ids,
              results: dict[str, str]) -> str:
    """Partial-completion summary: a multi-task run reports what got done and what
    still needs help, instead of one all-or-nothing status."""
    by_id = {t.id: t for t in ordered}
    n = len(ordered)
    done_ids = list(done_ids)
    lines = [f"Completed {len(done_ids)} of {n} task"
             f"{'s' if n != 1 else ''}."]
    if done_ids:
        lines.append("\nDone:")
        for tid in done_ids:
            t = by_id.get(tid)
            r = (results.get(tid) or "").strip().replace("\n", " ")
            lines.append(f"  ✓ {t.text if t else tid}"
                         + (f" — {r[:160]}" if r else ""))
    unfinished = list(failed_ids) + list(skipped_ids)
    if unfinished:
        lines.append("\nNeeds help:")
        for tid in unfinished:
            t = by_id.get(tid)
            why = "a prerequisite didn't complete" if tid in skipped_ids else "it didn't finish"
            lines.append(f"  ⚠ {t.text if t else tid} — {why}.")
    return "\n".join(lines)
