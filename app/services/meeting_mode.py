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
                calendar_event_id: str | None = None) -> str | None:
    if session_id is not None:
        return f"session:{int(session_id)}"
    if calendar_event_id:
        return f"cal:{calendar_event_id}"
    return None


def retention_for(
    session_id: int | None = None,
    calendar_event_id: str | None = None,
) -> dict[str, Any]:
    """Resolved retention record for a session / calendar event."""
    prefs = load_prefs()
    key = session_key(session_id, calendar_event_id)
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
    return {"ok": True, "active": False, "reason": reason, "ended": meta}


def consider_offer(
    store: Store | None = None, *, now: float | None = None,
) -> dict[str, Any]:
    """Suggest meeting mode when a calendar event is starting / in progress."""
    if not enabled():
        return {"ok": False, "skipped": "disabled"}
    store = store or get_store()
    now = float(now if now is not None else time.time())
    with _lock:
        if _runtime["active"]:
            return {"ok": True, "skipped": "already_active"}
    prefs = load_prefs()
    offered = prefs.get("offered") or {}
    declined = prefs.get("declined") or {}

    # Prefer structured calendar_events index.
    try:
        events = store.list_calendar_events(
            start_min=now - 30 * 60, start_max=now + OFFER_LEAD_S, limit=40)
    except Exception:
        events = []
    candidate = None
    for ev in events:
        eid = ev.get("id")
        if not eid:
            continue
        start = float(ev.get("start") or 0)
        end = float(ev.get("end") or start)
        if end < now:
            continue
        if start - now > OFFER_LEAD_S:
            continue
        # Cooldown / prior decline.
        last_off = float(offered.get(eid) or 0)
        if last_off and (now - last_off) < OFFER_COOLDOWN_S:
            continue
        last_dec = float(declined.get(eid) or 0)
        if last_dec and (now - last_dec) < OFFER_COOLDOWN_S:
            continue
        candidate = ev
        break

    if candidate is None:
        return {"ok": True, "skipped": "no_event"}

    title = (candidate.get("title") or "Meeting").strip()
    try:
        from app.services.agent_bridge import worker
        shown = worker.propose_meeting_mode({
            "title": title,
            "calendar_event_id": candidate.get("id"),
            "start": candidate.get("start"),
            "end": candidate.get("end"),
            "default_retention": default_retention(),
        })
        offered[str(candidate["id"])] = now
        save_prefs({"offered": offered, "declined": declined,
                    "sessions": prefs.get("sessions") or {},
                    "default_retention": prefs.get("default_retention")})
        return {"ok": True, "offered": True, "shown": shown,
                "calendar_event_id": candidate.get("id"), "title": title}
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
    store: Store | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Record + optionally apply retention for one session."""
    if choice not in VALID_RETENTION:
        return {"ok": False, "error": f"invalid retention: {choice}"}
    key = session_key(session_id, calendar_event_id)
    if not key:
        return {"ok": False, "error": "session_id or calendar_event_id required"}
    prefs = load_prefs()
    sessions = dict(prefs.get("sessions") or {})
    row = {
        "retention": choice,
        "applied_at": time.time(),
        "stripped": False,
        "source": "user",
        "session_id": session_id,
        "calendar_event_id": calendar_event_id,
    }
    strip_result = None
    if apply and choice == RETENTION_TRANSCRIPT:
        store = store or get_store()
        strip_result = strip_session_audio(
            store, session_id=session_id, calendar_event_id=calendar_event_id)
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
    """On settle/enhance: apply default retention once if unset."""
    sid = sess.get("id")
    cal = sess.get("calendar_event_id")
    key = session_key(sid, cal)
    if not key:
        return None
    prefs = load_prefs()
    existing = (prefs.get("sessions") or {}).get(key)
    if existing and not force:
        return None
    choice = prefs.get("default_retention") or RETENTION_TRANSCRIPT
    return set_session_retention(
        choice, session_id=sid, calendar_event_id=cal,
        store=store, apply=True,
    )


def strip_session_audio(
    store: Store, *,
    session_id: int | None = None,
    calendar_event_id: str | None = None,
    t0: float | None = None,
    t1: float | None = None,
) -> dict[str, Any]:
    """Delete WAVs for a session window; keep transcript + open ledger.

    Clears ``audio_path`` / enhanced paths on events. Marks citing facts
    ``state='evidence_removed'`` so vector_gc can drop embeddings — does **not**
    cancel open commitments/tasks (note + ledger stay functional; playback gone).
    """
    event_ids: list[int] = []
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
) -> dict[str, Any]:
    """Fields stamped onto hydrated meeting notes (P5 accept criteria)."""
    ret = retention_for(session_id, calendar_event_id)
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
