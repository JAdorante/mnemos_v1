"""Now-Context feeder (Track A2) — keep the present filled from the substrate.

The Now-Context itself is a dumb seed bag; this module is what *feeds* it from
signals the product already captures, with no LLM in the loop:

  chat / engagement / explicit   already wired (grounding, ledger, /field/context)
  speech                        settled turns → resolved people (last 30 min)
  desktop activity              recent app focus → matching entities
  calendar horizon              phone.calendar events starting within 90 min
                                → people/projects named in the summary

Event-driven on the bus (audio/vision/desktop/notification) plus a 60 s tick
for calendar ramps. Best-effort and never raises into capture or ranking.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

from app.events import Modality, bus
from app.services.now_context import now_context

# How far back speech counts as "still the present."
SPEECH_WINDOW_S = 30 * 60.0
# Calendar events starting within this window seed their attendees/topics.
CALENDAR_HORIZON_S = 90 * 60.0
# Tick for calendar ramps + activity refresh when the bus is quiet.
TICK_S = 60.0
# Don't re-seed the same source more often than this (bus can be chatty).
MIN_GAP_S = 15.0

_NAME = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_last_feed_ts = 0.0
_attached = False


def _people_index(store) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for p in store.all_people():
            name = (p.get("name") or "").strip()
            if name:
                out[name.lower()] = int(p["id"])
            for a in (p.get("aliases") or []):
                if isinstance(a, str) and a.strip():
                    out[a.strip().lower()] = int(p["id"])
    except Exception:
        pass
    return out


def _entity_index(store) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for e in store.all_entities():
            name = (e.get("name") or "").strip()
            if name:
                out[name.lower()] = int(e["id"])
    except Exception:
        pass
    return out


def _resolve_names(text: str, people: dict[str, int],
                   entities: dict[str, int]) -> list[tuple[str, int]]:
    keys: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    low = (text or "").lower()
    # Prefer longer names first so "Marc Chen" beats "Marc".
    for name, pid in sorted(people.items(), key=lambda kv: -len(kv[0])):
        if len(name) < 3:
            continue
        if name in low:
            key = ("person", pid)
            if key not in seen:
                keys.append(key)
                seen.add(key)
    for name, eid in sorted(entities.items(), key=lambda kv: -len(kv[0])):
        if len(name) < 3:
            continue
        if name in low:
            key = ("entity", eid)
            if key not in seen:
                keys.append(key)
                seen.add(key)
    return keys


def feed_from_speech(store, *, now: float | None = None) -> int:
    """Seed people named in recent turns."""
    now = now or time.time()
    try:
        turns = store.recent_turns(limit=40)
    except Exception:
        return 0
    people = _people_index(store)
    entities = _entity_index(store)
    keys: list[tuple[str, int]] = []
    for t in turns:
        ts = float(t.get("end") or t.get("start") or 0)
        if ts and now - ts > SPEECH_WINDOW_S:
            continue
        text = t.get("text") or ""
        keys.extend(_resolve_names(text, people, entities))
    if not keys:
        return 0
    # Dedup preserving order
    uniq: list[tuple[str, int]] = []
    seen: set = set()
    for k in keys:
        if k not in seen:
            uniq.append(k)
            seen.add(k)
    now_context.observe(uniq[:24], weight=0.7, source="speech", now=now)
    return len(uniq)


def feed_from_activity(store, *, now: float | None = None) -> int:
    """Seed entities whose names appear in the current desktop activity line."""
    now = now or time.time()
    try:
        from app.services.activity import describe_recent
        lines = describe_recent(store=store, limit=3)
    except Exception:
        lines = []
    if not lines:
        return 0
    entities = _entity_index(store)
    people = _people_index(store)
    keys: list[tuple[str, int]] = []
    for line in lines:
        keys.extend(_resolve_names(str(line), people, entities))
    if not keys:
        return 0
    uniq = list(dict.fromkeys(keys))
    now_context.observe(uniq[:16], weight=0.55, source="activity", now=now)
    return len(uniq)


def feed_from_calendar(store, *, now: float | None = None) -> int:
    """Seed people/projects named in calendar events starting within 90 min.

    Calendar landings are memory events with source=phone.calendar and
    meta.start; we also accept summary text matching for older rows.
    """
    now = now or time.time()
    try:
        events = store.recent_events(source_substr="calendar", limit=80)
    except Exception:
        return 0
    people = _people_index(store)
    entities = _entity_index(store)
    keys: list[tuple[str, int]] = []
    for ev in events:
        src = (ev.get("source") or "")
        if "calendar" not in src:
            continue
        meta = ev.get("meta") or {}
        if isinstance(meta, str):
            try:
                import json
                meta = json.loads(meta)
            except Exception:
                meta = {}
        start = meta.get("start")
        start_ts = _parse_start(start, now)
        # Ramp: only events already started (grace 5 min) or starting within horizon.
        if start_ts is None:
            # No parseable start — still allow very fresh calendar events.
            ets = float(ev.get("time") or 0)
            if not ets or now - ets > 6 * 3600:
                continue
            weight = 0.5
        else:
            dt = start_ts - now
            if dt > CALENDAR_HORIZON_S or dt < -30 * 60:
                continue
            # Weight rises as the event approaches (0.4 at 90m → 1.0 at start).
            weight = max(0.4, min(1.0, 1.0 - (max(0.0, dt) / CALENDAR_HORIZON_S) * 0.6))
        text = " ".join(str(x) for x in (
            ev.get("raw"), ev.get("summary"), meta.get("summary"),
        ) if x)
        found = _resolve_names(text, people, entities)
        if found:
            now_context.observe(found, weight=weight, source="calendar", now=now)
            keys.extend(found)
    return len(keys)


def _parse_start(value: Any, now: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        from datetime import datetime
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def feed_once(store=None) -> dict[str, int]:
    """One pass over all substrate seed sources. Throttled."""
    global _last_feed_ts
    now = time.time()
    with _lock:
        if now - _last_feed_ts < MIN_GAP_S:
            return {"throttled": 1}
        _last_feed_ts = now
    if store is None:
        from app.storage import get_store
        store = get_store()
    out = {
        "speech": feed_from_speech(store, now=now),
        "activity": feed_from_activity(store, now=now),
        "calendar": feed_from_calendar(store, now=now),
    }
    # Context moved → refresh WM so field/chat share the new seeds without
    # waiting on the next constellation poll.
    if any(int(out.get(k) or 0) > 0 for k in ("speech", "activity", "calendar")):
        try:
            from app.services import working_memory as _wm
            _wm.ensure_fresh(store, force=True)
        except Exception as exc:
            print(f"[context_feeder] wm refresh skipped ({exc}).")
        try:
            from app.services import horizon as _horizon
            _horizon.refresh(store)
        except Exception as exc:
            print(f"[context_feeder] horizon skipped ({exc}).")
    return out


def _on_bus(ev) -> None:
    """Cheap trigger: when perception moves, refresh context shortly after."""
    try:
        if ev.modality in (Modality.AUDIO, Modality.VISION, Modality.INPUT,
                           Modality.NOTIFICATION, Modality.SYSTEM):
            # Defer to the tick/lock throttle — just nudge a feed.
            feed_once()
    except Exception as exc:
        print(f"[context_feeder] bus skipped ({exc}).")


def _tick_loop() -> None:
    while not _stop.wait(TICK_S):
        try:
            feed_once()
        except Exception as exc:
            print(f"[context_feeder] tick skipped ({exc}).")


def attach() -> None:
    """Subscribe to the bus and start the 60 s calendar/activity tick."""
    global _attached, _thread
    with _lock:
        if _attached:
            return
        bus.subscribe(_on_bus)
        _stop.clear()
        _thread = threading.Thread(target=_tick_loop, name="context-feeder",
                                   daemon=True)
        _thread.start()
        _attached = True
    print("[context_feeder] Now-Context feeder attached (bus + 60s tick).")
    try:
        feed_once()
    except Exception:
        pass


def detach() -> None:
    global _attached, _thread
    _stop.set()
    _attached = False
    _thread = None


def status() -> dict[str, Any]:
    seeds = now_context.seeds()
    return {
        "attached": _attached,
        "generation": now_context.generation,
        "seed_count": len(seeds),
        "top_seeds": [
            {"id": f"{t}:{i}", "weight": round(w, 3)}
            for (t, i), w in sorted(seeds.items(), key=lambda kv: -kv[1])[:8]
        ],
    }
