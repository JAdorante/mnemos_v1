"""SessionConsolidator — group turns into coherent conversation/work blocks.

Consolidation (consolidation.py) merges utterances into *turns* (one thought).
This is the next level up: adjacent turns separated by only a short silence are
the same *session* — a phone call, a meeting, a work block — while a long quiet
gap starts a new one. A session is the natural unit that turns aren't:

  * session-level summary / reflection ("what happened in this block?") instead
    of re-reading hundreds of loose turns,
  * a scope for session-intent routing (#4's router, but over a whole block),
  * a cleaner provenance anchor for a digest than an arbitrary turn.

Like turns, sessions are a *derived, rebuildable* layer — raw events stay the
canonical truth, and every session links back to its member turn/event ids.
Grouping is a pure function (`group_sessions`) so it's trivially testable;
`rebuild()` recomputes the whole table from the current turns (idempotent).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import settings
from app.storage import Store, get_store

# Cap a single session's text so a marathon block doesn't bloat one row; the
# member ids still point at the full provenance.
_MAX_TEXT_CHARS = 8000


@dataclass
class Session:
    start: float
    end: float
    speakers: list[str] = field(default_factory=list)
    text: str = ""
    turn_ids: list[int] = field(default_factory=list)   # positional turn indices
    event_ids: list[int] = field(default_factory=list)  # flattened provenance
    n_turns: int = 0
    n_utterances: int = 0
    # Meeting Layer P1 — set by meeting_join.attach_calendar during rebuild.
    calendar_event_id: str | None = None
    meeting_meta: dict | None = None

    def to_dict(self) -> dict:
        return {
            "start": self.start, "end": self.end, "speakers": self.speakers,
            "text": self.text, "turn_ids": self.turn_ids,
            "event_ids": self.event_ids, "n_turns": self.n_turns,
            "n_utterances": self.n_utterances,
            "duration_s": round(self.end - self.start, 2),
            "calendar_event_id": self.calendar_event_id,
            "meeting_meta": self.meeting_meta,
        }


def group_sessions(turns: list[dict], max_gap_s: float) -> list[Session]:
    """Group time-sorted turn dicts into sessions. A new session starts when the
    gap from the previous turn's end to this turn's start exceeds `max_gap_s`.

    `turns` are dicts as returned by `store.recent_turns` (need `start`, `end`,
    `text`, `speaker`, `event_ids`, `n_utterances`). Input order is normalized to
    ascending start time here, so callers don't have to pre-sort."""
    ordered = sorted(turns, key=lambda t: t.get("start") or 0.0)
    sessions: list[Session] = []
    cur: Session | None = None
    for idx, t in enumerate(ordered):
        start = t.get("start") or 0.0
        end = t.get("end") or start
        text = (t.get("text") or "").strip()
        starts_new = cur is None or (start - cur.end) > max_gap_s
        if starts_new:
            cur = Session(start=start, end=end)
            sessions.append(cur)
        cur.end = max(cur.end, end)
        if text and len(cur.text) < _MAX_TEXT_CHARS:
            cur.text = (cur.text + "\n" + text).strip() if cur.text else text
        cur.turn_ids.append(idx)
        cur.event_ids.extend(int(e) for e in (t.get("event_ids") or []))
        spk = (t.get("speaker") or "").strip()
        if spk and spk not in cur.speakers:
            cur.speakers.append(spk)
        cur.n_turns += 1
        cur.n_utterances += int(t.get("n_utterances") or 0)
    return sessions


def rebuild(store: Store | None = None) -> int:
    """Recompute the sessions table from the current turns. Returns session count.
    Rebuilds turns first if they're empty, so a fresh DB doesn't no-op silently.

    Meeting Layer P1: after grouping, join overlapping calendar events so the
    session inherits title/attendees (idempotent — re-derived every rebuild).
    """
    store = store or get_store()
    if store.turn_count() == 0 and settings.consolidation.enabled:
        from app.services import consolidation
        consolidation.rebuild(store)
    turns = store.recent_turns(1_000_000)
    sessions = group_sessions(turns, settings.consolidation.session_gap_s)
    try:
        from app.services import meeting_join
        meeting_join.link_sessions(store, sessions)
    except Exception as exc:
        print(f"[sessions] calendar join skipped ({exc}).")
    store.replace_sessions(sessions)
    return len(sessions)
