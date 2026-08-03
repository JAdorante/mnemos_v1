"""Field history — snapshots, diff, and aging (constellation WS3).

Snapshots are a lightweight ring buffer for /field/diff — not an archive.
The event log remains the source of truth.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from app.services.ranking.config import (
    AGING_MARGIN_DAYS,
    AGING_OPEN_DAYS,
    FIELD_SNAPSHOT_MAX_N,
    FIELD_SNAPSHOT_RETAIN_DAYS,
)


def _start_of_today_local(now: float | None = None) -> float:
    now = float(now if now is not None else time.time())
    local = datetime.fromtimestamp(now).astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def snapshot_from_field(field: dict, *, version: str, ts: float | None = None) -> dict:
    """Build a snapshot payload from a constellation /field/state dict."""
    ts = float(ts if ts is not None else time.time())
    focus_ids: list[str] = []
    periphery_ids: list[str] = []
    per_node: dict[str, dict] = {}
    for n in field.get("nodes") or []:
        nid = n.get("id")
        if not nid:
            continue
        layer = n.get("layer")
        if layer == "focus":
            focus_ids.append(nid)
        elif layer == "periphery":
            periphery_ids.append(nid)
        due_ts = None
        due = n.get("due")
        if isinstance(due, (int, float)):
            due_ts = float(due)
        per_node[nid] = {
            "gravity_total": float(n.get("gravity") or 0),
            "due_ts": due_ts,
            "last_seen_ts": float(n["ts"]) if n.get("ts") else None,
            "kind": n.get("kind"),
            "age_days": n.get("age_days"),
            "aging": n.get("aging"),
        }
    return {
        "version": str(version),
        "ts": ts,
        "focus_ids": focus_ids,
        "periphery_ids": periphery_ids,
        "per_node": per_node,
    }


def maybe_persist_snapshot(store, field: dict, *, now: float | None = None) -> dict | None:
    """Persist a snapshot when memory_version moved since the last one."""
    now = float(now if now is not None else time.time())
    try:
        version = store.memory_version()
    except Exception:
        return None
    last = store.latest_field_snapshot()
    if last and last.get("version") == version:
        return None
    snap = snapshot_from_field(field, version=version, ts=now)
    try:
        store.add_field_snapshot(
            version=snap["version"],
            ts=snap["ts"],
            focus_ids=snap["focus_ids"],
            periphery_ids=snap["periphery_ids"],
            per_node=snap["per_node"],
        )
        store.prune_field_snapshots(
            retain_days=FIELD_SNAPSHOT_RETAIN_DAYS,
            max_n=FIELD_SNAPSHOT_MAX_N,
            now=now,
        )
    except Exception as exc:
        print(f"[field_history] snapshot skipped ({exc}).")
        return None
    return snap


def aging_open_work(store, *, now: float | None = None,
                    threshold_days: float | None = None) -> list[dict]:
    """Open tasks/commitments whose age exceeds the aging threshold."""
    now = float(now if now is not None else time.time())
    threshold = float(
        threshold_days if threshold_days is not None else AGING_MARGIN_DAYS)
    out: list[dict] = []
    try:
        facts = store.list_facts(status="open", limit=200, actionable=True)
    except Exception:
        return []
    for f in facts:
        if f.get("kind") not in ("task", "commitment"):
            continue
        ts = f.get("extracted_at") or f.get("source_time")
        if not ts:
            continue
        age_days = max(0.0, (now - float(ts)) / 86400.0)
        # Prefer due-based age when overdue.
        due = f.get("due")
        due_age = None
        if due is not None:
            try:
                from app.services.graph import _due_days
                dd = _due_days(due, now)
                if dd is not None and dd < 0:
                    due_age = -dd
            except Exception:
                pass
        effective = max(age_days, due_age or 0.0)
        if effective < threshold:
            continue
        out.append({
            "id": f"fact:{f['fact_id']}",
            "kind": f.get("kind"),
            "text": (f.get("text") or f.get("source_span") or "")[:120],
            "age_days": round(effective, 1),
            "due": due,
        })
    out.sort(key=lambda x: -float(x["age_days"]))
    return out


def diff(
    store,
    *,
    since: str | float | None = None,
    now: float | None = None,
    current: dict | None = None,
) -> dict[str, Any]:
    """Compare current field to a prior snapshot.

    `since` may be a unix timestamp, ISO date, or memory_version string.
    Default: start of today (user-local).
    """
    now = float(now if now is not None else time.time())
    since_ts: float | None = None
    since_version: str | None = None
    if since is None or since == "" or since == "today":
        since_ts = _start_of_today_local(now)
    else:
        try:
            since_ts = float(since)
        except (TypeError, ValueError):
            s = str(since).strip()
            # Version tokens look like "12-1700...-3-..."
            if s.count("-") >= 3 and s[0].isdigit():
                since_version = s
            else:
                try:
                    # ISO date / datetime
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    since_ts = datetime.fromisoformat(s).timestamp()
                except Exception:
                    since_ts = _start_of_today_local(now)

    prior = store.field_snapshot_at_or_before(
        since_ts=since_ts, since_version=since_version)
    if prior is None and since_ts is not None:
        # Nothing at/before since — use earliest snapshot after? Prefer none.
        prior = None

    if current is None:
        cur_snap = store.latest_field_snapshot()
    else:
        try:
            ver = store.memory_version()
        except Exception:
            ver = "live"
        cur_snap = snapshot_from_field(current, version=ver, ts=now)

    if cur_snap is None:
        cur_snap = {
            "version": None, "ts": now,
            "focus_ids": [], "periphery_ids": [], "per_node": {},
        }

    prior_focus = set((prior or {}).get("focus_ids") or [])
    cur_focus = set(cur_snap.get("focus_ids") or [])
    prior_node = (prior or {}).get("per_node") or {}
    cur_node = cur_snap.get("per_node") or {}

    entered = sorted(cur_focus - prior_focus)
    left = sorted(prior_focus - cur_focus)

    rising: list[dict] = []
    falling: list[dict] = []
    for nid, cur in cur_node.items():
        old = prior_node.get(nid)
        if not old:
            continue
        g0 = float(old.get("gravity_total") or 0)
        g1 = float(cur.get("gravity_total") or 0)
        delta = g1 - g0
        if abs(delta) < 0.03:
            continue
        row = {"id": nid, "delta": round(delta, 4),
               "gravity": round(g1, 4), "kind": cur.get("kind")}
        if delta > 0:
            rising.append(row)
        else:
            falling.append(row)
    rising.sort(key=lambda x: -x["delta"])
    falling.sort(key=lambda x: x["delta"])

    aging = aging_open_work(store, now=now)

    return {
        "since": {
            "ts": (prior or {}).get("ts") if prior else since_ts,
            "version": (prior or {}).get("version") if prior else since_version,
            "default": "today" if since in (None, "", "today") else since,
        },
        "current": {
            "ts": cur_snap.get("ts"),
            "version": cur_snap.get("version"),
        },
        "entered_focus": entered,
        "left_focus": left,
        "rising": rising[:24],
        "falling": falling[:24],
        "aging": aging[:24],
        "has_prior": prior is not None,
    }


def aging_signal(age_days: float, *, kind: str) -> float:
    """0..1 boost for neglected open work. Entities/people stay at 0."""
    if kind not in ("task", "commitment"):
        return 0.0
    if age_days <= AGING_OPEN_DAYS:
        return 0.0
    # Ramp: full signal after AGING_OPEN_DAYS + ramp window.
    from app.services.ranking.config import AGING_RAMP_DAYS
    return min(1.0, (age_days - AGING_OPEN_DAYS) / max(1.0, AGING_RAMP_DAYS))
