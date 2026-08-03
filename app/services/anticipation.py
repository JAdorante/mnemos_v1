"""Anticipate likely next work from recent desktop activities.

Reads activity blocks (app focus trail), scores simple A→B transitions plus
open tasks that match the predicted next app, and surfaces a yes/no chat offer
via agent_bridge — the proactive twin of todo_watcher / task_offer.

Heuristic only (no LLM). Disabled by default: QUILL_ANTICIPATE=1 to enable.
Triggered after activity.rebuild() when the newest block looks idle/closed.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import Counter
from typing import Any

from app.config import settings
from app.storage import Store, get_store

_recent: dict[str, float] = {}  # suggestion hash -> last offered time
_lock = threading.Lock()
_last_consider = 0.0


def _enabled() -> bool:
    return (
        settings.anticipation.enabled
        and os.environ.get("QUILL_AGENT") not in ("0", "false", "False")
    )


def _hash(key: str) -> str:
    return hashlib.sha1((key or "").strip().lower().encode()).hexdigest()


def _tele(hit: bool, reason: str, **meta) -> None:
    try:
        from app.services.cog_telemetry import cog_telemetry, OFFER

        cog_telemetry.record(OFFER, hit, reason=reason, kind="anticipation", **meta)
    except Exception:
        pass


def _transitions(chrono: list[dict]) -> Counter[tuple[str, str]]:
    """Count app→app transitions along chronological activities."""
    c: Counter[tuple[str, str]] = Counter()
    for i in range(len(chrono) - 1):
        a = (chrono[i].get("app") or "").strip()
        b = (chrono[i + 1].get("app") or "").strip()
        if a and b and a.lower() != b.lower():
            c[(a, b)] += 1
    return c


def _open_tasks(store: Store, limit: int = 24) -> list[dict]:
    try:
        return store.list_facts(kind="task", status="open", limit=limit,
                                actionable=True)
    except Exception:
        return []


def _task_matches_app(text: str, app: str) -> bool:
    """Loose match: app token appears in the task, or vice versa."""
    t = (text or "").lower()
    a = (app or "").lower().strip()
    if not t or not a or a == "desktop":
        return False
    # Strip common suffixes (.exe, etc.)
    for suf in (".exe", ".app"):
        if a.endswith(suf):
            a = a[: -len(suf)]
    if len(a) < 3:
        return False
    if a in t:
        return True
    # First significant word of the app name
    token = a.split()[0] if a.split() else a
    return len(token) >= 4 and token in t


def score_candidates(store: Store | None = None, *, now: float | None = None
                     ) -> list[dict[str, Any]]:
    """Return ranked suggestion dicts (may be empty). Pure scoring — no side effects."""
    cfg = settings.anticipation
    store = store or get_store()
    now = now if now is not None else time.time()
    acts = store.recent_activities(cfg.history)
    if len(acts) < cfg.min_activities:
        return []

    # recent_activities is newest-first; chrono is oldest→newest.
    chrono = list(reversed(acts))
    newest = chrono[-1]
    cur_app = (newest.get("app") or "").strip() or "desktop"
    idle_for = now - float(newest.get("end") or now)
    # Only anticipate when the current block looks paused/closed.
    if idle_for < cfg.idle_s:
        return []

    trans = _transitions(chrono)
    from_cur = [(b, n) for (a, b), n in trans.items() if a.lower() == cur_app.lower()]
    total_from = sum(n for _, n in from_cur) or 0
    if total_from < cfg.min_transition_count:
        # Not enough pattern from this app — still allow open-task match alone.
        from_cur = []

    candidates: list[dict[str, Any]] = []
    open_tasks = _open_tasks(store)

    # --- transition-based next app ---
    for next_app, count in sorted(from_cur, key=lambda x: -x[1]):
        conf = count / max(1, total_from)
        # Recency: if the previous block was already next_app, small boost.
        if (len(chrono) >= 2
                and (chrono[-2].get("app") or "").lower() == next_app.lower()):
            conf = min(1.0, conf + 0.05)
        matched = next(
            (f for f in open_tasks
             if _task_matches_app(f.get("text") or "", next_app)),
            None,
        )
        if matched:
            conf = min(1.0, conf + 0.15)
            goal = (matched.get("text") or "").strip()
            fact_id = matched.get("fact_id") or matched.get("id")
            title = f"Continue in {next_app}"
            rationale = (
                f"After {cur_app} you often open {next_app} "
                f"({count}/{total_from} times), and you have an open task that fits."
            )
        else:
            # Bare app launches are the noisiest offer class — opt-in only.
            if not getattr(cfg, "offer_open_app", False):
                continue
            goal = f"Open {next_app}"
            fact_id = None
            title = f"Open {next_app} next"
            rationale = (
                f"After working in {cur_app}, you often switch to {next_app} "
                f"({count}/{total_from} recent transitions)."
            )
        if conf < cfg.min_conf:
            continue
        candidates.append({
            "kind": "anticipation",
            "title": title,
            "goal": goal,
            "rationale": rationale,
            "confidence": round(conf, 3),
            "from_app": cur_app,
            "next_app": next_app,
            "fact_id": fact_id,
            "idle_s": round(idle_for, 1),
        })

    # --- open task only (no transition pattern): weak, needs higher conf via boost ---
    if not candidates and open_tasks:
        # Prefer a task that doesn't match the current app (something waiting elsewhere)
        waiting = [
            f for f in open_tasks
            if not _task_matches_app(f.get("text") or "", cur_app)
        ]
        pick = waiting[0] if waiting else open_tasks[0]
        text = (pick.get("text") or "").strip()
        if text:
            conf = 0.55  # below default min_conf (0.6) unless user lowers floor
            if conf >= cfg.min_conf:
                candidates.append({
                    "kind": "anticipation",
                    "title": "Open task waiting",
                    "goal": text,
                    "rationale": (
                        f"You've been idle in {cur_app}; this open task is still on "
                        f"your board."
                    ),
                    "confidence": conf,
                    "from_app": cur_app,
                    "next_app": "",
                    "fact_id": pick.get("fact_id") or pick.get("id"),
                    "idle_s": round(idle_for, 1),
                })

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates[: cfg.max_offers]


def consider(store: Store | None = None) -> bool:
    """Score + maybe surface one anticipation offer. Safe to call after rebuild."""
    global _last_consider
    if not _enabled():
        return False
    cfg = settings.anticipation
    now = time.time()
    with _lock:
        if now - _last_consider < cfg.consider_cooldown_s:
            return False
        _last_consider = now

    try:
        cands = score_candidates(store, now=now)
    except Exception as exc:
        print(f"[anticipate] score failed ({exc}).")
        return False
    if not cands:
        _tele(False, "no_candidate")
        return False

    top = cands[0]
    key = _hash(f"{top.get('from_app')}|{top.get('next_app')}|{top.get('goal')}")
    with _lock:
        last = _recent.get(key)
        if last is not None and now - last < cfg.cooldown_s:
            _tele(False, "cooldown", text=(top.get("goal") or "")[:80])
            return False
        _recent[key] = now

    try:
        from app.services.agent_bridge import worker

        shown = worker.propose_anticipation(top)
        _tele(True, "surfaced", score=top.get("confidence"),
              text=(top.get("goal") or "")[:80])
        print(f"[anticipate] offered ({'shown' if shown else 'queued'}): "
              f"{top.get('title')} · conf={top.get('confidence')}")
        return True
    except Exception as exc:
        print(f"[anticipate] offer skipped ({exc}).")
        return False
