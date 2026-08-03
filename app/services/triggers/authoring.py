"""Chat-first trigger authoring — "whenever X, do Y" → an approved trigger row.

Mnemos is a public product: authoring is a chat sentence, never a config file.
The flow mirrors calendar_intent.py: a deterministic `looks_like_*` gate on the
chat route, then a local-first LLM compile (heuristic fallback when no model is
reachable), then — the validate-live-then-persist move — a BACKTEST over the
last 7 days ("this would have fired N times") shown on the approval card.
Nothing persists until the user answers yes; the saved action goal is exactly
the text shown on the card (targets bound at authoring — the injection rail).
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

from app.services.triggers import ACTION_VERBS, matches
from app.services.triggers import signals as _signals

_BACKTEST_DAYS = 7.0

# Deterministic gate: a when-clause AND a consequent, or an explicit ask.
_EXPLICIT = re.compile(
    r"^\s*(add|create|make|set\s*up)\s+a\s+trigger\b", re.I)
_WHEN_THEN = re.compile(
    r"^\s*(whenever|every\s+time|each\s+time|when\s+(you\s+(see|notice)|i)\b)"
    r".{3,}?(,|\bthen\b)\s*\S.+", re.I | re.S)


def looks_like_trigger_request(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 600:
        return False
    return bool(_EXPLICIT.search(t) or _WHEN_THEN.search(t))


# --- LLM compile (calendar_intent pattern) ---------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {
        "is_trigger": {"type": "boolean"},
        "name": {"type": "string"},
        "signal": {"type": "string",
                   "enum": sorted(_signals.CATALOG)},
        "entity": {"type": "string"},
        "person": {"type": "string"},
        "app": {"type": "string"},
        "text_any": {"type": "array", "items": {"type": "string"}},
        "verb": {"type": "string", "enum": list(ACTION_VERBS)},
        "goal": {"type": "string"},
        "note": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["is_trigger"],
}


def _system_prompt() -> str:
    cat = "\n".join(f"  - {k}: {v}" for k, v in _signals.CATALOG.items())
    return (
        "You compile a user's standing-trigger request (\"whenever X, do Y\") "
        "into a JSON spec. Output ONLY JSON matching the schema.\n\n"
        "Signals (pick the one the WHEN-clause describes):\n" + cat + "\n\n"
        "Condition fields: entity (project/topic name), person, app, text_any "
        "(literal phrases to look for) — set only what the user stated.\n"
        "Action: verb 'run_goal' with an imperative `goal` when the user wants "
        "something done or drafted (email/message/move/schedule...); verb "
        "'notify' with a short `note` when they just want a heads-up. The goal "
        "must name its recipient/target LITERALLY (never a placeholder for "
        "who); {entity}/{app}/{person}/{text} placeholders are allowed only "
        "for what was observed.\n"
        "`name`: a short label (≤8 words). If the message is NOT a standing "
        "when-then automation request, return {\"is_trigger\": false}."
    )


def _compile_llm(text: str) -> dict | None:
    try:
        from app.services.model_router import router
        raw = router.complete_json(
            "trigger_compile", system=_system_prompt(),
            messages=[{"role": "user", "content": text}],
            schema=_SCHEMA, max_tokens=400) or {}
    except Exception as exc:
        print(f"[triggers] llm compile failed ({exc}).")
        return None
    if not raw.get("is_trigger") or raw.get("signal") not in _signals.CATALOG:
        return None
    return raw


# --- Heuristic fallback ----------------------------------------------------

_SPLIT = re.compile(r",|\bthen\b", re.I)
_ENTITY = re.compile(
    r"progress\s+on\s+(?:the\s+)?([\w .'\-]{2,40}?)(?:\s*(?:,|then|$))", re.I)
_ABOUT = re.compile(r"(?:about|on|for)\s+(?:the\s+)?([\w .'\-]{2,40}?)"
                    r"(?:\s*(?:,|then|$))", re.I)
_APP = re.compile(r"(?:close|quit|finish(?:\s+using)?|stop\s+using|"
                  r"done\s+with|leave)\s+(?:a\s+|the\s+)?([\w .\-]{2,30})", re.I)
_NOTIFY = re.compile(r"^\s*(remind\s+me|tell\s+me|let\s+me\s+know|ping\s+me|"
                     r"give\s+me\s+a\s+heads?\s*-?up)\b[\s:,-]*(?:to\s+|"
                     r"about\s+|that\s+)?", re.I)
_OFFER_LEAD = re.compile(r"^\s*(offer\s+to|ask\s+(?:me\s+)?(?:if\s+i\s+want\s+"
                         r"(?:you\s+)?to)?|have\s+(?:you|me)|you\s+should|"
                         r"please)\b[\s:,-]*", re.I)


def _compile_heuristic(text: str) -> dict | None:
    t = (text or "").strip()
    t = _EXPLICIT.sub("", t).strip(" :-,")
    parts = _SPLIT.split(t, maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None
    when, then = parts[0].strip(), parts[1].strip()
    low = when.lower()

    out: dict[str, Any] = {"is_trigger": True}
    m = _ENTITY.search(when)
    if m or "progress" in low:
        out["signal"] = "progress_on"
        if m:
            out["entity"] = m.group(1).strip()
    elif any(w in low for w in ("overdue", "due", "at risk")):
        out["signal"] = "commitment_due"
    elif any(w in low for w in ("quiet", "stalled", "dropped", "no update")):
        out["signal"] = "dropped_thread"
    elif (am := _APP.search(when)) and any(
            w in low for w in ("close", "quit", "session", "stop using",
                               "done with", "finish", "leave")):
        out["signal"] = "app_session_ended"
        out["app"] = am.group(1).strip()
    else:
        out["signal"] = "task_done"
        ab = _ABOUT.search(when)
        if ab:
            out["entity"] = ab.group(1).strip()
    if "entity" not in out and (ab := _ABOUT.search(when)) and \
            out["signal"] in ("progress_on", "task_done"):
        out["entity"] = ab.group(1).strip()

    nm = _NOTIFY.match(then)
    if nm:
        rest = then[nm.end():].strip() or then.strip()
        out["verb"] = "notify"
        out["note"] = ("Heads-up: " + rest[:160])
    else:
        goal = _OFFER_LEAD.sub("", then).strip()
        if not goal:
            return None
        out["verb"] = "run_goal"
        out["goal"] = goal[0].upper() + goal[1:]
    out["name"] = f"{when[:34]} -> {then[:24]}"
    return out


# --- Draft assembly + backtest ---------------------------------------------

def compile_draft(text: str) -> dict | None:
    """utterance → draft {name, signal, condition, action, provenance}."""
    raw = _compile_llm(text) or _compile_heuristic(text)
    if not raw:
        return None
    condition = {}
    for k in ("entity", "person", "app"):
        v = (raw.get(k) or "").strip()
        if v:
            condition[k] = v[:60]
    ta = [str(s).strip()[:60] for s in (raw.get("text_any") or []) if
          str(s).strip()]
    if ta:
        condition["text_any"] = ta[:5]
    verb = raw.get("verb") if raw.get("verb") in ACTION_VERBS else "notify"
    action: dict[str, Any] = {"verb": verb}
    if verb == "run_goal":
        goal = (raw.get("goal") or "").strip()
        if not goal:
            return None
        action["goal"] = goal[:300]
    elif verb == "set_status":
        action["status"] = (raw.get("status")
                            if raw.get("status") in ("open", "done",
                                                     "cancelled") else "done")
    else:
        action["note"] = ((raw.get("note") or raw.get("goal") or "").strip()
                          or "Heads-up: it happened.")[:300]
    name = (raw.get("name") or "").strip()[:80] or "Custom trigger"
    return {"name": name, "signal": raw["signal"], "condition": condition,
            "action": action,
            "provenance": {"source": "chat", "utterance": text[:400]}}


def backtest(store, draft: dict, *, days: float = _BACKTEST_DAYS,
             now: float | None = None) -> dict:
    """Would this trigger have fired recently? Runs the REAL signal scan +
    matcher over the trailing window — the pre-save honesty check."""
    now = float(now if now is not None else time.time())
    probe = {"id": 0, "signal": draft.get("signal"),
             "condition": draft.get("condition") or {}}
    try:
        sigs = _signals.scan(store, now=now, window_s=days * 86400.0)
    except Exception:
        sigs = []
    hits = [s for s in sigs if matches(probe, s)]
    hits.sort(key=lambda s: -s.ts)
    return {"days": days, "count": len(hits),
            "moments": [{"ts": s.ts, "text": s.text} for s in hits[:5]]}


def author(text: str, *, store=None, worker=None) -> dict:
    """Compile + backtest + surface the approval card. Returns a result dict
    (also emitted into chat). Synchronous — chat calls author_async."""
    if store is None:
        from app.storage import get_store
        store = get_store()
    if worker is None:
        from app.services.agent_bridge import worker as _w
        worker = _w
    draft = compile_draft(text)
    if not draft:
        worker._emit(
            "result",
            "I couldn't turn that into a trigger. Try the shape "
            "“whenever <something I can notice>, <what to do>” — e.g. "
            "“whenever I make progress on the thesis, offer to email "
            "Dr. Reyes an update”.")
        return {"ok": False, "reason": "compile_failed"}
    bt = backtest(store, draft)
    shown = worker.propose_trigger_draft(draft, bt)
    return {"ok": True, "draft": draft, "backtest": bt, "shown": bool(shown)}


def author_async(text: str) -> None:
    """Compile on a background thread (LLM work must not block /chat)."""
    threading.Thread(target=lambda: _safe_author(text), daemon=True).start()


def _safe_author(text: str) -> None:
    try:
        author(text)
    except Exception as exc:
        print(f"[triggers] author failed ({exc}).")
        try:
            from app.services.agent_bridge import worker
            worker._emit("result",
                         "Something went wrong designing that trigger — "
                         "try rephrasing it.")
        except Exception:
            pass
