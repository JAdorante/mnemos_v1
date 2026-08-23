"""First-class MeetingSession — calendar-anchored capture owner.

Inverts the old Meeting Layer posture: a session is spawned from a calendar
event (or a conferencing window as fallback), owns consent + roster + channel
tags until it ends, and only then does extraction see attendee priors live.
Derived ``sessions`` rows remain a rebuildable speech rollup; they do not own
this object (``replace_sessions`` would wipe a row written at T=0).

Ambient capture is unchanged outside an active/offered window. Remote
(system-audio) ingest is fail-closed until the user picks Skip / Transcript /
Receipts.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from app.storage import Store, get_store

CONSENT_PENDING = "pending"
CONSENT_SKIP = "skip"
CONSENT_TRANSCRIPT = "transcript_only"
CONSENT_RECEIPTS = "keep_receipts"
VALID_CONSENT = frozenset({
    CONSENT_PENDING, CONSENT_SKIP, CONSENT_TRANSCRIPT, CONSENT_RECEIPTS,
})
RECORD_CONSENT = frozenset({CONSENT_TRANSCRIPT, CONSENT_RECEIPTS})

STATUS_OFFERED = "offered"
STATUS_ACTIVE = "active"
STATUS_DECLINED = "declined"
STATUS_ENDED = "ended"

SOURCE_CALENDAR = "calendar"
SOURCE_WINDOW = "window_fallback"
SOURCE_MANUAL = "manual"

PROVIDERS = ("zoom", "meet", "teams", "unknown")

_JOIN_HOSTS = (
    ("zoom.us", "zoom"),
    ("zoom.com", "zoom"),
    ("meet.google.com", "meet"),
    ("teams.microsoft.com", "teams"),
    ("teams.live.com", "teams"),
)
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_BROWSER_SUFFIX = re.compile(
    r"\s*[-—–]\s*(google chrome|microsoft edge|firefox|safari|zoom)\s*$",
    re.I)
_WINDOW_PROVIDERS = (
    (re.compile(r"\bzoom\b", re.I), "zoom"),
    (re.compile(r"google meet|\bmeet\b", re.I), "meet"),
    (re.compile(r"microsoft teams|\bteams\b", re.I), "teams"),
    # Chrome Meet / Meet PWA often uses the calendar title as the window:
    # "EOW Team Call - Google Chrome" / popped-out "EOW Team Call".
    (re.compile(
        r"(?i)\b(team|staff|weekly|eow|all[- ]hands)\s+(call|meeting|sync)\b"),
     "meet"),
    (re.compile(
        r"(?i)\b(call|meeting|standup|sync)\s*[-—–]\s*"
        r"(google chrome|microsoft edge|zoom)\b"),
     "meet"),
)

POLL_S = float(os.environ.get("QUILL_MEETING_SESSION_POLL_S", "15"))
WINDOW_COOLDOWN_S = float(os.environ.get("QUILL_MEETING_WINDOW_COOLDOWN_S", "3600"))

_lock = threading.RLock()
_runtime: dict[str, Any] | None = None
_stop = threading.Event()
_thread: threading.Thread | None = None
# Tests inject a window-title provider; production uses Win32 foreground.
_window_title_fn: Callable[[], str] | None = None
_window_offered: dict[str, float] = {}  # provider -> last offer ts
_skip_logged: dict[str, float] = {}  # reason:title -> last log ts


def enabled() -> bool:
    return os.getenv("QUILL_MEETING_SESSION", "1") not in ("0", "false", "False")


def reset() -> None:
    """Drop in-memory runtime (tests). Does not touch the DB."""
    global _runtime
    with _lock:
        _runtime = None
        _window_offered.clear()
        _skip_logged.clear()


def set_window_title_fn(fn: Callable[[], str] | None) -> None:
    global _window_title_fn
    _window_title_fn = fn


# ---------------------------------------------------------------------------
# Conference URL / window detection
# ---------------------------------------------------------------------------
def extract_conference_link(*texts: str | None) -> tuple[str, str]:
    """Return (join_url, provider) from free text. Empty url → provider unknown."""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return "", "unknown"
    for m in _URL_RE.finditer(blob):
        url = m.group(0).rstrip(".,;>")
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        for suffix, provider in _JOIN_HOSTS:
            if host == suffix or host.endswith("." + suffix):
                return url, provider
    low = blob.lower()
    if "zoom" in low:
        return "", "zoom"
    if "google meet" in low or "meet.google" in low:
        return "", "meet"
    if "microsoft teams" in low or "teams.microsoft" in low:
        return "", "teams"
    return "", "unknown"


def _strip_browser_suffix(title: str) -> str:
    return _BROWSER_SUFFIX.sub("", (title or "").strip()).strip()


def provider_from_window(title: str) -> str | None:
    t = (title or "").strip()
    if not t:
        return None
    for rx, provider in _WINDOW_PROVIDERS:
        if rx.search(t):
            return provider
    return None


def _norm_title(s: str) -> str:
    return re.sub(r"\s+", " ", _strip_browser_suffix(s).lower())


def calendar_event_matching_window(
    events: list[dict], window_title: str, *, min_chars: int = 8,
) -> dict | None:
    """Bind a Chrome/Meet tab titled like the invite to that calendar row."""
    needle = _norm_title(window_title)
    if len(needle) < min_chars:
        return None
    best = None
    best_n = 0
    for ev in events:
        if ev.get("all_day"):
            continue
        ht = _norm_title(ev.get("title") or ev.get("summary") or "")
        if len(ht) < min_chars:
            continue
        if ht in needle or needle in ht:
            n = min(len(ht), len(needle))
            if n > best_n:
                best, best_n = ev, n
    return best


def recurrence_uid_of(calendar_event_id: str | None) -> str:
    if not calendar_event_id:
        return ""
    return str(calendar_event_id).split("#", 1)[0]


def _foreground_title() -> str:
    if _window_title_fn is not None:
        try:
            return (_window_title_fn() or "").strip()
        except Exception:
            return ""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return (buf.value or "").strip()
    except Exception:
        return ""


def _store(store: Store | None = None) -> Store:
    return store if store is not None else get_store()


def _lead_s() -> float:
    return float(os.environ.get("QUILL_MEETING_MODE_LEAD_S", "120"))


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
def current() -> dict[str, Any] | None:
    """Active or offered session, auto-expiring past ``t_end``."""
    with _lock:
        st = dict(_runtime) if _runtime else None
    if not st:
        return None
    until = st.get("t_end")
    status_ = st.get("status")
    if (status_ in (STATUS_ACTIVE, STATUS_OFFERED, STATUS_DECLINED)
            and until is not None and time.time() > float(until)):
        end(reason="expired")
        return None
    if status_ in (STATUS_ENDED,):
        return None
    return st


def status() -> dict[str, Any]:
    st = current() or {}
    return {
        "enabled": enabled(),
        "active": st.get("status") == STATUS_ACTIVE,
        "pending": st.get("status") == STATUS_OFFERED,
        "status": st.get("status") or "",
        "consent": st.get("consent") or "",
        "title": st.get("title") or "",
        "id": st.get("id"),
        "calendar_event_id": st.get("calendar_event_id"),
        "provider": st.get("provider") or "",
        "source": st.get("source") or "",
        "t_start": st.get("t_start"),
        "t_end": st.get("t_end"),
        "attendee_count": len(st.get("attendees") or []),
        "channel_note": (
            "Remote audio is whole-device loopback (not Zoom-only) — "
            "other playback may land in the transcript."
            if st.get("status") == STATUS_ACTIVE else ""
        ),
    }


def attendees_live(store: Store | None = None) -> list[dict]:
    """Roster of the live session (active or offered). Empty if none."""
    st = current()
    if not st:
        return []
    out = list(st.get("attendees") or [])
    org = st.get("organizer")
    if org and isinstance(org, dict):
        email = (org.get("email") or "").lower()
        if email and not any((a.get("email") or "").lower() == email for a in out):
            out = [org, *out]
    return out


def asr_extra_terms() -> list[str]:
    names: list[str] = []
    for a in attendees_live():
        n = (a.get("name") or "").strip()
        if n:
            names.append(n)
    return names


def _channel_of(source: str) -> str:
    src = (source or "")
    if src.startswith("audio.system"):
        return "remote"
    if src.startswith("audio.") or src in ("audio.whisper", "audio.skipped"):
        return "mic"
    return "other"


def should_ingest(source: str, *, now: float | None = None) -> bool:
    """Fail-closed remote ingest while a meeting is offered/skipped.

    Ambient path (no live session) is unchanged *except* in meeting-first
    mode, where mic/loopback events outside the calendar window + pad are
    dropped so the always-on mic never records between meetings.
    """
    try:
        from app.services import first_run
        if not first_run.audio_event_allowed(source, now):
            return False
    except Exception:
        pass
    st = current()
    if not st:
        return True
    status_ = st.get("status")
    consent = st.get("consent")
    ch = _channel_of(source)
    if ch == "other":
        return True
    if status_ == STATUS_OFFERED or consent == CONSENT_PENDING:
        return ch != "remote"
    if status_ == STATUS_DECLINED or consent == CONSENT_SKIP:
        return False
    if status_ == STATUS_ACTIVE and consent in RECORD_CONSENT:
        return True
    return True


def stamp_event(event) -> Any:
    """Attach meeting_session_id + audio_channel when a session is recording."""
    st = current()
    if not st or st.get("status") != STATUS_ACTIVE:
        return event
    if st.get("consent") not in RECORD_CONSENT:
        return event
    meta = event.meta if isinstance(getattr(event, "meta", None), dict) else None
    if meta is None:
        try:
            event.meta = {}
            meta = event.meta
        except Exception:
            return event
    meta["meeting_session_id"] = st.get("id")
    src = getattr(event, "source", "") or ""
    ch = _channel_of(src)
    if ch in ("mic", "remote"):
        meta["audio_channel"] = ch
    return event


def speaker_space(source: str) -> str:
    """``self`` | ``remote`` | ``default`` for diarization during a recording."""
    st = current()
    if not st or st.get("status") != STATUS_ACTIVE:
        return "default"
    if st.get("consent") not in RECORD_CONSENT:
        return "default"
    ch = _channel_of(source)
    if ch == "mic":
        return "self"
    if ch == "remote":
        return "remote"
    return "default"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _row_to_runtime(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "calendar_event_id": row.get("calendar_event_id"),
        "recurrence_uid": row.get("recurrence_uid") or "",
        "title": row.get("title") or "",
        "attendees": list(row.get("attendees") or []),
        "organizer": row.get("organizer"),
        "join_url": row.get("join_url") or "",
        "provider": row.get("provider") or "unknown",
        "source": row.get("source") or SOURCE_CALENDAR,
        "consent": row.get("consent") or CONSENT_PENDING,
        "status": row.get("status") or STATUS_OFFERED,
        "t_start": row.get("t_start"),
        "t_end": row.get("t_end"),
        "entered_at": row.get("entered_at"),
        "ended_at": row.get("ended_at"),
    }


def _set_runtime(row: dict[str, Any] | None) -> None:
    global _runtime
    with _lock:
        _runtime = _row_to_runtime(row) if row else None


def last_consent_for_series(store: Store, recurrence_uid: str) -> str | None:
    if not recurrence_uid:
        return None
    try:
        rows = store.list_meeting_sessions(
            recurrence_uid=recurrence_uid, limit=20)
    except Exception:
        return None
    for r in rows:
        c = r.get("consent")
        if c in (CONSENT_SKIP, CONSENT_TRANSCRIPT, CONSENT_RECEIPTS):
            return c
    return None


def spawn(
    store: Store | None = None, *,
    calendar_event_id: str | None = None,
    title: str = "",
    attendees: list | None = None,
    organizer: dict | None = None,
    join_url: str = "",
    provider: str = "unknown",
    source: str = SOURCE_CALENDAR,
    t_start: float | None = None,
    t_end: float | None = None,
    consent: str = CONSENT_PENDING,
    status: str = STATUS_OFFERED,
) -> dict[str, Any]:
    store = _store(store)
    now = time.time()
    rec = recurrence_uid_of(calendar_event_id)
    row = store.insert_meeting_session(
        calendar_event_id=calendar_event_id,
        recurrence_uid=rec,
        title=title,
        attendees=attendees or [],
        organizer=organizer,
        join_url=join_url,
        provider=provider if provider in PROVIDERS else "unknown",
        source=source,
        consent=consent,
        status=status,
        t_start=t_start,
        t_end=t_end,
        created_at=now,
    )
    _set_runtime(row)
    return _row_to_runtime(row)


def _patch_row(store: Store, session_id: int, **fields: Any) -> dict[str, Any]:
    row = store.update_meeting_session(session_id, **fields)
    _set_runtime(row)
    return _row_to_runtime(row) if row else {}


def decide(
    choice: str, *,
    store: Store | None = None,
    session_id: int | None = None,
    remember: bool = True,
) -> dict[str, Any]:
    """Apply Skip / transcript_only / keep_receipts to the live (or given) session."""
    if choice not in (CONSENT_SKIP, CONSENT_TRANSCRIPT, CONSENT_RECEIPTS):
        return {"ok": False, "error": f"invalid choice: {choice}"}
    store = _store(store)
    st = current()
    sid = session_id or (st.get("id") if st else None)
    if sid is None:
        return {"ok": False, "error": "no meeting session"}
    if st is None or st.get("id") != int(sid):
        try:
            st = store.get_meeting_session(int(sid)) or st
        except Exception:
            pass
    now = time.time()
    if choice == CONSENT_SKIP:
        row = _patch_row(store, int(sid), consent=CONSENT_SKIP,
                         status=STATUS_DECLINED, ended_at=now)
        return {"ok": True, "consent": CONSENT_SKIP, "status": STATUS_DECLINED,
                "session": row}

    from app.services import meeting_mode as mm
    mm.enter(
        until=float(st["t_end"]) if st and st.get("t_end") else None,
        title=(st or {}).get("title") or "",
        calendar_event_id=(st or {}).get("calendar_event_id"),
        source="meeting_session",
        retention_hint=choice,
    )
    row = _patch_row(store, int(sid), consent=choice, status=STATUS_ACTIVE,
                     entered_at=now)
    try:
        mm.set_session_retention(
            choice, calendar_event_id=row.get("calendar_event_id"),
            store=store, apply=False)
    except Exception:
        pass
    try:
        from app.services import meeting_capture as _mc
        _mc.sync()
    except Exception:
        pass
    return {"ok": True, "consent": choice, "status": STATUS_ACTIVE, "session": row}


def end(*, reason: str = "manual", store: Store | None = None) -> dict[str, Any]:
    global _runtime
    store = _store(store)
    with _lock:
        st = dict(_runtime) if _runtime else None
        _runtime = None
    if not st:
        return {"ok": True, "active": False, "reason": reason}
    sid = st.get("id")
    now = time.time()
    if sid is not None:
        try:
            store.update_meeting_session(
                int(sid), status=STATUS_ENDED, ended_at=now)
        except Exception:
            pass
    # Pilot ledger (WS-A): a meeting counts when it *ends*, not when it is
    # offered — an offer the user skipped never entered_at, so it is not a
    # captured meeting. Duration only; the title and attendees stay out.
    from app.services.usage_ledger import usage
    entered = st.get("entered_at")
    if entered:
        usage.bump("meetings_captured")
        usage.bump("meeting_minutes", int(max(0.0, now - float(entered)) // 60))
    try:
        from app.services import meeting_mode as mm
        if st.get("consent") == CONSENT_TRANSCRIPT:
            try:
                mm.strip_session_audio(
                    store, calendar_event_id=st.get("calendar_event_id"),
                    t0=st.get("entered_at") or st.get("t_start"),
                    t1=now)
            except Exception:
                pass
        mm.exit_mode(reason=reason)
    except Exception:
        pass
    try:
        from app.services import meeting_capture as _mc
        _mc.sync()
    except Exception:
        pass
    return {"ok": True, "active": False, "reason": reason, "ended": st}


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------
def _overlapping_events(store: Store, now: float, *,
                        lookback_s: float = 12 * 3600) -> list[dict]:
    lead = _lead_s()
    try:
        events = store.list_calendar_events(
            start_min=now - lookback_s, start_max=now + lead, limit=80)
    except Exception:
        return []
    out = []
    for ev in events:
        if ev.get("all_day"):
            continue
        start = float(ev.get("start") or 0)
        end = float(ev.get("end") or start)
        if end < now:
            continue
        if start - now > lead:
            continue
        out.append(ev)
    return out


def _calendar_candidate(
    store: Store, now: float, window_title: str | None = None,
) -> dict | None:
    events = _overlapping_events(store, now)
    if not events:
        return None
    if window_title:
        matched = calendar_event_matching_window(events, window_title)
        if matched is not None:
            return matched
    return events[0]


def _calendar_by_title(store: Store, now: float, window_title: str) -> dict | None:
    """Same-named event today — catches timezone skew / late join."""
    events = _overlapping_events(store, now, lookback_s=16 * 3600)
    if events:
        matched = calendar_event_matching_window(events, window_title)
        if matched is not None:
            return matched
    try:
        today = store.list_calendar_events(
            start_min=now - 16 * 3600, start_max=now + 16 * 3600, limit=80)
    except Exception:
        return None
    live = []
    for ev in today:
        if ev.get("all_day"):
            continue
        start = float(ev.get("start") or 0)
        end = float(ev.get("end") or start)
        if end < now - 300:
            continue
        if start - now > 2 * 3600:
            continue
        live.append(ev)
    return calendar_event_matching_window(live, window_title)


def _already_claimed(store: Store, calendar_event_id: str) -> bool:
    try:
        rows = store.list_meeting_sessions(
            calendar_event_id=calendar_event_id, limit=5)
    except Exception:
        return False
    return any(r.get("status") in (STATUS_OFFERED, STATUS_ACTIVE,
                                   STATUS_DECLINED, STATUS_ENDED)
               for r in rows)


def consider(
    store: Store | None = None, *,
    now: float | None = None,
    window_title: str | None = None,
    propose: Callable[[dict], bool] | None = None,
) -> dict[str, Any]:
    """Offer or auto-enter a MeetingSession for a starting calendar event.

    Window-title conferencing is a fallback when no calendar row claims the
    window. Recurring series reuse the last Skip/Transcript/Receipts choice.
    """
    if not enabled():
        return {"ok": False, "skipped": "disabled"}
    store = _store(store)
    now = float(now if now is not None else time.time())
    st = current()
    if st and st.get("status") in (STATUS_OFFERED, STATUS_ACTIVE):
        return {"ok": True, "skipped": "already_active", "id": st.get("id")}

    title = window_title if window_title is not None else _foreground_title()
    ev = _calendar_candidate(store, now, window_title=title)
    if ev is None and title and provider_from_window(title):
        ev = _calendar_by_title(store, now, title)
    if ev is not None:
        eid = ev.get("id") or ""
        if eid and _already_claimed(store, eid):
            return _announce({"ok": True, "skipped": "already_claimed",
                              "calendar_event_id": eid},
                             window_title=title, now=now)
        join = (ev.get("join_url") or "")
        provider = (ev.get("provider") or "unknown")
        if not join:
            join, provider = extract_conference_link(
                ev.get("location") or "", ev.get("description") or "",
                ev.get("url") or "")
            if provider == "unknown":
                provider = "unknown"
        rec = recurrence_uid_of(eid)
        standing = last_consent_for_series(store, rec)
        sess = spawn(
            store,
            calendar_event_id=eid,
            title=(ev.get("title") or "Meeting").strip(),
            attendees=list(ev.get("attendees") or []),
            organizer=ev.get("organizer"),
            join_url=join,
            provider=provider,
            source=SOURCE_CALENDAR,
            t_start=float(ev.get("start") or now),
            t_end=float(ev.get("end") or now + 3600),
            consent=standing or CONSENT_PENDING,
            status=STATUS_OFFERED,
        )
        if standing in RECORD_CONSENT:
            return _announce(
                {"ok": True, "auto": True, **decide(standing, store=store,
                                                    session_id=sess["id"])},
                window_title=title, now=now)
        if standing == CONSENT_SKIP:
            return _announce(
                {"ok": True, "auto": True, **decide(CONSENT_SKIP, store=store,
                                                    session_id=sess["id"])},
                window_title=title, now=now)
        shown = _offer(sess, propose=propose)
        return _announce(
            {"ok": True, "offered": True, "shown": shown,
             "calendar_event_id": eid, "title": sess["title"],
             "id": sess["id"], "source": SOURCE_CALENDAR},
            window_title=title, now=now)

    provider = provider_from_window(title)
    if not provider:
        return _announce({"ok": True, "skipped": "no_event"},
                         window_title=title, now=now)
    last = float(_window_offered.get(provider) or 0)
    if last and (now - last) < WINDOW_COOLDOWN_S:
        return {"ok": True, "skipped": "window_cooldown"}
    _window_offered[provider] = now
    nice = _strip_browser_suffix(title)[:200] or f"{provider} call"
    sess = spawn(
        store,
        calendar_event_id=None,
        title=nice,
        attendees=[],
        organizer=None,
        join_url="",
        provider=provider,
        source=SOURCE_WINDOW,
        t_start=now,
        t_end=now + 3600,
        consent=CONSENT_PENDING,
        status=STATUS_OFFERED,
    )
    shown = _offer(sess, propose=propose)
    return _announce(
        {"ok": True, "offered": True, "shown": shown, "source": SOURCE_WINDOW,
         "title": sess["title"], "id": sess["id"]},
        window_title=title, now=now)


def _announce(out: dict[str, Any], *, window_title: str = "",
              now: float | None = None) -> dict[str, Any]:
    """Terminal-visible consider() outcome so a live miss is diagnosable."""
    now = float(now if now is not None else time.time())
    if out.get("offered"):
        print(
            f"[meeting_session] offered {out.get('title')!r} "
            f"via {out.get('source') or 'calendar'} id={out.get('id')}")
        return out
    if out.get("auto"):
        print(
            f"[meeting_session] auto {out.get('consent')} "
            f"{out.get('title') or out.get('id')!r}")
        return out
    reason = out.get("skipped")
    if reason in ("no_event", "already_claimed"):
        label = (window_title or out.get("calendar_event_id") or "")[:80]
        key = f"{reason}:{_norm_title(str(label))}"
        last = float(_skip_logged.get(key) or 0)
        if not last or (now - last) >= 300:
            _skip_logged[key] = now
            extra = f" title={label!r}" if label else ""
            print(f"[meeting_session] skipped {reason}{extra}")
    return out


def _offer(sess: dict[str, Any], *,
           propose: Callable[[dict], bool] | None = None) -> bool:
    payload = {
        "title": sess.get("title") or "Meeting",
        "calendar_event_id": sess.get("calendar_event_id"),
        "meeting_session_id": sess.get("id"),
        "start": sess.get("t_start"),
        "end": sess.get("t_end"),
        "provider": sess.get("provider"),
        "attendees": list(sess.get("attendees") or []),
        "default_retention": CONSENT_TRANSCRIPT,
    }
    if propose is not None:
        try:
            return bool(propose(payload))
        except Exception as exc:
            print(f"[meeting_session] propose skipped ({exc}).")
            return False
    try:
        from app.services.agent_bridge import worker
        return bool(worker.propose_meeting_record(payload))
    except Exception as exc:
        print(f"[meeting_session] propose skipped ({exc}).")
        return False


def parse_choice(text: str, *, default: str = CONSENT_TRANSCRIPT) -> str | None:
    """Map a chat reply onto a consent choice. None = not a consent reply."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in ("skip", "no", "don't", "dont", "not now", "nope"):
        return CONSENT_SKIP
    if "skip" in t or "don't record" in t or "do not record" in t:
        return CONSENT_SKIP
    if t in ("yes", "record", "ok", "okay"):
        return default
    if "receipt" in t or ("audio" in t and "transcript" in t) or t in (
            "keep_receipts", "audio"):
        return CONSENT_RECEIPTS
    if "transcript" in t or t == CONSENT_TRANSCRIPT:
        return CONSENT_TRANSCRIPT
    return None


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------
def attach() -> None:
    """Background consider() so calendar start is not boot-only."""
    global _thread
    if not enabled():
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_poll_loop, name="meeting-session",
                               daemon=True)
    _thread.start()
    print("[meeting_session] polling for calendar / conferencing windows.")


def stop() -> None:
    _stop.set()


def _poll_loop() -> None:
    while not _stop.wait(max(5.0, POLL_S)):
        try:
            consider()
        except Exception as exc:
            print(f"[meeting_session] consider skipped ({exc}).")
        try:
            st = current()
            if st and st.get("status") == STATUS_ACTIVE:
                until = st.get("t_end")
                if until is not None and time.time() > float(until):
                    end(reason="expired")
        except Exception:
            pass
