"""Meeting notepad jots (Meeting Layer P2).

Each jot is a first-class TEXT event (`source=meeting.note`). Jots do **not**
run through the chat extractor on their own — they are importance anchors for
co-timed transcript turns (±90s). Span faithfulness still requires a verbatim
quote from the spoken text; the note only steers attention.
"""
from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.storage import Store

SOURCE = "meeting.note"
NOTE_WINDOW_S = 90.0
# Jots can be short ("pricing", "pushback") — lower floor than chat.user.
MIN_CHARS = int(os.getenv("QUILL_MEETING_NOTE_MIN_CHARS", "2"))


def enabled() -> bool:
    return os.getenv("QUILL_MEETING_NOTES", "1") not in ("0", "false", "False")


def clean(text: str) -> str | None:
    t = (text or "").strip()
    if len(t) < MIN_CHARS:
        return None
    return t


def ingest(
    text: str, *,
    store: "Store | None" = None,
    session_id: int | None = None,
    session_hint: dict | None = None,
    now: float | None = None,
) -> int | None:
    """Store one notepad jot. Returns event id, or None when skipped.

    No extract job — jots anchor co-timed speech extraction, they are not
    themselves fact sources (span-faithfulness invariant).
    """
    if not enabled():
        return None
    body = clean(text)
    if body is None:
        return None
    try:
        from app.events import Event, Modality
        from app.services import confidence as _conf
        from app.services.attachments import _index_event
        from app.storage import get_store

        store = store or get_store()
        ts = float(now if now is not None else time.time())
        hint = dict(session_hint or {})
        if session_id is not None:
            hint["session_id"] = int(session_id)
        if not hint.get("session_id"):
            # Best-effort: attach the active calendar-linked session.
            try:
                active = active_session(store, ts)
                if active:
                    hint["session_id"] = active.get("id")
                    if active.get("calendar_event_id"):
                        hint["calendar_event_id"] = active["calendar_event_id"]
                    meta = active.get("meeting_meta") or {}
                    if meta.get("title"):
                        hint["title"] = meta["title"]
            except Exception:
                pass
        meta = {
            "section": "notepad",
            "session_hint": hint,
        }
        ev = Event(
            time=ts, modality=Modality.TEXT, raw=body,
            summary=f"[meeting.note] {body[:120]}", source=SOURCE,
            meta=meta,
        )
        _conf.attach(ev, _conf.ACCEPTED, capture=1.0)
        anchor = store.insert(ev)
        try:
            _index_event(anchor, ev)
        except Exception:
            pass
        return anchor
    except Exception as exc:
        print(f"[meeting_notes] skipped ({exc}).")
        return None


def active_session(store: "Store", now: float | None = None) -> dict | None:
    """Calendar-linked session overlapping `now`, else longest open session."""
    now = float(now if now is not None else time.time())
    try:
        sessions = store.recent_sessions(limit=40)
    except Exception:
        return None
    overlapping = [
        s for s in sessions
        if float(s.get("start") or 0) - 120 <= now <= float(s.get("end") or 0) + 300
    ]
    if not overlapping:
        return None
    linked = [s for s in overlapping if s.get("calendar_event_id")]
    pool = linked or overlapping
    pool.sort(key=lambda s: -(float(s.get("end") or 0) - float(s.get("start") or 0)))
    return pool[0]


def jots_near(
    store: "Store", center: float, *,
    window_s: float = NOTE_WINDOW_S, limit: int = 20,
) -> list[dict]:
    """meeting.note events with time in [center±window_s], oldest-first."""
    t0 = float(center) - float(window_s)
    t1 = float(center) + float(window_s)
    try:
        rows = store.events_in_window(
            t0, t1, source=SOURCE, modality="text", limit=limit)
    except Exception:
        # Fallback for older Store without the helper.
        try:
            rows = store.recent_events(source_substr=SOURCE, since=t0, limit=80)
            rows = [r for r in rows
                    if r.get("time") is not None and float(r["time"]) <= t1]
        except Exception:
            return []
    rows = sorted(rows, key=lambda r: float(r.get("time") or 0))
    return rows


def jot_texts_near(store: "Store", center: float, *,
                   window_s: float = NOTE_WINDOW_S) -> list[str]:
    return [
        (r.get("raw") or "").strip()
        for r in jots_near(store, center, window_s=window_s)
        if (r.get("raw") or "").strip()
    ]


def jot_times(store: "Store", *, since: float | None = None,
              limit: int = 500) -> list[float]:
    """Unix timestamps of recent meeting notes (for ranking adjacency)."""
    try:
        rows = store.recent_events(
            source_substr=SOURCE, since=since, limit=limit)
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            out.append(float(r["time"]))
        except (TypeError, ValueError, KeyError):
            continue
    out.sort()
    return out


def note_adjacent_score(ts: float | None, note_times: list[float], *,
                        window_s: float = NOTE_WINDOW_S) -> float:
    """1.0 when `ts` falls within ±window_s of any jot, else 0.0."""
    if ts is None or not note_times:
        return 0.0
    t = float(ts)
    # note_times sorted — binary search neighborhood
    import bisect
    i = bisect.bisect_left(note_times, t - window_s)
    while i < len(note_times) and note_times[i] <= t + window_s:
        if abs(note_times[i] - t) <= window_s:
            return 1.0
        i += 1
    return 0.0


def format_anchor_block(jots: list[str]) -> str:
    """Prompt fragment appended to an extract user message."""
    lines = [j.strip() for j in jots if (j or "").strip()]
    if not lines:
        return ""
    body = "\n".join(f'- "{j}"' for j in lines[:6])
    return (
        "USER'S LIVE NOTE AT THIS MOMENT (importance / disambiguation only — "
        "NOT a source of quotes; source_span must still be a verbatim substring "
        "of the spoken transcript above):\n"
        f"{body}"
    )


def recent_jots(store: "Store", *, limit: int = 30,
                session_id: int | None = None) -> list[dict[str, Any]]:
    """Recent notepad jots for the Today UI."""
    try:
        rows = store.recent_events(source_substr=SOURCE, limit=max(limit, 80))
    except Exception:
        return []
    out = []
    for r in rows:
        meta = r.get("meta") or {}
        hint = meta.get("session_hint") or {}
        if session_id is not None and hint.get("session_id") != session_id:
            continue
        out.append({
            "id": r.get("id"),
            "time": r.get("time"),
            "text": (r.get("raw") or "").strip(),
            "session_id": hint.get("session_id"),
            "title": hint.get("title") or "",
        })
        if len(out) >= limit:
            break
    return out
