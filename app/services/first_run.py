"""First-run / tester onboarding posture (Workstream 1).

Meeting-first inverts day one: calendar + one brief before always-on capture.
Ambient mic/webcam/desktop stay off until an explicit opt-in card (wizard or
the post-brief unlock nudge). Nothing here authorizes actions.

State lives in ``data/first_run.json`` under QUILL_DATA_DIR.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.RLock()
_cached: dict[str, Any] | None = None

VALID_MODES = ("meeting", "ambient", "full")
AMBIENT_SOURCES = ("mic", "webcam", "desktop")


def _path() -> Path:
    data = os.environ.get("QUILL_DATA_DIR", "data")
    return Path(data) / "first_run.json"


def _blank() -> dict[str, Any]:
    return {
        "meeting_listen_consent": False,
        "ambient": {s: False for s in AMBIENT_SOURCES},
        "unlock_shown": False,
        "briefs_completed": 0,
        "pending_first_win": None,  # {session_id, href, at, has_facts}
        "wizard_step": None,
        "updated_at": None,
    }


def load(*, force: bool = False) -> dict[str, Any]:
    global _cached
    with _lock:
        if _cached is not None and not force:
            return dict(_cached)
        out = _blank()
        try:
            p = _path()
            if p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    out["meeting_listen_consent"] = bool(
                        raw.get("meeting_listen_consent"))
                    out["unlock_shown"] = bool(raw.get("unlock_shown"))
                    try:
                        out["briefs_completed"] = int(raw.get("briefs_completed") or 0)
                    except (TypeError, ValueError):
                        out["briefs_completed"] = 0
                    amb = raw.get("ambient") or {}
                    if isinstance(amb, dict):
                        for s in AMBIENT_SOURCES:
                            out["ambient"][s] = bool(amb.get(s))
                    pend = raw.get("pending_first_win")
                    out["pending_first_win"] = pend if isinstance(pend, dict) else None
                    out["wizard_step"] = raw.get("wizard_step")
                    out["updated_at"] = raw.get("updated_at")
        except Exception as exc:
            print(f"[first_run] load skipped ({exc}).")
        _cached = dict(out)
        return dict(out)


def save(patch: dict[str, Any] | None = None) -> dict[str, Any]:
    global _cached
    with _lock:
        cur = load(force=True)
        if patch:
            if "meeting_listen_consent" in patch:
                cur["meeting_listen_consent"] = bool(patch["meeting_listen_consent"])
            if "unlock_shown" in patch:
                cur["unlock_shown"] = bool(patch["unlock_shown"])
            if "briefs_completed" in patch:
                try:
                    cur["briefs_completed"] = int(patch["briefs_completed"])
                except (TypeError, ValueError):
                    pass
            if "wizard_step" in patch:
                cur["wizard_step"] = patch["wizard_step"]
            if "pending_first_win" in patch:
                pend = patch["pending_first_win"]
                cur["pending_first_win"] = pend if isinstance(pend, dict) else None
            if "ambient" in patch and isinstance(patch["ambient"], dict):
                for s in AMBIENT_SOURCES:
                    if s in patch["ambient"]:
                        cur["ambient"][s] = bool(patch["ambient"][s])
        cur["updated_at"] = time.time()
        try:
            from app.atomic_json import write_json
            write_json(_path(), cur, sort_keys=True)
        except Exception as exc:
            print(f"[first_run] save failed ({exc}).")
        _cached = dict(cur)
        return dict(cur)


def mode() -> str:
    # Default `full` preserves pre-adoption capture-consent behaviour (invariant 4).
    # Tester / installer builds pin meeting via QUILL_PROFILE=tester.
    raw = (os.environ.get("QUILL_FIRST_RUN_MODE") or "full").strip().lower()
    return raw if raw in VALID_MODES else "full"


def meeting_pad_s() -> float:
    try:
        mins = float(os.environ.get("QUILL_MEETING_PAD_MIN", "5"))
    except (TypeError, ValueError):
        mins = 5.0
    return max(0.0, mins) * 60.0


def unlock_after_briefs() -> int:
    try:
        n = int(os.environ.get("QUILL_UNLOCK_AFTER_BRIEFS", "3"))
    except (TypeError, ValueError):
        n = 3
    return max(1, n)


def is_meeting_first() -> bool:
    return mode() == "meeting"


def meeting_listen_ok() -> bool:
    """Wizard confirmed Sparrow may listen inside calendar windows."""
    return bool(load().get("meeting_listen_consent"))


def allows_continuous(source: str) -> bool:
    """Always-on capture for a source. Meeting-first requires an opt-in card."""
    src = "desktop" if source in ("screen", "clicks", "desktop") else source
    if src == "system_audio":
        src = "mic"
    if mode() == "full":
        try:
            from app.services import capture_consent
            key = "screen" if src == "desktop" else src
            if key == "mic":
                return capture_consent.allows("mic")
            if key == "webcam":
                return capture_consent.allows("webcam")
            return capture_consent.allows("screen") or capture_consent.allows("clicks")
        except Exception:
            return False
    if mode() == "ambient":
        try:
            from app.services import capture_consent
            key = "screen" if src == "desktop" else ("webcam" if src == "webcam" else "mic")
            return capture_consent.allows(key)
        except Exception:
            return False
    # meeting: continuous only after the deferred opt-in (and consent file).
    if not bool((load().get("ambient") or {}).get(src)):
        return False
    try:
        from app.services import capture_consent
        if src == "mic":
            return capture_consent.allows("mic")
        if src == "webcam":
            return capture_consent.allows("webcam")
        return capture_consent.allows("screen") or capture_consent.allows("clicks")
    except Exception:
        return False


def in_meeting_window(now: float | None = None, store=None) -> bool:
    """True inside an active/offered calendar session, plus the pad."""
    ts = time.time() if now is None else float(now)
    pad = meeting_pad_s()
    try:
        from app.services import meeting_session as ms
        st = ms.current()
        if st:
            status = st.get("status")
            if status in (ms.STATUS_ACTIVE, ms.STATUS_OFFERED):
                t0 = st.get("t_start")
                t1 = st.get("t_end")
                if t0 is None and t1 is None:
                    return status == ms.STATUS_ACTIVE
                lo = (float(t0) - pad) if t0 is not None else ts - pad
                hi = (float(t1) + pad) if t1 is not None else ts + pad
                if lo <= ts <= hi:
                    return True
    except Exception:
        pass
    try:
        from app.storage import get_store
        store = store or get_store()
        events = store.list_calendar_events(
            start_min=ts - 12 * 3600, start_max=ts + 12 * 3600, limit=80)
    except Exception:
        events = []
    for ev in events or []:
        if ev.get("all_day"):
            continue
        try:
            t0 = float(ev.get("start") or 0)
            t1 = float(ev.get("end") or t0)
        except (TypeError, ValueError):
            continue
        if (t0 - pad) <= ts <= (t1 + pad):
            return True
    return False


def audio_event_allowed(source: str = "", now: float | None = None,
                        store=None) -> bool:
    """Gate for mic/loopback events. Ambient opt-in OR meeting window."""
    src = (source or "").lower()
    is_audio = src.startswith("audio") or src in ("", "mic", "system_audio")
    if not is_audio:
        return True
    if allows_continuous("mic"):
        return True
    if not is_meeting_first():
        return True
    if not meeting_listen_ok():
        return False
    return in_meeting_window(now, store=store)


def set_ambient_opt_in(sources: dict[str, bool], *,
                       persist_consent: bool = True) -> dict[str, Any]:
    """UI nudge / wizard cards. Never silently enables capture."""
    amb = {s: bool(sources.get(s)) for s in AMBIENT_SOURCES if s in sources}
    state = save({"ambient": amb})
    if persist_consent:
        try:
            from app.services import capture_consent
            mapped = {}
            if "mic" in amb:
                mapped["mic"] = amb["mic"]
                mapped["save_audio"] = amb["mic"]
                mapped["system_audio"] = amb["mic"]
            if "webcam" in amb:
                mapped["webcam"] = amb["webcam"]
            if "desktop" in amb:
                mapped["screen"] = amb["desktop"]
            if mapped:
                capture_consent.save(mapped, consented=any(mapped.values()) or None)
        except Exception as exc:
            print(f"[first_run] consent mirror skipped ({exc}).")
    return state


def note_brief_ready(session_id: int | None, *, has_facts: bool,
                     href: str | None = None) -> dict[str, Any]:
    """Post-meeting first-win: bump brief count and queue a Console toast."""
    cur = load()
    n = int(cur.get("briefs_completed") or 0) + 1
    sid = int(session_id) if session_id is not None else 0
    link = href or (f"/meetings/{sid}" if sid else "/meetings")
    return save({
        "briefs_completed": n,
        "pending_first_win": {
            "session_id": sid or None,
            "href": link,
            "at": time.time(),
            "has_facts": bool(has_facts),
        },
    })


def consume_first_win() -> dict[str, Any] | None:
    cur = load()
    pend = cur.get("pending_first_win")
    if not isinstance(pend, dict):
        return None
    save({"pending_first_win": None})
    return dict(pend)


def unlock_card() -> dict[str, Any] | None:
    """One-time ambient offer after N successful briefs. UI-only."""
    cur = load()
    if cur.get("unlock_shown"):
        return None
    if not is_meeting_first():
        return None
    if int(cur.get("briefs_completed") or 0) < unlock_after_briefs():
        return None
    if any((cur.get("ambient") or {}).values()):
        return None
    return {
        "show": True,
        "after_briefs": unlock_after_briefs(),
        "completed": int(cur.get("briefs_completed") or 0),
        "sources": list(AMBIENT_SOURCES),
        "copy": (
            "Sparrow can keep listening between meetings — mic, camera, and "
            "screen stay off until you turn each one on. Nothing is enabled "
            "by this card alone."
        ),
    }


def mark_unlock_shown() -> dict[str, Any]:
    return save({"unlock_shown": True})


def next_meeting(store=None, *, now: float | None = None) -> dict[str, Any] | None:
    """Soonest non-all-day calendar event at or after now."""
    ts = time.time() if now is None else float(now)
    try:
        from app.storage import get_store
        store = store or get_store()
        events = store.list_calendar_events(
            start_min=ts - 300, start_max=ts + 14 * 86400, limit=40)
    except Exception:
        events = []
    best = None
    for ev in events or []:
        if ev.get("all_day"):
            continue
        try:
            start = float(ev.get("start") or 0)
        except (TypeError, ValueError):
            continue
        if start + 3600 < ts:
            continue
        if best is None or start < float(best.get("start") or 0):
            best = ev
    if not best:
        return None
    return {
        "id": best.get("id"),
        "title": best.get("title") or "Untitled meeting",
        "start": best.get("start"),
        "end": best.get("end"),
        "attendees": best.get("attendees") or [],
    }


def status() -> dict[str, Any]:
    cur = load()
    return {
        "mode": mode(),
        "meeting_first": is_meeting_first(),
        "meeting_listen_consent": bool(cur.get("meeting_listen_consent")),
        "ambient": dict(cur.get("ambient") or {}),
        "allows_continuous_mic": allows_continuous("mic"),
        "in_meeting_window": in_meeting_window(),
        "pad_s": meeting_pad_s(),
        "briefs_completed": int(cur.get("briefs_completed") or 0),
        "unlock_after": unlock_after_briefs(),
        "unlock": unlock_card(),
        "pending_first_win": cur.get("pending_first_win"),
        "next_meeting": next_meeting(),
    }
