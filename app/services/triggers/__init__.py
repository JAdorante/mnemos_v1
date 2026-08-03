"""Standing triggers — "when it sees X, it offers Y", as DATA rows.

The engine generalizes the shape every proactive feature already shares
(todo_watcher, task_offer, anticipation, Track D reasoners): a calm background
pass that derives signals, matches them against standing trigger rows, gates
the winner through the ONE readiness seam, and surfaces at most one yes/no
offer via the agent_bridge queue. User-specific behavior lives entirely in
`triggers` table rows (custom-authored in chat, miner-suggested, or builtin);
this module ships identical for every user.

Safety posture (v1, deliberate):
  * OFFER-ONLY. A trigger firing never acts — it asks. Accepting hands the
    authored goal to the existing agent path with its per-commit approval
    gate; there is no auto-execution tier yet.
  * Targets bound at authoring. Action goals are the text the user approved
    when the trigger was created; matched content may only fill the
    {entity}/{app}/{person}/{text} placeholders, single-pass, sanitized —
    ambient (screen/phone/documents/peer-derived) signals are labeled on the
    card so the user sees what the evidence source was.
  * One interruption budget. Trigger offers draw from the SAME daily calm
    budget as the Track D reasoners (reasoners/base.py) — adding ten triggers
    doesn't buy ten interruptions a day.

Kill switch: QUILL_TRIGGERS=0 (and QUILL_AGENT=0 implies off).
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from app.services.triggers import signals as _signals
from app.services.triggers.signals import Signal

_attach_lock = threading.Lock()
_timer: threading.Timer | None = None
_INTERVAL_S = float(os.environ.get("QUILL_TRIGGER_INTERVAL_S", "900") or "900")
_WINDOW_S = float(os.environ.get("QUILL_TRIGGER_WINDOW_S", "3600") or "3600")
# Per-(trigger, signal identity) re-fire cooldown; a trigger row may override
# via gating.cooldown_s.
_COOLDOWN_S = float(os.environ.get("QUILL_TRIGGER_COOLDOWN_S", "21600")
                    or "21600")
# Auto-pause: a trigger whose offers keep getting waved off pauses itself.
_PAUSE_MIN_OFFERS = int(os.environ.get("QUILL_TRIGGER_PAUSE_MIN", "5") or "5")
_PAUSE_ACCEPT_RATE = float(os.environ.get("QUILL_TRIGGER_PAUSE_RATE", "0.2")
                           or "0.2")

_recent: dict[str, float] = {}       # f"{trigger_id}|{signal.key}" -> last ts
_lock = threading.Lock()
_last_run: dict[str, Any] = {}
_last_mine = 0.0
_MINE_INTERVAL_S = float(os.environ.get("QUILL_TRIGGER_MINE_INTERVAL_S",
                                        "21600") or "21600")

ACTION_VERBS = ("run_goal", "notify", "set_status")


def enabled() -> bool:
    if os.environ.get("QUILL_TRIGGERS", "1") in ("0", "false", "False"):
        return False
    if os.environ.get("QUILL_AGENT") in ("0", "false", "False"):
        return False
    return True


def clear_state_for_tests() -> None:
    global _last_mine
    with _lock:
        _recent.clear()
        _last_mine = 0.0


# ---------------------------------------------------------------------------
# Matching + action rendering

def matches(trigger: dict, sig: Signal) -> bool:
    """Structured predicates only — cheap, deterministic, no LLM."""
    if (trigger.get("signal") or "") != sig.name:
        return False
    cond = trigger.get("condition") or {}
    ent = (cond.get("entity") or "").strip().lower()
    if ent:
        have = (sig.entity or "").strip().lower()
        if not have or (ent not in have and have not in ent):
            return False
    person = (cond.get("person") or "").strip().lower()
    if person:
        have = (sig.person or "").strip().lower()
        if not have or (person not in have and have not in person):
            return False
    app = (cond.get("app") or "").strip().lower()
    if app:
        have = (sig.app or "").strip().lower()
        for suf in (".exe", ".app"):
            if have.endswith(suf):
                have = have[: -len(suf)]
            if app.endswith(suf):
                app = app[: -len(suf)]
        if not have or (app not in have and have not in app):
            return False
    text_any = cond.get("text_any") or []
    if text_any:
        blob = " ".join([sig.text or "",
                         str((sig.payload or {}).get("summary") or "")]).lower()
        if not any((t or "").strip().lower() in blob
                   for t in text_any if (t or "").strip()):
            return False
    return True


_PLACEHOLDER = re.compile(r"\{(entity|app|person|text)\}")


def _fill(template: str, sig: Signal) -> str:
    """Single-pass placeholder fill. Values are sanitized (no newlines, capped)
    and substituted values are NEVER re-scanned — matched content can't smuggle
    a second placeholder in."""
    vals = {"entity": sig.entity or "", "app": sig.app or "",
            "person": sig.person or "", "text": sig.text or ""}

    def _sub(m: re.Match) -> str:
        v = " ".join(str(vals.get(m.group(1), "")).split())
        return v[:80]

    return _PLACEHOLDER.sub(_sub, template or "")


def render_action(trigger: dict, sig: Signal) -> dict:
    """The concrete thing 'yes' does, built from the AUTHORED action spec."""
    act = dict(trigger.get("action") or {})
    verb = act.get("verb") if act.get("verb") in ACTION_VERBS else "notify"
    out: dict[str, Any] = {"verb": verb}
    if verb == "run_goal":
        out["goal"] = _fill(str(act.get("goal") or ""), sig)
    elif verb == "set_status":
        out["status"] = (act.get("status")
                         if act.get("status") in ("open", "done", "cancelled")
                         else "done")
        out["fact_id"] = sig.fact_id
    else:  # notify
        out["note"] = _fill(str(act.get("note") or act.get("goal") or ""), sig)
    return out


def _action_summary(action: dict) -> str:
    verb = action.get("verb")
    if verb == "run_goal":
        return action.get("goal") or "run the saved action"
    if verb == "set_status":
        return f"mark it {action.get('status', 'done')}"
    return action.get("note") or "give you a heads-up"


# ---------------------------------------------------------------------------
# The calm pass

def _cooldown_key(trigger_id: int, sig: Signal) -> str:
    return f"{trigger_id}|{sig.key}"


def _on_cooldown(trigger: dict, sig: Signal, now: float) -> bool:
    cd = float((trigger.get("gating") or {}).get("cooldown_s") or _COOLDOWN_S)
    with _lock:
        last = _recent.get(_cooldown_key(int(trigger["id"]), sig))
        return last is not None and (now - last) < cd


def _mark_fired(trigger: dict, sig: Signal, now: float) -> None:
    with _lock:
        _recent[_cooldown_key(int(trigger["id"]), sig)] = now


def _tele(hit: bool, reason: str, **meta) -> None:
    try:
        from app.services.cog_telemetry import cog_telemetry, TRIGGER_OFFER
        cog_telemetry.record(TRIGGER_OFFER, hit, reason=reason, **meta)
    except Exception:
        pass


def run_once(store=None, *, surface: bool = True,
             now: float | None = None) -> dict[str, Any]:
    """One pass: scan signals → match active triggers → gate → ≤1 offer.
    Also surfaces at most one pending miner suggestion per pass, and kicks the
    miner itself on its own (slow) cadence. Never raises."""
    global _last_run, _last_mine
    if not enabled():
        out = {"ok": True, "enabled": False, "offered": False,
               "reason": "disabled"}
        _last_run = out
        return out
    try:
        if store is None:
            from app.storage import get_store
            store = get_store()
    except Exception as exc:
        out = {"ok": False, "error": str(exc)}
        _last_run = out
        return out
    now = float(now if now is not None else time.time())

    # Mining runs INSIDE the pass but on its own cadence — suggestions land as
    # status='suggested' rows and surface below, through the same budget.
    if os.environ.get("QUILL_TRIGGER_MINE", "1") not in ("0", "false", "False"):
        with _lock:
            due = (now - _last_mine) >= _MINE_INTERVAL_S
            if due:
                _last_mine = now
        if due:
            try:
                from app.services.triggers import miner
                miner.mine(store, now=now)
            except Exception as exc:
                print(f"[triggers] mine skipped ({exc}).")

    active = store.list_triggers(status="active")
    suggested = store.list_triggers(status="suggested")
    result: dict[str, Any] = {"ok": True, "enabled": True, "ts": now,
                              "n_active": len(active),
                              "n_suggested": len(suggested),
                              "offered": False}
    if not active and not suggested:
        result["reason"] = "no_triggers"
        _last_run = result
        return result

    from app.services.reasoners import base as _budget

    # Candidate fires: (trigger, signal), cooldown-filtered.
    fires: list[tuple[dict, Signal]] = []
    if active:
        sigs = _signals.scan(store, now=now, window_s=_WINDOW_S)
        for trg in active:
            for sig in sigs:
                if matches(trg, sig) and not _on_cooldown(trg, sig, now):
                    fires.append((trg, sig))
    result["n_candidates"] = len(fires)

    if _budget.daily_budget_exhausted():
        result["reason"] = "daily_budget"
        _last_run = result
        return result

    # Arbitrate: strongest evidence first (signal confidence, then recency).
    fires.sort(key=lambda fs: (-fs[1].confidence, -fs[1].ts))
    for trg, sig in fires:
        store.bump_trigger_stat(int(trg["id"]), "fires", now)
        action = render_action(trg, sig)
        goal_text = (action.get("goal") or action.get("note")
                     or f"{sig.text} -> {trg.get('name')}")
        from app.services.readiness import for_task
        v = for_task(goal_text, sig.confidence)
        if not v.should_offer:
            _tele(False, f"readiness:{v.band}", trigger_id=trg["id"],
                  signal=sig.name, score=v.score, risk=v.risk)
            continue
        _mark_fired(trg, sig, now)
        result["proposal"] = {"trigger_id": trg["id"], "name": trg["name"],
                              "signal": sig.name, "text": sig.text,
                              "action": action, "ambient": sig.ambient}
        result["verdict"] = {"band": v.band, "score": v.score, "risk": v.risk}
        if not surface:
            result["reason"] = "dry_run"
            _last_run = result
            return result
        try:
            from app.services.agent_bridge import worker
            shown = worker.propose_trigger(trg, sig, action)
            store.bump_trigger_stat(int(trg["id"]), "offers", now)
            # Unified calm budget: a trigger offer spends a reasoner slot.
            _budget.mark_offered(_budget.Proposal(
                reasoner="trigger", goal=goal_text[:80],
                summary=trg.get("name") or "", fact_id=sig.fact_id))
            _tele(True, "surfaced" if shown else "queued",
                  trigger_id=trg["id"], signal=sig.name, score=v.score,
                  risk=v.risk, ambient=sig.ambient)
            result["offered"] = True
            result["reason"] = "surfaced" if shown else "queued"
        except Exception as exc:
            result["reason"] = f"offer_failed:{exc}"
            print(f"[triggers] offer skipped ({exc}).")
        _last_run = result
        return result

    # Nothing fired — surface one pending suggestion instead (adopt card).
    for row in suggested:
        h = f"suggest|{row['id']}"
        with _lock:
            last = _recent.get(h)
            if last is not None and (now - last) < _COOLDOWN_S:
                continue
            _recent[h] = now
        if not surface:
            result["reason"] = "dry_run_suggest"
            result["proposal"] = {"suggest_id": row["id"]}
            _last_run = result
            return result
        try:
            from app.services.agent_bridge import worker
            shown = worker.propose_trigger_suggest(row)
            _budget.mark_offered(_budget.Proposal(
                reasoner="trigger_suggest", goal=(row.get("name") or "")[:80],
                summary=row.get("name") or ""))
            _tele(True, "suggest_surfaced" if shown else "suggest_queued",
                  trigger_id=row["id"])
            result["offered"] = True
            result["reason"] = "suggest_surfaced" if shown else "suggest_queued"
        except Exception as exc:
            result["reason"] = f"suggest_failed:{exc}"
            print(f"[triggers] suggestion skipped ({exc}).")
        _last_run = result
        return result

    result["reason"] = "no_candidate"
    _last_run = result
    return result


# ---------------------------------------------------------------------------
# Offer resolution (called from agent_bridge.resolve_todo)

def resolve_offer(worker, pend: dict, accept: bool, store=None) -> dict:
    """Resolve a trigger-family offer: `trigger` (a fire), `trigger_suggest`
    (adopt a mined suggestion), `trigger_draft` (approve a chat-authored
    draft). Records outcomes on the row so a cold trigger pauses itself."""
    kind = pend.get("kind") or ""
    now = time.time()
    if store is None:
        from app.storage import get_store
        store = get_store()

    # Close the ledger/telemetry loop exactly like task offers do.
    try:
        from app.services.task_offer import record_offer_outcome
        items = pend.get("items") or []
        record_offer_outcome(items[0] if items else (pend.get("title") or ""),
                             bool(accept), kind=kind,
                             fact_id=pend.get("fact_id"))
    except Exception:
        pass

    if kind == "trigger_draft":
        draft = pend.get("draft") or {}
        if not accept:
            worker._emit("system", "Okay — dropped that trigger draft.")
            worker._advance_offers()
            return {"ok": True, "accepted": False, "kind": kind}
        try:
            tid = store.add_trigger(
                str(draft.get("name") or "Untitled trigger")[:80],
                str(draft.get("signal") or ""),
                condition=draft.get("condition") or {},
                action=draft.get("action") or {},
                gating=draft.get("gating") or {},
                provenance=draft.get("provenance") or {"source": "chat"},
                origin="custom", status="active", created_at=now)
            worker._emit("system",
                         f"Trigger saved and active: “{draft.get('name')}”. "
                         "Manage it any time at /triggers.")
            worker._advance_offers()
            return {"ok": True, "accepted": True, "kind": kind,
                    "trigger_id": tid}
        except Exception as exc:
            worker._emit("system", f"Couldn't save that trigger ({exc}).")
            worker._advance_offers()
            return {"ok": False, "error": str(exc), "kind": kind}

    trigger_id = pend.get("trigger_id")
    row = store.get_trigger(int(trigger_id)) if trigger_id else None

    if kind == "trigger_suggest":
        if row is None:
            worker._emit("system", "That suggestion is gone — nothing to adopt.")
            worker._advance_offers()
            return {"ok": False, "error": "missing trigger", "kind": kind}
        if accept:
            store.set_trigger_status(int(row["id"]), "active", now)
            worker._emit("system",
                         f"Adopted — I'll watch for that. “{row.get('name')}” "
                         "is active (manage at /triggers).")
        else:
            # A dismissed suggestion is a durable negative example: retired
            # rows keep their pattern_key so the miner never re-suggests it.
            store.set_trigger_status(int(row["id"]), "retired", now)
            worker._emit("system", "Okay — I won't suggest that again.")
        worker._advance_offers()
        return {"ok": True, "accepted": bool(accept), "kind": kind,
                "trigger_id": row["id"]}

    # kind == "trigger": a fire card.
    if row is not None:
        stats = store.bump_trigger_stat(
            int(row["id"]), "accepts" if accept else "dismisses", now)
        # Auto-pause: enough offers, almost no acceptance -> stop interrupting.
        offers = int(stats.get("offers", 0))
        accepts = int(stats.get("accepts", 0))
        if (not accept and offers >= _PAUSE_MIN_OFFERS
                and (accepts / max(1, offers)) < _PAUSE_ACCEPT_RATE):
            store.set_trigger_status(int(row["id"]), "paused", now)
            worker._emit("system",
                         f"I've paused the “{row.get('name')}” trigger — its "
                         "offers keep getting waved off. Reactivate it at "
                         "/triggers if you still want it.")
    if not accept:
        worker._emit("system", "Okay — I'll leave that for now.")
        worker._advance_offers()
        return {"ok": True, "accepted": False, "kind": kind}

    action = pend.get("action") or {}
    verb = action.get("verb")
    if verb == "run_goal" and (action.get("goal") or "").strip():
        worker._emit("system",
                     "On it — I'll pause for your approval before sending, "
                     "buying, or anything irreversible.")
        worker.send(action["goal"], fact_id=pend.get("fact_id"))
        worker._advance_offers()
        return {"ok": True, "accepted": True, "kind": kind, "queued": 1}
    if verb == "set_status" and action.get("fact_id"):
        try:
            ok = store.set_fact_status(int(action["fact_id"]),
                                       action.get("status") or "done")
            worker._emit("system",
                         ("Done — marked it "
                          f"{action.get('status') or 'done'}.") if ok
                         else "Couldn't find that item to update.")
        except Exception as exc:
            worker._emit("system", f"Couldn't update it ({exc}).")
        worker._advance_offers()
        return {"ok": True, "accepted": True, "kind": kind}
    note = (action.get("note") or "").strip()
    worker._emit("system", note or "Noted.")
    worker._advance_offers()
    return {"ok": True, "accepted": True, "kind": kind}


# ---------------------------------------------------------------------------
# Status + background tick

def status(store=None) -> dict[str, Any]:
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    from app.services.reasoners.base import daily_budget_remaining
    rows = store.list_triggers(limit=200)
    return {
        "enabled": enabled(),
        "interval_s": _INTERVAL_S,
        "daily_remaining": daily_budget_remaining(),
        "signals": dict(_signals.CATALOG),
        "counts": {
            s: sum(1 for r in rows if r.get("status") == s)
            for s in ("active", "suggested", "paused", "retired")},
        "triggers": rows,
        "last": _last_run or None,
    }


def attach() -> None:
    """Background tick — same spirit as reasoners.attach()."""
    if not enabled():
        return
    with _attach_lock:
        _schedule_next(immediate=True)
    print(f"[triggers] attached (every {int(_INTERVAL_S)}s when agent on).")


def _schedule_next(*, immediate: bool = False) -> None:
    global _timer
    delay = 20.0 if immediate else max(60.0, _INTERVAL_S)

    def _tick() -> None:
        try:
            run_once(surface=True)
        except Exception as exc:
            print(f"[triggers] tick skipped ({exc}).")
        with _attach_lock:
            _schedule_next(immediate=False)

    t = threading.Timer(delay, _tick)
    t.daemon = True
    t.start()
    _timer = t


# Re-exported for agent_bridge / routes convenience.
__all__ = ["enabled", "run_once", "resolve_offer", "status", "attach",
           "matches", "render_action", "Signal", "clear_state_for_tests",
           "ACTION_VERBS"]
