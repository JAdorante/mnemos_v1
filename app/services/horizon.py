"""Horizon strip — predicted-next attention (Track A4 / Field §13.1).

Heuristic-first (D4): calendar events within the horizon dominate. Resolve
attendees/projects from the event text, pull open commitments/tasks they own,
and surface ≤3 items with reasons. Confidence below min_p renders nothing.
"""
from __future__ import annotations

import re
import time
from typing import Any

_NAME = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")


def _cfg():
    try:
        from app.config import settings
        return settings.attention
    except Exception:
        class _D:
            horizon = True
            horizon_min_p = 0.5
            horizon_horizon_s = 90 * 60.0
        return _D()


def _parse_start(start, now: float) -> float | None:
    if start is None:
        return None
    if isinstance(start, (int, float)):
        return float(start)
    if isinstance(start, str) and start.strip():
        try:
            from datetime import datetime
            return datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def _people_index(store) -> list[tuple[str, int]]:
    try:
        return [(p["name"], int(p["id"])) for p in store.all_people()
                if (p.get("name") or "").strip()]
    except Exception:
        return []


def _entity_index(store) -> list[tuple[str, int]]:
    try:
        return [(e["name"], int(e["id"])) for e in store.all_entities()
                if (e.get("name") or "").strip()]
    except Exception:
        return []


def _resolve_in_text(text: str, people, entities) -> list[tuple[str, int, str]]:
    """Return (type, id, matched_name) for known roster hits in text."""
    t = text or ""
    low = t.lower()
    hits = []
    for name, pid in sorted(people, key=lambda x: -len(x[0])):
        if name.lower() in low:
            hits.append(("person", pid, name))
    for name, eid in sorted(entities, key=lambda x: -len(x[0])):
        if name.lower() in low:
            hits.append(("entity", eid, name))
    return hits


def _next_calendar_events(store, *, now: float, horizon_s: float) -> list[dict]:
    try:
        events = store.recent_events(source_substr="calendar", limit=80)
    except Exception:
        return []
    out = []
    for ev in events:
        meta = ev.get("meta") or {}
        if isinstance(meta, str):
            try:
                import json
                meta = json.loads(meta)
            except Exception:
                meta = {}
        start_ts = _parse_start(meta.get("start"), now)
        if start_ts is None:
            continue
        dt = start_ts - now
        if dt < -5 * 60 or dt > horizon_s:
            continue
        title = (meta.get("summary") or ev.get("summary") or ev.get("raw")
                 or "upcoming event")
        out.append({
            "start_ts": start_ts,
            "when_s": dt,
            "title": str(title)[:120],
            "text": " ".join(str(x) for x in (
                ev.get("raw"), ev.get("summary"), meta.get("summary"),
                meta.get("location"),
            ) if x),
            "event_key": f"cal:{start_ts:.0f}:{(title or '')[:40]}",
        })
    out.sort(key=lambda e: e["when_s"])
    return out


def _open_work_for_person(store, person_name: str, *, limit: int = 4) -> list[dict]:
    name = (person_name or "").strip().lower()
    if not name:
        return []
    items = []
    try:
        facts = (store.list_facts(kind="commitment", status="open", limit=80)
                 + store.list_facts(kind="task", status="open", limit=80))
    except Exception:
        return []
    for f in facts:
        blob = " ".join(str(f.get(k) or "") for k in
                        ("owner", "from_person", "to_person", "text")).lower()
        if name not in blob:
            continue
        items.append(f)
        if len(items) >= limit:
            break
    return items


def predict(store=None, *, now: float | None = None, limit: int = 3) -> list[dict]:
    """Build ≤`limit` horizon items. Empty if below min_p or horizon disabled."""
    cfg = _cfg()
    if not getattr(cfg, "horizon", True):
        return []
    now = float(now if now is not None else time.time())
    min_p = float(getattr(cfg, "horizon_min_p", 0.5))
    horizon_s = float(getattr(cfg, "horizon_horizon_s", 90 * 60))

    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception:
            return []

    events = _next_calendar_events(store, now=now, horizon_s=horizon_s)
    people = _people_index(store)
    entities = _entity_index(store)
    items: list[dict] = []
    seen: set[str] = set()

    for ev in events:
        # Confidence rises as the event approaches (0.45 at horizon → 0.95 at start)
        proximity = 1.0 - max(0.0, float(ev["when_s"])) / max(1.0, horizon_s)
        p_event = 0.45 + 0.50 * proximity
        hits = _resolve_in_text(ev["text"], people, entities)
        people_hits = [h for h in hits if h[0] == "person"]
        entity_hits = [h for h in hits if h[0] == "entity"]

        # Primary: first person on the invite
        if people_hits:
            _, pid, pname = people_hits[0]
            key = f"person:{pid}"
            if key not in seen:
                reasons = [
                    f"calendar: {ev['title']} in {_fmt_when(ev['when_s'])}",
                ]
                if entity_hits:
                    reasons.append(f"about {entity_hits[0][2]}")
                work = _open_work_for_person(store, pname, limit=2)
                for w in work:
                    reasons.append(
                        f"open {w.get('kind') or 'item'}: "
                        f"{(w.get('text') or '')[:60]}")
                p = min(0.98, p_event + (0.08 if work else 0.0))
                if p >= min_p:
                    items.append({
                        "node_type": "person",
                        "node_id": pid,
                        "id": key,
                        "label": pname,
                        "p_need": round(p, 3),
                        "when_s": round(float(ev["when_s"]), 1),
                        "when_label": _fmt_when(ev["when_s"]),
                        "reason": reasons[:4],
                        "source": "calendar",
                        "event_key": ev["event_key"],
                        "event_title": ev["title"],
                    })
                    seen.add(key)

            # Related open work as its own chip (term sheet)
            for w in _open_work_for_person(store, people_hits[0][2], limit=2):
                fid = w.get("fact_id") or w.get("id")
                if fid is None:
                    continue
                key = f"fact:{int(fid)}"
                if key in seen:
                    continue
                p = min(0.95, p_event + 0.05)
                if p < min_p:
                    continue
                items.append({
                    "node_type": "fact",
                    "node_id": int(fid),
                    "id": key,
                    "label": (w.get("text") or "open item")[:80],
                    "p_need": round(p, 3),
                    "when_s": round(float(ev["when_s"]), 1),
                    "when_label": _fmt_when(ev["when_s"]),
                    "reason": [
                        f"calendar: {ev['title']} in {_fmt_when(ev['when_s'])}",
                        f"open {w.get('kind') or 'work'} with {people_hits[0][2]}",
                    ],
                    "source": "calendar",
                    "event_key": ev["event_key"],
                    "event_title": ev["title"],
                })
                seen.add(key)

        elif entity_hits:
            _, eid, ename = entity_hits[0]
            key = f"entity:{eid}"
            if key not in seen and p_event >= min_p:
                items.append({
                    "node_type": "entity",
                    "node_id": eid,
                    "id": key,
                    "label": ename,
                    "p_need": round(p_event, 3),
                    "when_s": round(float(ev["when_s"]), 1),
                    "when_label": _fmt_when(ev["when_s"]),
                    "reason": [
                        f"calendar: {ev['title']} in {_fmt_when(ev['when_s'])}",
                    ],
                    "source": "calendar",
                    "event_key": ev["event_key"],
                    "event_title": ev["title"],
                })
                seen.add(key)

        if len(items) >= limit:
            break

    # Plan 4.3: open loops fill remaining horizon slots (also when no calendar).
    if len(items) < limit:
        try:
            from app.services import open_loops
            for it in open_loops.horizon_items(
                    store, now=now, limit=limit - len(items), exclude=seen):
                key = it.get("id") or f"{it.get('node_type')}:{it.get('node_id')}"
                if key in seen:
                    continue
                if float(it.get("p_need") or 0) < min_p:
                    continue
                items.append(it)
                seen.add(key)
                if len(items) >= limit:
                    break
        except Exception as exc:
            print(f"[horizon] open_loops skipped ({exc}).")

    items.sort(key=lambda x: (-float(x["p_need"]), float(x.get("when_s") or 0)))
    return items[:limit]


def _fmt_when(when_s: float) -> str:
    s = float(when_s)
    if s <= 0:
        return "now"
    if s < 90:
        return f"{int(s)}s"
    mins = int(round(s / 60.0))
    if mins < 60:
        return f"{mins} min"
    hrs = mins / 60.0
    return f"{hrs:.1f} h"


def refresh(store=None, *, now: float | None = None) -> list[dict]:
    """Predict, persist, and record horizon impressions."""
    items = predict(store=store, now=now, limit=3)
    if store is None:
        return items
    try:
        rows = []
        for it in items:
            rows.append({
                "ts": now or time.time(),
                "node_type": it["node_type"],
                "node_id": it["node_id"],
                "p_need": it["p_need"],
                "when_s": it["when_s"],
                "reason": {
                    "label": it.get("label"),
                    "reasons": it.get("reason") or [],
                    "when_label": it.get("when_label"),
                    "event_title": it.get("event_title"),
                    "loop_kind": it.get("loop_kind"),
                    "evidence": it.get("evidence") or {},
                },
                "source": it.get("source") or "calendar",
                "event_key": it.get("event_key"),
            })
        store.replace_attention_predictions(rows)
    except Exception as exc:
        print(f"[horizon] persist skipped ({exc}).")
    try:
        from app.services import open_loops
        open_loops.mark_surfaced(store, items, now=now)
    except Exception:
        pass
    try:
        from app.services.attention_ledger import attention_ledger
        attention_ledger.record_horizon(items, store)
    except Exception:
        pass
    # Pre-warm Now-Context so WM lights the neighborhood
    try:
        from app.services.now_context import now_context
        keys = [(it["node_type"], int(it["node_id"])) for it in items
                if it.get("node_id") is not None]
        if keys:
            now_context.observe(keys, weight=0.85, source="horizon", now=now)
    except Exception:
        pass
    return items


def strip(store=None, *, refresh_first: bool = True) -> dict[str, Any]:
    """Payload for /field/state and /field/predictions."""
    cfg = _cfg()
    if refresh_first:
        items = refresh(store=store)
    else:
        items = predict(store=store)
    return {
        "enabled": bool(getattr(cfg, "horizon", True)),
        "min_p": float(getattr(cfg, "horizon_min_p", 0.5)),
        "items": [
            {
                "id": it.get("id"),
                "label": it.get("label"),
                "p_need": it.get("p_need"),
                "when_s": it.get("when_s"),
                "when_label": it.get("when_label"),
                "reason": it.get("reason") or [],
                "source": it.get("source"),
                "event_title": it.get("event_title"),
                "loop_kind": it.get("loop_kind"),
                "evidence": it.get("evidence") or {},
                "fact_id": it.get("fact_id"),
            }
            for it in items
        ],
    }


def dismiss(store, node_id: str) -> bool:
    """Strong negative: mark matching prediction dismissed + snooze open loops."""
    ok = False
    fact_id: int | None = None
    try:
        # Accept "fact:12", "12", or "loop:waiting_on_them:12"
        s = (node_id or "").strip()
        if s.startswith("fact:"):
            fact_id = int(s.split(":", 1)[1])
        elif s.startswith("loop:") and s.count(":") >= 2:
            fact_id = int(s.rsplit(":", 1)[1])
        elif s.isdigit():
            fact_id = int(s)
    except (TypeError, ValueError):
        fact_id = None
    try:
        rows = store.list_attention_predictions(limit=20)
        for r in rows:
            nid = f"{r.get('node_type')}:{r.get('node_id')}"
            if nid == node_id or str(r.get("id")) == str(node_id):
                store.dismiss_attention_prediction(int(r["id"]))
                ok = True
                if fact_id is None and r.get("node_type") == "fact":
                    try:
                        fact_id = int(r["node_id"])
                    except Exception:
                        pass
    except Exception:
        pass
    if fact_id is not None:
        try:
            from app.services import open_loops
            if open_loops.snooze(store, int(fact_id), kind="horizon_dismiss"):
                ok = True
        except Exception:
            pass
    try:
        from app.services.attention_ledger import attention_ledger
        attention_ledger.outcome(node_id, "dismiss",
                                 detail={"surface": "horizon"}, store=store)
    except Exception:
        pass
    return ok
