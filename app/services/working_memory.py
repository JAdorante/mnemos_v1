"""Working Memory — one attention state for field + grounding + planner (A3).

Consumes ranked constellation candidates, admits them through MMR + hysteresis
+ cluster collapse, and persists `wm_slots` so a restart still "was just
thinking about" the fundraise. Field focus, chat WORKING SET, and planner
`select_context` all read this layer (Field §11 / roadmap A3).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from app.services import mmr as _mmr

# Hysteresis (Field §8.3)
THETA_IN = 0.10
THETA_OUT_RATIO = 0.70
MIN_RESIDENCE_S = 90.0
# Eviction pressure for furniture that nobody engaged
BOREDOM_S = 30 * 60.0
MAX_CLUSTERS = 4
CAPACITY_LO, CAPACITY_HI = 7, 12

_lock = threading.RLock()
_last_ctx_gen: int | None = None
_built_for_gen: int | None = None
_refreshing = False
_last_selection: dict[str, Any] = {
    "path": "wm", "fallback": False, "reason": None, "ts": None,
}
_last_slot_ids: set[str] = set()
_last_delta: dict[str, Any] = {
    "enter": [], "exit": [], "wm": [], "ts": None,
}
# node_key → last positive engagement ts (click/pin/dwell)
_engagement: dict[str, float] = {}

URGENT_THRESH = 0.75


def _wm_enabled() -> bool:
    try:
        from app.config import settings
        return bool(settings.attention.wm)
    except Exception:
        import os
        return os.environ.get("QUILL_WM", "1") not in ("0", "false", "False")


def mark_selection(*, path: str, fallback: bool = False,
                   reason: str | None = None) -> dict[str, Any]:
    """Record how focus was chosen — surfaces silent quota fallbacks."""
    global _last_selection
    info = {
        "path": path,
        "fallback": bool(fallback),
        "reason": reason,
        "ts": time.time(),
    }
    with _lock:
        _last_selection = dict(info)
    if fallback:
        print(f"[working_memory] FALLBACK to {path}"
              + (f" ({reason})" if reason else ""))
    return info


def last_selection() -> dict[str, Any]:
    with _lock:
        return dict(_last_selection)


def last_delta() -> dict[str, Any]:
    """Latest WM enter/exit set — consumed by /field/stream SSE."""
    with _lock:
        return {
            "enter": list(_last_delta.get("enter") or []),
            "exit": list(_last_delta.get("exit") or []),
            "wm": list(_last_delta.get("wm") or []),
            "ts": _last_delta.get("ts"),
        }


def touch_engagement(key, *, now: float | None = None) -> None:
    """Record that a node was engaged (click/pin/dwell) — resets boredom."""
    now = float(now if now is not None else time.time())
    if isinstance(key, (tuple, list)) and len(key) == 2:
        nid = f"{key[0]}:{int(key[1])}"
    else:
        nid = str(key or "")
    if not nid or ":" not in nid:
        return
    with _lock:
        _engagement[nid] = now


def _record_delta(focus: list[dict], *, now: float | None = None) -> dict[str, Any]:
    global _last_slot_ids, _last_delta
    now = float(now if now is not None else time.time())
    new_ids = {n["id"] for n in focus if n.get("id")}
    with _lock:
        enter = sorted(new_ids - _last_slot_ids)
        exit_ids = sorted(_last_slot_ids - new_ids)
        _last_slot_ids = set(new_ids)
        _last_delta = {
            "enter": enter,
            "exit": exit_ids,
            "wm": sorted(new_ids),
            "ts": now,
        }
        return dict(_last_delta)


def _engaged_at(nid: str, slot: dict | None, entered_at: float,
                now: float) -> float:
    """Last engagement clock; defaults to entered_at (no click yet)."""
    reason = (slot or {}).get("reason") or {}
    if isinstance(reason, str):
        try:
            reason = json.loads(reason)
        except Exception:
            reason = {}
    with _lock:
        live = float(_engagement.get(nid) or 0)
    stored = float(reason.get("engaged_at") or 0)
    return max(live, stored, float(entered_at or now))


def capacity_for(focus_k: int) -> int:
    return max(CAPACITY_LO, min(CAPACITY_HI, int(focus_k or CAPACITY_LO)))


def _parse_id(nid: str) -> tuple[str, int] | None:
    try:
        kind, raw = (nid or "").split(":", 1)
        return kind, int(raw)
    except Exception:
        return None


def _theta_out(theta_in: float = THETA_IN) -> float:
    return theta_in * THETA_OUT_RATIO


def _slot_row(n: dict, *, slot: int, entered_at: float | None = None,
              now: float | None = None) -> dict:
    now = now or time.time()
    parsed = _parse_id(n["id"])
    with _lock:
        engaged = float(_engagement.get(n["id"]) or 0)
    reason = {
        "why": list(n.get("why") or [])[:3],
        "gravity": round(float(n.get("gravity") or 0), 4),
        "label": n.get("label"),
        "kind": n.get("kind"),
        "cluster_members": list(n.get("cluster_members") or [])[:24],
        "urgent": bool(n.get("urgent_preempt")),
    }
    if engaged:
        reason["engaged_at"] = engaged
    return {
        "slot": slot,
        "node_type": parsed[0] if parsed else None,
        "node_id": parsed[1] if parsed else None,
        "node_key": n["id"],
        "entered_at": float(entered_at if entered_at is not None else now),
        "score": float(n.get("gravity") or 0),
        "cluster_head": 1,
        "cluster_n": int(n.get("cluster_n") or 1),
        "reason": reason,
        "label": n.get("label"),
        "kind": n.get("kind"),
        "why": list(n.get("why") or [])[:3],
        "pinned": bool(n.get("pinned")),
    }


def load_slots(store) -> list[dict]:
    try:
        rows = store.load_wm_slots()
    except Exception:
        return []
    out = []
    for r in rows:
        reason = r.get("reason")
        if isinstance(reason, str):
            try:
                reason = json.loads(reason)
            except Exception:
                reason = {}
        reason = reason or {}
        nid = None
        if r.get("node_type") is not None and r.get("node_id") is not None:
            nid = f"{r['node_type']}:{int(r['node_id'])}"
        out.append({
            "slot": int(r["slot"]),
            "node_type": r.get("node_type"),
            "node_id": r.get("node_id"),
            "node_key": nid,
            "entered_at": float(r.get("entered_at") or 0),
            "score": float(r.get("score") or 0),
            "cluster_head": int(r.get("cluster_head") or 0),
            "cluster_n": int(r.get("cluster_n") or 1),
            "reason": reason,
            "label": reason.get("label"),
            "kind": reason.get("kind"),
            "why": list(reason.get("why") or []),
        })
    return out


def persist_slots(store, slots: list[dict]) -> None:
    rows = []
    for i, s in enumerate(slots):
        parsed = _parse_id(s.get("node_key") or "")
        if not parsed and s.get("node_type") is not None:
            parsed = (s["node_type"], int(s["node_id"]))
        if not parsed:
            continue
        reason = s.get("reason") or {
            "why": s.get("why") or [],
            "gravity": s.get("score"),
            "label": s.get("label"),
            "kind": s.get("kind"),
            "cluster_members": s.get("cluster_members") or [],
        }
        rows.append({
            "slot": i,
            "node_type": parsed[0],
            "node_id": parsed[1],
            "entered_at": float(s.get("entered_at") or time.time()),
            "score": float(s.get("score") or 0),
            "cluster_head": int(s.get("cluster_head") or 1),
            "cluster_n": int(s.get("cluster_n") or 1),
            "reason": reason,
        })
    store.replace_wm_slots(rows)


def snapshot(store=None) -> list[dict]:
    """Current WM slots for grounding / planner / /field/state."""
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception:
            return []
    return load_slots(store)


def ensure_fresh(store=None, *, limit: int = 28,
                 force: bool = False) -> dict[str, Any]:
    """Rebuild WM from live ranking when Now-Context moved or slots are empty.

    Chat (`compose`) and the planner call this before reading the WORKING SET
    so they share the field's attention without waiting on a constellation poll.
    Re-entrant and no-op when already current for this context generation.
    """
    global _built_for_gen, _refreshing
    if not _wm_enabled():
        return {"refreshed": False, "reason": "wm_disabled"}
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            return {"refreshed": False, "reason": f"no_store:{exc}"}

    try:
        from app.services.now_context import now_context
        gen = int(now_context.generation)
    except Exception:
        gen = None

    with _lock:
        if _refreshing:
            return {"refreshed": False, "reason": "reentrant"}
        slots = load_slots(store)
        if not force:
            # After restart (or hand-seeded slots), trust persistence until
            # Now-Context actually moves — don't thrash-rebuild on every chat.
            if _built_for_gen is None and slots:
                _built_for_gen = gen
                return {"refreshed": False, "reason": "adopted",
                        "generation": gen, "n_slots": len(slots)}
            if slots and gen is not None and gen == _built_for_gen:
                return {"refreshed": False, "reason": "current",
                        "generation": gen, "n_slots": len(slots)}
        _refreshing = True

    try:
        from app.services import graph
        # Scoring + select_focus persist wm_slots; skip ledger spam on chat path.
        graph.constellation(store, limit=max(12, min(int(limit), 40)),
                            record_impressions=False)
        with _lock:
            _built_for_gen = gen
        return {"refreshed": True, "generation": gen,
                "n_slots": len(load_slots(store))}
    except Exception as exc:
        # FakeStore / partial stores in unit tests lack constellation APIs —
        # fail closed without log spam; real failures still surface.
        if not isinstance(exc, AttributeError):
            print(f"[working_memory] ensure_fresh failed ({exc}).")
        return {"refreshed": False, "reason": str(exc)}
    finally:
        with _lock:
            _refreshing = False


def note_built_for_generation(gen: int | None) -> None:
    """Called after select_focus persists so ensure_fresh can skip a no-op."""
    global _built_for_gen
    with _lock:
        _built_for_gen = gen


def render_lines(slots: list[dict] | None = None, *, store=None,
                 limit: int = 12) -> list[str]:
    """Human lines for the WORKING SET grounding block."""
    slots = slots if slots is not None else snapshot(store)
    if not slots:
        return []
    lines = ["WORKING SET (what attention is holding right now):"]
    for s in slots[:limit]:
        label = (s.get("label")
                 or (s.get("reason") or {}).get("label")
                 or s.get("node_key")
                 or "?")
        why = s.get("why") or (s.get("reason") or {}).get("why") or []
        why_s = (why[0] if why else "").strip()
        chip = ""
        n = int(s.get("cluster_n") or 1)
        if n > 1:
            chip = f" (+{n - 1} related)"
        if why_s:
            lines.append(f"- {label}{chip}: {why_s}")
        else:
            lines.append(f"- {label}{chip}")
    return lines


def _context_switched(now_gen: int | None) -> bool:
    global _last_ctx_gen
    with _lock:
        if now_gen is None:
            return False
        switched = (_last_ctx_gen is not None and now_gen != _last_ctx_gen)
        _last_ctx_gen = now_gen
        return switched


def select_focus(
    ranked: list[dict],
    focus_k: int,
    *,
    store=None,
    now: float | None = None,
    theta_in: float = THETA_IN,
    persist: bool = True,
) -> list[dict]:
    """Admit ranked candidates into WM / focus via MMR + hysteresis.

    Returns focus nodes (mutates layer / cluster_* on copies). When WM is
    disabled, returns [] — callers should use ranking.selector (top-k) then
    Admitter; do not treat empty as a signal to run the old quota fork.
    """
    if not _wm_enabled():
        return []

    now = float(now if now is not None else time.time())
    cap = capacity_for(focus_k)
    theta_out = _theta_out(theta_in)

    ctx_gen = None
    try:
        from app.services.now_context import now_context
        ctx_gen = int(now_context.generation)
    except Exception:
        pass
    switched = _context_switched(ctx_gen)

    prev = load_slots(store) if store is not None else []
    by_id = {n["id"]: n for n in ranked}
    kept: list[dict] = []
    kept_ids: set[str] = set()

    for s in prev:
        nid = s.get("node_key")
        if not nid or nid not in by_id:
            continue
        n = by_id[nid]
        score = float(n.get("gravity") or 0)
        age = now - float(s.get("entered_at") or now)
        if n.get("pinned"):
            hold = True
        elif (not switched) and age < MIN_RESIDENCE_S:
            hold = True
        elif score >= theta_out:
            hold = True
        else:
            hold = False
        if not hold:
            continue
        out = dict(n)
        out["cluster_n"] = int(s.get("cluster_n") or out.get("cluster_n") or 1)
        out["entered_at"] = float(s.get("entered_at") or now)
        # Refresh cluster membership against current pool
        members = [
            c for c in ranked
            if c["id"] != nid and _mmr.structural_sim(out, c) >= _mmr.CLUSTER_TAU
        ]
        # Don't claim members that are themselves pinned elsewhere
        members = [m for m in members if not m.get("pinned") or m["id"] == nid]
        out["cluster_n"] = 1 + len(members)
        out["cluster_members"] = [m["id"] for m in members]
        kept.append(out)
        kept_ids.add(nid)
        for m in members:
            kept_ids.add(m["id"])  # absorbed — not separately selectable

    # Boredom: drop non-pinned furniture idle > 30 min without engagement,
    # even under capacity — a slot is too expensive for furniture (§11).
    prev_by = {s.get("node_key"): s for s in prev}
    survivors: list[dict] = []
    for n in kept:
        if n.get("pinned") or float(n.get("prospective_risk") or 0) >= URGENT_THRESH:
            survivors.append(n)
            continue
        entered = float(n.get("entered_at") or now)
        idle = now - _engaged_at(n["id"], prev_by.get(n["id"]), entered, now)
        if idle > BOREDOM_S:
            continue  # evicted → Active; can re-enter after context shifts
        survivors.append(n)
    if len(survivors) != len(kept):
        kept = survivors
        kept_ids = set()
        for n in kept:
            kept_ids.add(n["id"])
            for mid in n.get("cluster_members") or []:
                kept_ids.add(mid)

    # Soft capacity pressure (still apply when over-subscribed after hold)
    if len(kept) > cap:
        def _evict_key(n: dict) -> float:
            entered = float(n.get("entered_at") or now)
            idle = now - _engaged_at(
                n["id"], prev_by.get(n["id"]), entered, now)
            boredom = max(0.0, (idle - BOREDOM_S) / BOREDOM_S) if idle > BOREDOM_S else 0.0
            return float(n.get("gravity") or 0) * max(0.35, 1.0 - idle / (6 * 3600)) - boredom
        pinned = [n for n in kept if n.get("pinned")]
        rest = [n for n in kept if not n.get("pinned")]
        rest.sort(key=_evict_key, reverse=True)
        kept = pinned + rest[: max(0, cap - len(pinned))]
        kept_ids = set()
        for n in kept:
            kept_ids.add(n["id"])
            for mid in n.get("cluster_members") or []:
                kept_ids.add(mid)

    remaining = max(0, cap - len(kept))
    pool = [n for n in ranked
            if n["id"] not in kept_ids
            and (n.get("pinned") or float(n.get("gravity") or 0) >= theta_in
                 or remaining > 0)]
    # Fresh MMR over what's left; pins already kept are excluded from pool.
    fresh = _mmr.mmr_select(pool, remaining, pinned_first=True)
    for n in fresh:
        if n["id"] in kept_ids:
            continue
        n = dict(n)
        n["entered_at"] = now
        # Cap distinct open-work clusters
        open_clusters = sum(
            1 for x in kept if x.get("kind") in _mmr._OPEN)
        if (n.get("kind") in _mmr._OPEN
                and open_clusters >= MAX_CLUSTERS
                and not n.get("pinned")):
            continue
        kept.append(n)
        kept_ids.add(n["id"])
        for mid in n.get("cluster_members") or []:
            kept_ids.add(mid)
        if len(kept) >= cap:
            break

    # If still short (e.g. everything filtered), fill from ranked order
    if len(kept) < cap:
        for n in ranked:
            if n["id"] in kept_ids:
                continue
            out = dict(n)
            out["entered_at"] = now
            out["cluster_n"] = int(out.get("cluster_n") or 1)
            kept.append(out)
            kept_ids.add(n["id"])
            if len(kept) >= cap:
                break

    # Urgent may preempt exactly one slot (§10 / §11)
    kept = _apply_urgent_preempt(kept, ranked, cap, now)

    focus = kept[:cap]
    for n in focus:
        n["layer"] = "focus"

    if persist and store is not None:
        try:
            slots = [
                _slot_row(n, slot=i, entered_at=n.get("entered_at"), now=now)
                for i, n in enumerate(focus)
            ]
            persist_slots(store, slots)
            _sync_att_state(store, focus, ranked)
            note_built_for_generation(ctx_gen)
            mark_selection(path="wm", fallback=False)
            _record_delta(focus, now=now)
        except Exception as exc:
            print(f"[working_memory] persist skipped ({exc}).")
    else:
        _record_delta(focus, now=now)

    return focus


def _apply_urgent_preempt(kept: list[dict], ranked: list[dict],
                          cap: int, now: float) -> list[dict]:
    """Urgent (prospective_risk ≥ 0.75) may claim exactly one WM slot."""
    have = {n["id"] for n in kept}
    urgent = [
        n for n in ranked
        if float(n.get("prospective_risk") or 0) >= URGENT_THRESH
        and n["id"] not in have
        and not n.get("pinned")  # pins already handled
    ]
    if not urgent:
        # Already holding an urgent? mark it.
        for n in kept:
            if float(n.get("prospective_risk") or 0) >= URGENT_THRESH:
                n["urgent_preempt"] = bool(n.get("urgent_preempt"))
        return kept

    best = max(urgent, key=lambda n: (
        float(n.get("prospective_risk") or 0),
        float(n.get("gravity") or 0)))
    out = [dict(n) for n in kept]
    if len(out) >= cap:
        victims = [
            i for i, n in enumerate(out)
            if not n.get("pinned")
            and float(n.get("prospective_risk") or 0) < URGENT_THRESH
        ]
        if not victims:
            return kept
        victim_i = min(victims, key=lambda i: float(out[i].get("gravity") or 0))
        out.pop(victim_i)

    u = dict(best)
    u["entered_at"] = now
    u["layer"] = "focus"
    u["urgent_preempt"] = True
    u["why"] = (["Urgency claimed a working-memory slot"]
                + list(u.get("why") or []))[:3]
    u["cluster_n"] = int(u.get("cluster_n") or 1)
    out.append(u)
    return out[:cap]


def _sync_att_state(store, focus: list[dict], ranked: list[dict]) -> None:
    """Mark focus nodes Focused (or Urgent); clear Focused on nodes that left."""
    focus_keys = set()
    for n in focus:
        p = _parse_id(n["id"])
        if not p:
            continue
        focus_keys.add(p)
        state = "urgent" if (
            n.get("urgent_preempt")
            or float(n.get("prospective_risk") or 0) >= URGENT_THRESH
        ) else "focused"
        try:
            store.set_att_state(p[0], p[1], state)
        except Exception:
            pass
    # Demote previously focused/urgent candidates that fell out (best-effort)
    try:
        prev_rows = (store.list_att_state("focused") or []) + (
            store.list_att_state("urgent") or [])
    except Exception:
        return
    for row in prev_rows:
        key = (row["node_type"], int(row["node_id"]))
        if key in focus_keys:
            continue
        try:
            store.set_att_state(key[0], key[1], "active")
        except Exception:
            pass


def status(store=None) -> dict[str, Any]:
    slots = snapshot(store)
    sel = last_selection()
    delta = last_delta()
    return {
        "enabled": _wm_enabled(),
        "n_slots": len(slots),
        "slots": [
            {"id": s.get("node_key"), "label": s.get("label"),
             "kind": s.get("kind"), "score": round(float(s.get("score") or 0), 3),
             "cluster_n": int(s.get("cluster_n") or 1),
             "entered_at": s.get("entered_at")}
            for s in slots
        ],
        "gamma": _mmr.GAMMA,
        "theta_in": THETA_IN,
        "theta_out": _theta_out(),
        "min_residence_s": MIN_RESIDENCE_S,
        "built_for_generation": _built_for_gen,
        "selection": sel,
        "delta": {
            "enter": delta.get("enter") or [],
            "exit": delta.get("exit") or [],
            "ts": delta.get("ts"),
        },
    }
