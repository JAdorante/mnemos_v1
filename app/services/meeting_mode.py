"""Meeting mode — consent posture, capture aggressiveness, retention (P5).

Granola's trust posture as a *mode*, not the architecture:
  * enter → hotter capture/extract for a calendar window + capturing chip
  * exit / settle → per-session retention: transcript_only (strip WAVs) or
    keep_receipts (audio stays for playback)
  * default retention is a user pref; consent + choice surface on the note

Durable prefs live in ``data/meeting_prefs.json`` (survives session rebuild).
Runtime aggressiveness is hot-patched like ``capture_consent``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from app.storage import Store, get_store

RETENTION_TRANSCRIPT = "transcript_only"
RETENTION_RECEIPTS = "keep_receipts"
VALID_RETENTION = frozenset({RETENTION_TRANSCRIPT, RETENTION_RECEIPTS})

# Offer when a calendar event starts within this many seconds (or already in).
OFFER_LEAD_S = float(os.environ.get("QUILL_MEETING_MODE_LEAD_S", "120"))
OFFER_COOLDOWN_S = float(os.environ.get("QUILL_MEETING_MODE_COOLDOWN_S", "3600"))

# Aggressiveness deltas while mode is on (restored on exit).
_FACT_MIN_CONF_MEETING = float(os.environ.get("QUILL_MEETING_FACT_MIN_CONF", "0.25"))
_VAD_MEETING = float(os.environ.get("QUILL_MEETING_VAD_THRESHOLD", "0.35"))

_lock = threading.RLock()
_runtime: dict[str, Any] = {
    "active": False,
    "entered_at": None,
    "until": None,
    "title": "",
    "calendar_event_id": None,
    "session_id": None,
    "source": None,  # offer | manual | auto
    "snapshot": None,  # settings to restore
}


def _prefs_path() -> Path:
    from app.config import settings
    return Path(settings.storage.data_dir) / "meeting_prefs.json"


def _blank_prefs() -> dict[str, Any]:
    return {
        "default_retention": RETENTION_TRANSCRIPT,
        "sessions": {},          # key -> {retention, applied_at, stripped, ...}
        "offered": {},           # calendar_event_id -> ts
        "declined": {},          # calendar_event_id -> ts
    }


def load_prefs(*, force: bool = False) -> dict[str, Any]:
    out = _blank_prefs()
    try:
        p = _prefs_path()
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                dr = raw.get("default_retention") or RETENTION_TRANSCRIPT
                out["default_retention"] = (
                    dr if dr in VALID_RETENTION else RETENTION_TRANSCRIPT)
                for key in ("sessions", "offered", "declined"):
                    if isinstance(raw.get(key), dict):
                        out[key] = dict(raw[key])
    except Exception as exc:
        print(f"[meeting_mode] prefs load skipped ({exc}).")
    return out


def save_prefs(prefs: dict[str, Any]) -> dict[str, Any]:
    cur = load_prefs(force=True)
    if "default_retention" in prefs:
        dr = prefs["default_retention"]
        cur["default_retention"] = (
            dr if dr in VALID_RETENTION else cur["default_retention"])
    for key in ("sessions", "offered", "declined"):
        if key in prefs and isinstance(prefs[key], dict):
            cur[key] = dict(prefs[key])
    try:
        from app.atomic_json import write_json
        write_json(_prefs_path(), cur, sort_keys=True)
    except Exception as exc:
        print(f"[meeting_mode] prefs save failed ({exc}).")
    return cur


def default_retention() -> str:
    return load_prefs().get("default_retention") or RETENTION_TRANSCRIPT


def set_default_retention(choice: str) -> dict[str, Any]:
    if choice not in VALID_RETENTION:
        raise ValueError(f"invalid retention: {choice}")
    return save_prefs({"default_retention": choice})


def session_key(session_id: int | None = None,
                calendar_event_id: str | None = None,
                meeting_session_id: int | None = None) -> str | None:
    """Three id spaces, one prefs map. A MeetingSession (the consent owner)
    keys as `meeting:<id>`; a derived speech session — rebuilt wholesale, so
    its ids churn — as `session:<id>`; a calendar row as `cal:<id>`."""
    if meeting_session_id is not None:
        return f"meeting:{int(meeting_session_id)}"
    if session_id is not None:
        return f"session:{int(session_id)}"
    if calendar_event_id:
        return f"cal:{calendar_event_id}"
    return None


def _consent_of(store: Store | None, meeting_session_id: int | None) -> str | None:
    """The retention a MeetingSession row states, or None."""
    if meeting_session_id is None:
        return None
    try:
        row = (store or get_store()).get_meeting_session(int(meeting_session_id))
    except Exception:
        return None
    c = (row or {}).get("consent")
    return c if c in VALID_RETENTION else None


def retention_for(
    session_id: int | None = None,
    calendar_event_id: str | None = None,
    meeting_session_id: int | None = None,
    store: Store | None = None,
) -> dict[str, Any]:
    """Resolved retention record for a session / calendar event.

    A MeetingSession's own consent is the source of truth when one is named;
    the prefs file is where derived-session and calendar choices live.
    """
    prefs = load_prefs()
    stated = _consent_of(store, meeting_session_id)
    key = session_key(session_id, calendar_event_id, meeting_session_id)
    if stated:
        row = (prefs.get("sessions") or {}).get(key or "") or {}
        return {
            "retention": stated,
            "applied_at": row.get("applied_at"),
            "stripped": bool(row.get("stripped")),
            "source": "meeting_session",
            "key": key,
            "is_default": False,
        }
    row = (prefs.get("sessions") or {}).get(key or "") if key else None
    if isinstance(row, dict) and row.get("retention") in VALID_RETENTION:
        return {
            "retention": row["retention"],
            "applied_at": row.get("applied_at"),
            "stripped": bool(row.get("stripped")),
            "source": row.get("source") or "session",
            "key": key,
            "is_default": False,
        }
    return {
        "retention": prefs.get("default_retention") or RETENTION_TRANSCRIPT,
        "applied_at": None,
        "stripped": False,
        "source": "default",
        "key": key,
        "is_default": True,
    }


def enabled() -> bool:
    return os.getenv("QUILL_MEETING_MODE", "1") not in ("0", "false", "False")


def status() -> dict[str, Any]:
    with _lock:
        st = dict(_runtime)
    # Auto-expire wall-clock window.
    until = st.get("until")
    if st.get("active") and until is not None and time.time() > float(until):
        exit_mode(reason="expired")
        with _lock:
            st = dict(_runtime)
    prefs = load_prefs()
    return {
        "enabled": enabled(),
        "active": bool(st.get("active")),
        "entered_at": st.get("entered_at"),
        "until": st.get("until"),
        "title": st.get("title") or "",
        "calendar_event_id": st.get("calendar_event_id"),
        "session_id": st.get("session_id"),
        "source": st.get("source"),
        "default_retention": prefs.get("default_retention"),
        "capturing": bool(st.get("active")),
    }


def _snapshot_settings() -> dict[str, Any]:
    from app.config import settings
    return {
        "save_audio": bool(settings.storage.save_audio),
        "fact_min_conf": float(settings.facts.min_conf),
        "skip_bad": bool(settings.audio_quality.skip_bad),
        "vad_threshold": float(settings.audio.vad_threshold),
    }


def _apply_aggressiveness(*, want_receipts: bool) -> dict[str, Any]:
    """Hot-patch capture/extract knobs for the meeting window."""
    from app.config import settings
    snap = _snapshot_settings()
    try:
        # Receipts need WAVs; transcript-only default still captures hotter text
        # but does not force disk WAVs unless the user chose keep_receipts.
        if want_receipts:
            object.__setattr__(settings.storage, "save_audio", True)
            os.environ["QUILL_SAVE_AUDIO"] = "1"
        object.__setattr__(settings.facts, "min_conf", _FACT_MIN_CONF_MEETING)
        os.environ["QUILL_FACT_MIN_CONF"] = str(_FACT_MIN_CONF_MEETING)
        object.__setattr__(settings.audio_quality, "skip_bad", False)
        os.environ["QUILL_AQ_SKIP_BAD"] = "0"
        object.__setattr__(settings.audio, "vad_threshold", _VAD_MEETING)
        os.environ["QUILL_VAD_THRESHOLD"] = str(_VAD_MEETING)
    except Exception as exc:
        print(f"[meeting_mode] aggressiveness patch skipped ({exc}).")
    return snap


def _restore_aggressiveness(snap: dict[str, Any] | None) -> None:
    if not snap:
        return
    from app.config import settings
    try:
        object.__setattr__(settings.storage, "save_audio", bool(snap["save_audio"]))
        os.environ["QUILL_SAVE_AUDIO"] = "1" if snap["save_audio"] else "0"
        object.__setattr__(settings.facts, "min_conf", float(snap["fact_min_conf"]))
        os.environ["QUILL_FACT_MIN_CONF"] = str(snap["fact_min_conf"])
        object.__setattr__(settings.audio_quality, "skip_bad", bool(snap["skip_bad"]))
        os.environ["QUILL_AQ_SKIP_BAD"] = "1" if snap["skip_bad"] else "0"
        object.__setattr__(settings.audio, "vad_threshold", float(snap["vad_threshold"]))
        os.environ["QUILL_VAD_THRESHOLD"] = str(snap["vad_threshold"])
        # Re-apply durable consent so we don't leave save_audio above consent.
        try:
            from app.services import capture_consent
            capture_consent.apply_saved_to_runtime()
        except Exception:
            pass
    except Exception as exc:
        print(f"[meeting_mode] restore skipped ({exc}).")


def enter(
    *,
    until: float | None = None,
    title: str = "",
    calendar_event_id: str | None = None,
    session_id: int | None = None,
    source: str = "manual",
    retention_hint: str | None = None,
) -> dict[str, Any]:
    """Enter meeting mode for a window. Idempotent if already active."""
    if not enabled():
        return {"ok": False, "error": "meeting mode disabled"}
    want = retention_hint or default_retention()
    want_receipts = want == RETENTION_RECEIPTS
    with _lock:
        if _runtime["active"]:
            # Extend / refresh metadata.
            if until is not None:
                _runtime["until"] = float(until)
            if title:
                _runtime["title"] = title
            if calendar_event_id:
                _runtime["calendar_event_id"] = calendar_event_id
            if session_id is not None:
                _runtime["session_id"] = int(session_id)
            return {"ok": True, "already": True, **status()}
        snap = _apply_aggressiveness(want_receipts=want_receipts)
        _runtime.update({
            "active": True,
            "entered_at": time.time(),
            "until": float(until) if until is not None else None,
            "title": (title or "").strip(),
            "calendar_event_id": calendar_event_id,
            "session_id": int(session_id) if session_id is not None else None,
            "source": source,
            "snapshot": snap,
        })
    return {"ok": True, **status()}


def exit_mode(*, reason: str = "manual") -> dict[str, Any]:
    with _lock:
        if not _runtime["active"]:
            return {"ok": True, "active": False, "reason": reason}
        snap = _runtime.get("snapshot")
        meta = {
            "title": _runtime.get("title"),
            "calendar_event_id": _runtime.get("calendar_event_id"),
            "session_id": _runtime.get("session_id"),
            "entered_at": _runtime.get("entered_at"),
        }
        _runtime.update({
            "active": False, "entered_at": None, "until": None,
            "title": "", "calendar_event_id": None, "session_id": None,
            "source": None, "snapshot": None,
        })
    _restore_aggressiveness(snap if isinstance(snap, dict) else None)
    # Notices and peer offers held back during the meeting surface now.
    for mod, fn in (("salience", "flush_deferred"),
                    ("peer_channel", "flush_deferred_null_offers")):
        try:
            import importlib
            getattr(importlib.import_module(f"app.services.{mod}"), fn)()
        except Exception as exc:
            print(f"[meeting_mode] {mod}.{fn} skipped ({exc}).")
    return {"ok": True, "active": False, "reason": reason, "ended": meta}


def consider_offer(
    store: Store | None = None, *, now: float | None = None,
) -> dict[str, Any]:
    """Delegate to MeetingSession (calendar-first spawn + 3-way consent)."""
    try:
        from app.services import meeting_session as _ms
        return _ms.consider(store, now=now)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def accept_offer(pend: dict) -> dict[str, Any]:
    end = pend.get("end")
    return enter(
        until=float(end) if end else None,
        title=pend.get("title") or "",
        calendar_event_id=pend.get("calendar_event_id"),
        source="offer",
    )


def decline_offer(pend: dict) -> None:
    eid = pend.get("calendar_event_id")
    if not eid:
        return
    prefs = load_prefs()
    declined = dict(prefs.get("declined") or {})
    declined[str(eid)] = time.time()
    save_prefs({
        "declined": declined,
        "offered": prefs.get("offered") or {},
        "sessions": prefs.get("sessions") or {},
        "default_retention": prefs.get("default_retention"),
    })


def set_session_retention(
    choice: str, *,
    session_id: int | None = None,
    calendar_event_id: str | None = None,
    meeting_session_id: int | None = None,
    store: Store | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Record + optionally apply retention for one session.

    Named a MeetingSession, the choice is also written to its row, so a later
    settle pass reads the same answer the user gave — the prefs file alone
    was where a keep-receipts choice went missing.
    """
    if choice not in VALID_RETENTION:
        return {"ok": False, "error": f"invalid retention: {choice}"}
    key = session_key(session_id, calendar_event_id, meeting_session_id)
    if not key:
        return {"ok": False,
                "error": "meeting_session_id, session_id or calendar_event_id required"}
    if meeting_session_id is not None:
        try:
            (store or get_store()).update_meeting_session(
                int(meeting_session_id), consent=choice)
        except Exception as exc:
            print(f"[meeting_mode] consent write skipped ({exc}).")
    prefs = load_prefs()
    sessions = dict(prefs.get("sessions") or {})
    row = {
        "retention": choice,
        "applied_at": time.time(),
        "stripped": False,
        "source": "user",
        "session_id": session_id,
        "calendar_event_id": calendar_event_id,
        "meeting_session_id": meeting_session_id,
    }
    strip_result = None
    if apply and choice == RETENTION_TRANSCRIPT:
        store = store or get_store()
        strip_result = strip_session_audio(
            store, session_id=session_id, calendar_event_id=calendar_event_id,
            meeting_session_id=meeting_session_id)
        row["stripped"] = bool(strip_result.get("ok"))
        row["strip"] = {
            "n_files": strip_result.get("n_files"),
            "n_events": strip_result.get("n_events"),
            "n_facts": strip_result.get("n_facts"),
        }
    sessions[key] = row
    # Also mirror under the other key when both known.
    if session_id is not None and calendar_event_id:
        sessions[session_key(session_id, None)] = row
        sessions[session_key(None, calendar_event_id)] = row
    save_prefs({
        "sessions": sessions,
        "offered": prefs.get("offered") or {},
        "declined": prefs.get("declined") or {},
        "default_retention": prefs.get("default_retention"),
    })
    return {"ok": True, "key": key, "retention": choice,
            "strip": strip_result}


def apply_default_for_session(
    store: Store, sess: dict, *, force: bool = False,
) -> dict[str, Any] | None:
    """On settle/enhance: apply retention to a session's events, once.

    Per EVENT, from the stamp. An event a recording MeetingSession stamped
    follows that session's stated consent; only unstamped events follow the
    user's default. A speech session is a wall-clock rollup that can span
    several meetings and the silence between them, so a session-wide default
    was deleting receipts the user had asked to keep — measured on the first
    pilot night: a keep-receipts meeting lost all nine of its clips to the
    transcript-only default six minutes after it ended.
    """
    msid = sess.get("meeting_session_id")
    sid = sess.get("id")
    cal = sess.get("calendar_event_id")
    key = session_key(sid, cal, msid)
    if not key:
        return None
    prefs = load_prefs()
    existing = (prefs.get("sessions") or {}).get(key)
    if existing and not force:
        return None
    default = prefs.get("default_retention") or RETENTION_TRANSCRIPT
    event_ids = [int(x) for x in (sess.get("event_ids") or []) if x is not None]
    if msid is not None and not event_ids:
        try:
            event_ids = store.events_for_meeting_session(int(msid))
        except Exception:
            event_ids = []
    stamps = {}
    try:
        stamps = store.event_meeting_sessions(event_ids)
    except Exception:
        stamps = {}
    groups: dict[int | None, list[int]] = {}
    for eid in event_ids:
        groups.setdefault(stamps.get(eid), []).append(eid)
    by_meeting: dict[str, str] = {}
    to_strip: list[int] = []
    kept = 0
    for gid, ids in groups.items():
        if gid is None:
            choice = default
            src = "default"
        else:
            choice = _consent_of(store, gid) or default
            src = "meeting_session" if _consent_of(store, gid) else "default"
            by_meeting[str(gid)] = choice
        if choice == RETENTION_TRANSCRIPT:
            to_strip.extend(ids)
        else:
            kept += len(ids)
    strip_result = None
    if to_strip:
        strip_result = store.strip_event_audio(to_strip)
    sessions = dict(prefs.get("sessions") or {})
    sessions[key] = {
        "retention": (_consent_of(store, msid) if msid is not None else None) or default,
        "applied_at": time.time(),
        "stripped": bool(to_strip),
        "source": "settle",
        "session_id": sid,
        "calendar_event_id": cal,
        "meeting_session_id": msid,
        "by_meeting_session": by_meeting,
        "strip": ({"n_files": strip_result.get("n_files"),
                   "n_events": strip_result.get("n_events"),
                   "n_facts": strip_result.get("n_facts")}
                  if strip_result else None),
        "kept_events": kept,
    }
    save_prefs({
        "sessions": sessions,
        "offered": prefs.get("offered") or {},
        "declined": prefs.get("declined") or {},
        "default_retention": prefs.get("default_retention"),
    })
    return {"ok": True, "key": key, "retention": sessions[key]["retention"],
            "by_meeting_session": by_meeting, "stripped_events": len(to_strip),
            "kept_events": kept, "strip": strip_result}


def _honor_stamps(store: Store, event_ids: list[int]) -> list[int]:
    """Drop events a keep-receipts MeetingSession stamped. A window or a
    derived-session strip must never take a stated consent down with it."""
    if not event_ids:
        return []
    try:
        stamps = store.event_meeting_sessions(event_ids)
    except Exception:
        return list(event_ids)
    keep_ids = {gid for gid in set(stamps.values()) if gid is not None
                and _consent_of(store, gid) == RETENTION_RECEIPTS}
    if not keep_ids:
        return list(event_ids)
    return [e for e in event_ids if stamps.get(e) not in keep_ids]


def strip_session_audio(
    store: Store, *,
    session_id: int | None = None,
    calendar_event_id: str | None = None,
    meeting_session_id: int | None = None,
    t0: float | None = None,
    t1: float | None = None,
) -> dict[str, Any]:
    """Delete WAVs for a session; keep transcript + open ledger.

    Named a MeetingSession, strips exactly the events it stamped. Otherwise
    the derived session's events, else a wall-clock window — and in every
    branch an event a keep-receipts meeting stamped is left alone.

    Clears ``audio_path`` / enhanced paths on events. Marks citing facts
    ``state='evidence_removed'`` so vector_gc can drop embeddings — does **not**
    cancel open commitments/tasks (note + ledger stay functional; playback gone).
    """
    event_ids: list[int] = []
    if meeting_session_id is not None:
        try:
            event_ids = store.events_for_meeting_session(int(meeting_session_id))
        except Exception:
            event_ids = []
        if not event_ids:
            return {"ok": True, "n_files": 0, "n_events": 0, "n_facts": 0,
                    "skipped": "no_events"}
        return store.strip_event_audio(event_ids)
    if session_id is not None:
        try:
            for s in store.recent_sessions(limit=80):
                if s.get("id") == int(session_id):
                    event_ids = [int(x) for x in (s.get("event_ids") or [])]
                    t0 = float(s.get("start") or t0 or 0)
                    t1 = float(s.get("end") or t1 or 0)
                    break
        except Exception:
            pass
    if not event_ids and t0 is not None and t1 is not None:
        try:
            rows = store.events_in_window(float(t0), float(t1), limit=2000)
            event_ids = [int(r["id"]) for r in rows if r.get("id") is not None]
        except Exception:
            event_ids = []
    if not event_ids and calendar_event_id:
        try:
            for s in store.recent_sessions(limit=80):
                if s.get("calendar_event_id") == calendar_event_id:
                    event_ids = [int(x) for x in (s.get("event_ids") or [])]
                    break
        except Exception:
            pass
    event_ids = _honor_stamps(store, event_ids)
    if not event_ids:
        return {"ok": True, "n_files": 0, "n_events": 0, "n_facts": 0,
                "skipped": "no_events"}

    return store.strip_event_audio(event_ids)


def consent_summary() -> dict[str, Any]:
    """Compact consent snapshot for the meeting note mast."""
    try:
        from app.services import capture_consent
        st = capture_consent.status()
        src = st.get("sources") or {}
        on = [k for k, v in src.items() if v]
        return {
            "consented": bool(st.get("consented")),
            "sources_on": on,
            "save_audio": bool(src.get("save_audio")),
        }
    except Exception:
        return {"consented": False, "sources_on": [], "save_audio": False}


def note_privacy_block(
    *,
    session_id: int | None = None,
    calendar_event_id: str | None = None,
    meeting_session_id: int | None = None,
    store: Store | None = None,
) -> dict[str, Any]:
    """Fields stamped onto hydrated meeting notes (P5 accept criteria)."""
    ret = retention_for(session_id, calendar_event_id,
                        meeting_session_id=meeting_session_id, store=store)
    mode = status()
    return {
        "consent": consent_summary(),
        "retention": ret,
        "meeting_mode": {
            "active": mode.get("active"),
            "title": mode.get("title"),
        },
        "tradeoff": (
            "transcript-only = Granola-parity and socially safest; "
            "keep receipts = playback and dispute-proof memory."
        ),
    }
