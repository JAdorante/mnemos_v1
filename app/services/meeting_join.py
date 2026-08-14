"""Calendar ↔ session join (Meeting Layer P1).

When a session's time window overlaps a calendar event (≥50% of the shorter
window, or ≥10 minutes), the session inherits the event's title, attendees,
and organizer. Runs inside `sessions.rebuild()` — pure match + mutate, then
`replace_sessions` persists. Rebuild-safe / idempotent.

Also normalizes `phone.calendar` memory events into `calendar_events` so the
join has a structured index (attendees included when CalDAV parse provides them).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.storage import Store

MIN_OVERLAP_S = 10 * 60          # ≥10 minutes
MIN_OVERLAP_FRAC = 0.50         # ≥50% of the shorter window


def _store(store: "Store | None" = None):
    if store is not None:
        return store
    from app.storage import get_store
    return get_store()


def overlap_seconds(a0: float, a1: float, b0: float, b1: float) -> float:
    """Intersection length of [a0,a1] and [b0,b1]; 0 if disjoint."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def overlap_qualifies(
    overlap: float, sess_dur: float, evt_dur: float, *,
    min_frac: float = MIN_OVERLAP_FRAC, min_s: float = MIN_OVERLAP_S,
) -> bool:
    """True when overlap is ≥ min_s OR ≥ min_frac of the shorter interval."""
    if overlap <= 0:
        return False
    if overlap >= min_s:
        return True
    shorter = min(max(sess_dur, 1e-6), max(evt_dur, 1e-6))
    return (overlap / shorter) >= min_frac


def to_unix(value: Any) -> float | None:
    """Parse calendar start/end (datetime, date, ISO string, unix) → unix ts."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dt.datetime):
        d = value
        if d.tzinfo is None:
            d = d.astimezone()
        return d.timestamp()
    if isinstance(value, dt.date):
        # All-day: local midnight → +1 day.
        d0 = dt.datetime.combine(value, dt.time.min).astimezone()
        return d0.timestamp()
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        # Tolerate trailing Z and space-separated ISO from str(datetime).
        s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        d = dt.datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.astimezone()
        return d.timestamp()
    except ValueError:
        return None


def event_window(ev: dict) -> tuple[float, float] | None:
    """(start_ts, end_ts) for a calendar_events row or sync dict."""
    start = to_unix(ev.get("start"))
    if start is None:
        return None
    end = to_unix(ev.get("end"))
    if end is None or end <= start:
        # Default 1h timed meeting; all-day → 24h.
        end = start + (24 * 3600 if ev.get("all_day") else 3600)
    return start, end


def best_event_for_session(
    sess_start: float, sess_end: float, events: list[dict], *,
    min_frac: float = MIN_OVERLAP_FRAC, min_s: float = MIN_OVERLAP_S,
) -> dict | None:
    """Pick the best overlapping calendar event; ties → closer start time.

    Skips all-day events (not meeting-mode signal). Returns the event dict
    augmented with `_overlap` / `_start` / `_end` for callers.
    """
    sess_dur = max(0.0, sess_end - sess_start)
    candidates: list[tuple[float, float, dict]] = []
    for ev in events:
        if ev.get("all_day"):
            continue
        win = event_window(ev)
        if win is None:
            continue
        e0, e1 = win
        ov = overlap_seconds(sess_start, sess_end, e0, e1)
        if not overlap_qualifies(ov, sess_dur, e1 - e0,
                                 min_frac=min_frac, min_s=min_s):
            continue
        start_delta = abs(sess_start - e0)
        enriched = {**ev, "_overlap": ov, "_start": e0, "_end": e1}
        # Sort key: prefer more overlap, then closer start (smaller delta).
        candidates.append((-ov, start_delta, enriched))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))
    return candidates[0][2]


def meeting_meta_from_event(ev: dict) -> dict:
    """Compact JSON blob stored on the session row."""
    attendees = list(ev.get("attendees") or [])
    organizer = ev.get("organizer")
    return {
        "title": (ev.get("title") or ev.get("summary") or "")[:200],
        "attendees": attendees,
        "organizer": organizer,
        "calendar": ev.get("calendar") or "",
        "location": ev.get("location") or "",
    }


def attach_calendar(
    sessions: list, events: list[dict], *,
    min_frac: float = MIN_OVERLAP_FRAC, min_s: float = MIN_OVERLAP_S,
) -> int:
    """Mutate Session objects in place with calendar_event_id + meeting_meta.

    Returns the number of sessions linked. Idempotent — re-running with the
    same inputs yields the same links.
    """
    linked = 0
    # One event → at most one session (greedy by session start). If two
    # sessions both qualify for the same event, the closer start wins and
    # the other falls through to its next-best event (or none).
    claimed: set[str] = set()
    ordered = sorted(sessions, key=lambda s: getattr(s, "start", 0.0) or 0.0)
    for sess in ordered:
        # Clear stale links so rebuild never retains a prior join.
        sess.calendar_event_id = None
        sess.meeting_meta = None
        available = [e for e in events if (e.get("id") or "") not in claimed]
        best = best_event_for_session(
            float(sess.start), float(sess.end), available,
            min_frac=min_frac, min_s=min_s,
        )
        if best is None:
            continue
        eid = best.get("id") or ""
        if not eid:
            continue
        sess.calendar_event_id = eid
        sess.meeting_meta = meeting_meta_from_event(best)
        claimed.add(eid)
        linked += 1
    return linked


def normalize_from_memory_events(store: "Store | None" = None) -> int:
    """Backfill `calendar_events` from phone.calendar memory events.

    Historical landings lack attendees (pre-P1 parse); still useful for
    title/window join. Returns upsert count.
    """
    store = _store(store)
    try:
        rows = store.recent_events(source_substr="calendar", limit=2000)
    except Exception:
        return 0
    n = 0
    for ev in rows:
        meta = ev.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if (meta.get("origin") or "") != "icloud" and "uid" not in meta:
            # Horizon/feeder sometimes uses source_substr=calendar broadly.
            if not meta.get("uid"):
                continue
        uid = (meta.get("uid") or "").strip()
        cal = (meta.get("calendar") or "").strip() or "Home"
        if not uid:
            continue
        start = to_unix(meta.get("start"))
        if start is None:
            continue
        end = to_unix(meta.get("end")) or (start + 3600)
        title = (meta.get("summary") or meta.get("title") or "")
        if not title:
            # Parse from raw "Calendar (Work): Title — Mon ..."
            raw = ev.get("raw") or ev.get("summary") or ""
            m = re.search(r"Calendar\s*\([^)]*\):\s*(.+?)\s*—", raw)
            title = (m.group(1).strip() if m else raw)[:200]
        event_id = f"{cal}|{uid}"
        try:
            store.upsert_calendar_event(
                event_id=event_id, calendar=cal, uid=uid, title=title,
                start=start, end=end, all_day=bool(meta.get("all_day")),
                location=meta.get("location") or "",
                organizer=meta.get("organizer"),
                attendees=meta.get("attendees") or [],
                source_event_id=ev.get("id"),
                updated_at=ev.get("time"),
                join_url=meta.get("join_url") or "",
                provider=meta.get("provider") or "",
            )
            n += 1
        except Exception:
            continue
    return n


def upsert_from_sync_event(store: "Store", ev: dict, *,
                           source_event_id: int | None = None) -> str | None:
    """Write one CalDAV-parsed event into calendar_events. Returns event id."""
    uid = (ev.get("uid") or "").strip()
    cal = (ev.get("calendar") or "").strip() or "Home"
    if not uid:
        return None
    win = event_window(ev)
    if win is None:
        return None
    start, end = win
    event_id = f"{cal}|{uid}"
    store.upsert_calendar_event(
        event_id=event_id, calendar=cal, uid=uid,
        title=(ev.get("summary") or ev.get("title") or "")[:200],
        start=start, end=end, all_day=bool(ev.get("all_day")),
        location=ev.get("location") or "",
        organizer=ev.get("organizer"),
        attendees=ev.get("attendees") or [],
        source_event_id=source_event_id,
        join_url=ev.get("join_url") or "",
        provider=ev.get("provider") or "",
    )
    return event_id


def link_sessions(store: "Store | None" = None, sessions: list | None = None) -> int:
    """Attach calendar meta to a session list (or no-op if none given).

    Prefer calling `attach_calendar` from `sessions.rebuild` before
    `replace_sessions`. This helper is for ad-hoc re-link after calendar sync.
    """
    store = _store(store)
    normalize_from_memory_events(store)
    if sessions is None:
        return 0
    # Pad the window so early-join / late-end still find the event.
    if not sessions:
        return 0
    t0 = min(float(s.start) for s in sessions) - 3600
    t1 = max(float(s.end) for s in sessions) + 3600
    events = store.list_calendar_events(start_min=t0, start_max=t1, limit=2000)
    return attach_calendar(sessions, events)


def attendees_for_time(
    store: "Store | None", start: float, end: float | None = None,
) -> list[dict]:
    """Attendee priors for a turn/window inside a calendar-linked session.

    Prefer the live MeetingSession roster (available from the first utterance)
    then fall back to derived session join.
    """
    try:
        from app.services import meeting_session as _ms
        live = _ms.attendees_live(store)
        if live:
            return live
    except Exception:
        pass
    store = _store(store)
    end = float(end if end is not None else start)
    try:
        sessions = store.recent_sessions(limit=300)
    except Exception:
        return []
    best = None
    best_ov = -1.0
    for s in sessions:
        if not s.get("calendar_event_id"):
            continue
        ov = overlap_seconds(float(start), float(end),
                             float(s["start"]), float(s["end"]))
        if ov <= 0:
            continue
        if ov > best_ov:
            best_ov = ov
            best = s
    if not best:
        return []
    meta = best.get("meeting_meta") or {}
    attendees = list(meta.get("attendees") or [])
    org = meta.get("organizer")
    if org and isinstance(org, dict):
        email = (org.get("email") or "").lower()
        if email and not any(
                (a.get("email") or "").lower() == email for a in attendees):
            attendees = [org, *attendees]
    return attendees
