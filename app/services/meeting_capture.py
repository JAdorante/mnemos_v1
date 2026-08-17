"""Start/stop mic only inside calendar-anchored meeting windows.

Used when QUILL_FIRST_RUN_MODE=meeting so always-on capture never arms from
boot. The poller is a no-op when the user later opts into ambient mic.
"""
from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_armed = False


def _start_meeting_audio() -> None:
    from app.api.routes import start_all
    from app.services import first_run
    if not first_run.meeting_listen_ok() and not first_run.allows_continuous("mic"):
        return
    start_all(audio=True, vision=False, notifications=False,
              desktop_capture=False, system_audio=True)


def _stop_meeting_audio() -> None:
    from app.api import routes as R
    from app.services import first_run
    if first_run.allows_continuous("mic"):
        return
    # Pause mic + loopback; leave notifications/webcam/screen as they are.
    try:
        R._pause_source("mic")
    except Exception:
        pass
    try:
        R._pause_source("system_audio")
    except Exception:
        pass


def sync(*, now: float | None = None) -> dict[str, Any]:
    """Arm or disarm meeting audio to match the calendar window."""
    global _armed
    from app.services import first_run
    if first_run.allows_continuous("mic"):
        return {"armed": False, "reason": "ambient_mic"}
    if not first_run.is_meeting_first():
        return {"armed": False, "reason": "not_meeting_first"}
    want = first_run.meeting_listen_ok() and first_run.in_meeting_window(now)
    with _lock:
        if want and not _armed:
            try:
                _start_meeting_audio()
                _armed = True
            except Exception as exc:
                print(f"[meeting_capture] start skipped ({exc}).")
                return {"armed": False, "error": str(exc)}
        elif not want and _armed:
            try:
                _stop_meeting_audio()
            except Exception as exc:
                print(f"[meeting_capture] stop skipped ({exc}).")
            _armed = False
        return {"armed": _armed, "want": want}


def _loop() -> None:
    while not _stop.is_set():
        try:
            sync()
        except Exception as exc:
            print(f"[meeting_capture] tick skipped ({exc}).")
        _stop.wait(5.0)


def attach() -> None:
    global _thread
    from app.services import first_run
    if not first_run.is_meeting_first():
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="meeting-capture-gate",
                               daemon=True)
    _thread.start()


def reset() -> None:
    """Tests: drop armed flag (does not touch live pipelines)."""
    global _armed
    with _lock:
        _armed = False
