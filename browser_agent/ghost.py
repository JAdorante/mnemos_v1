"""Ghost-browser frame relay — the agent's live view, streamed into the chat UI.

The BrowserDriver publishes a PNG of its page after every scan/action; the chat
page polls GET /agent/ghost/frame and renders it in a small pane. The browser
window itself runs headless or parked off-screen (QUILL_GHOST_BROWSER), so the
agent never takes the user's screen or mouse.

Thread-safety: frames are produced on the Playwright-bound worker thread and
consumed from FastAPI request threads — a plain lock around one slot is enough
(latest-wins; the relay deliberately keeps no history).
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_frame: bytes | None = None
_meta: dict = {"ts": 0.0, "url": "", "title": ""}

# A frame older than this is treated as "no live agent view" by the UI.
FRESH_S = 20.0


def publish(png: bytes, *, url: str = "", title: str = "") -> None:
    """Latest-wins frame drop. Never raises — the agent loop must not care."""
    global _frame
    if not png:
        return
    with _lock:
        _frame = png
        _meta.update(ts=time.time(), url=url or "", title=title or "")


def latest() -> tuple[bytes, dict] | None:
    """The newest frame and its metadata, or None if nothing was published."""
    with _lock:
        if _frame is None:
            return None
        return _frame, dict(_meta)


def meta() -> dict:
    """Cheap freshness/state probe for the UI (no frame bytes)."""
    with _lock:
        age = (time.time() - _meta["ts"]) if _meta["ts"] else None
        return {
            "has_frame": _frame is not None,
            "age_s": round(age, 1) if age is not None else None,
            "fresh": _frame is not None and age is not None and age < FRESH_S,
            "url": _meta["url"],
            "title": _meta["title"],
        }


def clear() -> None:
    global _frame
    with _lock:
        _frame = None
        _meta.update(ts=0.0, url="", title="")


# --- parked-window management (Windows best-effort) -------------------------
# In "hidden" mode the headed agent window is parked at -32000,-32000. The only
# window we will ever touch is one that is ALREADY parked off-screen (the user's
# own browser can never match that), remembered so it can be parked again.
_PARK_X = -32000
_revealed_hwnd: int | None = None

# user32 constants
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x0000_0080     # no taskbar button, skipped by Alt-Tab
_WS_EX_APPWINDOW = 0x0004_0000      # forces a taskbar button
_SW_HIDE, _SW_SHOWNOACTIVATE = 0, 4
_SWP_NOSIZE, _SWP_NOMOVE = 0x0001, 0x0002
_SWP_NOZORDER, _SWP_FRAMECHANGED = 0x0004, 0x0020


def _user32():
    import ctypes
    return ctypes.windll.user32


_ghost_hwnds: set[int] = set()      # windows we parked (snapshot-diff at launch)


def _widgetwin_hwnds() -> set[int]:
    """All visible top-level Chromium-family windows (class *WidgetWin*)."""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    found: set[int] = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if "WidgetWin" in cls.value:
            found.add(hwnd)
        return True

    user32.EnumWindows(_enum, 0)
    return found


def snapshot_windows() -> set[int]:
    """Pre-launch snapshot; hide_new_windows() parks whatever appears after.
    Chromium clamps --window-position back onto the display, so the only
    reliable hide is a post-launch SetWindowPos (never clamped)."""
    import os
    if os.name != "nt":
        return set()
    try:
        return _widgetwin_hwnds()
    except Exception:
        return set()


def _parked_hwnds() -> list[int]:
    """Windows we parked, still alive; falls back to the position discriminator
    (a window at the parking spot can only be ours)."""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    alive = [h for h in _ghost_hwnds if user32.IsWindow(h)]
    if alive:
        return alive
    found: list[int] = []
    for hwnd in _widgetwin_hwnds():
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.left <= _PARK_X + 4000:
            found.append(hwnd)
    return found


def _set_toolwindow(hwnd: int, on: bool) -> None:
    """Add/remove the tool-window style. The window is briefly hidden while the
    style flips — Windows only re-evaluates taskbar presence on show."""
    user32 = _user32()
    get_l = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_l = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    ex = get_l(hwnd, _GWL_EXSTYLE)
    ex = (ex | _WS_EX_TOOLWINDOW) & ~_WS_EX_APPWINDOW if on \
        else (ex & ~_WS_EX_TOOLWINDOW) | _WS_EX_APPWINDOW
    user32.ShowWindow(hwnd, _SW_HIDE)
    set_l(hwnd, _GWL_EXSTYLE, ex)
    user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                        _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOZORDER
                        | _SWP_FRAMECHANGED)


def hide_new_windows(before: set[int], retries: int = 10,
                     delay_s: float = 0.3) -> dict:
    """Park every Chromium window that appeared since `before` (the launch
    snapshot): move it off-screen and strip its taskbar button. Retries
    briefly — the window can lag the Playwright call."""
    import os
    if os.name != "nt":
        return {"ok": False, "reason": "windows only"}
    try:
        user32 = _user32()
        for _ in range(max(1, retries)):
            new = _widgetwin_hwnds() - before
            if new:
                for h in new:
                    user32.SetWindowPos(h, 0, _PARK_X, _PARK_X, 0, 0,
                                        _SWP_NOSIZE | _SWP_NOZORDER)
                    _set_toolwindow(h, True)
                    _ghost_hwnds.add(h)
                return {"ok": True, "windows": len(new)}
            time.sleep(delay_s)
        return {"ok": False, "reason": "no new agent window appeared"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def reveal_window() -> dict:
    """Bring the parked agent window on-screen with its taskbar button back
    (e.g. for a sign-in handoff)."""
    global _revealed_hwnd
    import os
    if os.name != "nt":
        return {"ok": False, "reason": "windows only"}
    try:
        found = _parked_hwnds()
        if not found:
            return {"ok": False, "reason": "no parked agent window found"}
        hwnd = found[0]
        _set_toolwindow(hwnd, False)
        _user32().SetWindowPos(hwnd, 0, 80, 60, 0, 0,
                               _SWP_NOSIZE | _SWP_NOZORDER)
        _revealed_hwnd = hwnd
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def park_window() -> dict:
    """Move a previously revealed agent window back off-screen, taskbar-less."""
    global _revealed_hwnd
    import os
    if os.name != "nt":
        return {"ok": False, "reason": "windows only"}
    if not _revealed_hwnd:
        return {"ok": False, "reason": "nothing was revealed"}
    try:
        _user32().SetWindowPos(_revealed_hwnd, 0, _PARK_X, _PARK_X, 0, 0,
                               _SWP_NOSIZE | _SWP_NOZORDER)
        _set_toolwindow(_revealed_hwnd, True)
        _revealed_hwnd = None
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
