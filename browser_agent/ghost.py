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


# --- parked-window management (Windows + X11, best-effort) ------------------
# In "hidden" mode the headed agent window is hidden after launch: Windows
# parks it at -32000,-32000 via user32; X11 iconifies it with skip-taskbar
# (WMs like mutter clamp off-screen moves, but honour iconify — and CDP
# screenshots keep feeding the ghost pane while iconified). Either way the
# only window we will ever touch is one WE hid — remembered by handle, with a
# discriminator no user-owned window can match (the off-screen position on
# Windows; hidden+skip-taskbar together on X11). Wayland and hosted-headless
# fail soft with an honest reason — sign-in reveal needs a real window on a
# real display.
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


def _x11():
    """desktop_agent.x11_util when a usable X11 session exists, else None."""
    import os
    if os.name == "nt":
        return None
    try:
        from desktop_agent import x11_util
        if x11_util.session_ok():
            return x11_util
    except Exception:
        pass
    return None


# Executable basenames the agent's Chromium-family window may run under
# (Playwright's bundled build is "chrome"; channels add edge/brave).
_CHROMIUM_EXES = ("chrome", "chromium", "chromium-browser", "msedge", "brave")


def _x11_browser_xids(x) -> set[int]:
    """Top-level X11 windows owned by a Chromium-family process."""
    found: set[int] = set()
    for xid, pid, _title in x.client_windows():
        exe = x.exe_for_pid(pid).lower()
        if exe in _CHROMIUM_EXES or exe.startswith("chrom"):
            found.add(int(xid))
    return found


def _no_window_reason() -> str:
    """Why there is no agent window to reveal on this install."""
    try:
        from . import config as cfg
        if cfg.GHOST_MODE == "headless":
            return ("the agent browser runs fully headless here — there is "
                    "no window to reveal (on a desktop install, "
                    "QUILL_GHOST_BROWSER=hidden enables sign-in handoffs)")
    except Exception:
        pass
    return ("no desktop window session available — revealing the agent "
            "window needs Windows or a Linux X11 session")


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
    reliable hide is a post-launch move (never clamped)."""
    import os
    if os.name == "nt":
        try:
            return _widgetwin_hwnds()
        except Exception:
            return set()
    x = _x11()
    if x is not None:
        try:
            return x.snapshot_window_ids()
        except Exception:
            return set()
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
    if os.name == "nt":
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
    x = _x11()
    if x is None:
        return {"ok": False, "reason": _no_window_reason()}
    try:
        for _ in range(max(1, retries)):
            new = _x11_browser_xids(x) - set(before)
            if new:
                # Iconify, not an off-screen move — WMs like mutter clamp
                # coordinates back onto the display. ORDER MATTERS: mutter
                # refuses to minimize a window that is already skip-taskbar,
                # so hide first, then strip the taskbar button (HIDDEN
                # survives gaining skip-taskbar). Together the two states
                # make the window invisible AND are the reveal discriminator
                # (a user's own minimized browser keeps its taskbar button,
                # so it can never match).
                _ghost_hwnds.update(new)
                hidden = _iconify_until_hidden(x, new)
                for xid in new:
                    x.set_skip_taskbar(xid, True)
                if not hidden:
                    return {"ok": False, "windows": len(new),
                            "reason": "window manager kept the window visible"}
                return {"ok": True, "windows": len(new)}
            time.sleep(delay_s)
        return {"ok": False, "reason": "no new agent window appeared"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _iconify_until_hidden(x, xids, attempts: int = 8,
                          delay_s: float = 0.4) -> bool:
    """Iconify until _NET_WM_STATE_HIDDEN sticks. The WM applies state
    asynchronously — and mutter drops a WM_CHANGE_STATE that lands while the
    window is still mid-map right after launch — so fire-and-forget is not
    enough; verify and re-send."""
    remaining = {int(i) for i in xids}
    for _ in range(max(1, attempts)):
        for xid in list(remaining):
            if "_NET_WM_STATE_HIDDEN" in x.net_wm_state(xid):
                remaining.discard(xid)
            else:
                x.iconify(xid)
        if not remaining:
            return True
        time.sleep(delay_s)
    return not remaining


def _parked_x11_xids(x) -> list[int]:
    """X11 twin of _parked_hwnds: tracked-and-alive first, else the state
    discriminator — hidden AND skip-taskbar together only ever describe OUR
    parked window (a user's own minimized browser keeps its taskbar button)."""
    alive_ids = x.snapshot_window_ids()
    alive = [h for h in _ghost_hwnds if h in alive_ids]
    if alive:
        return alive
    found: list[int] = []
    for xid in _x11_browser_xids(x):
        states = x.net_wm_state(xid)
        if ("_NET_WM_STATE_HIDDEN" in states
                and "_NET_WM_STATE_SKIP_TASKBAR" in states):
            found.append(xid)
    return found


def is_parked() -> bool:
    """True while an agent window WE hid is meant to STAY hidden (parked and
    not currently revealed). The driver checks this before bring_to_front —
    CDP's Page.bringToFront activates the X window, which deiconifies an X11
    park (proven live: every real-site navigation step unhid the window)."""
    return bool(_ghost_hwnds) and not _revealed_hwnd


def can_reveal() -> bool:
    """True when a parked agent window exists to reveal. Cheap and
    side-effect-free — chat uses it to decide whether a sign-in ask should
    mention the reveal button (a headless install must not advertise it)."""
    import os
    if os.name == "nt":
        try:
            return bool(_parked_hwnds())
        except Exception:
            return False
    x = _x11()
    if x is None:
        return False
    try:
        return bool(_parked_x11_xids(x))
    except Exception:
        return False


def reveal_window() -> dict:
    """Bring the parked agent window on-screen with its taskbar button back
    (e.g. for a sign-in handoff)."""
    global _revealed_hwnd
    import os
    if os.name == "nt":
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
    x = _x11()
    if x is None:
        return {"ok": False, "reason": _no_window_reason()}
    try:
        found = _parked_x11_xids(x)
        if not found:
            return {"ok": False, "reason": "no parked agent window found"}
        xid = found[0]
        x.set_skip_taskbar(xid, False)
        x.activate_window(xid)     # deiconifies, raises, and focuses
        _revealed_hwnd = xid
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def park_window() -> dict:
    """Move a previously revealed agent window back off-screen, taskbar-less."""
    global _revealed_hwnd
    import os
    if not _revealed_hwnd:
        return {"ok": False, "reason": "nothing was revealed"}
    if os.name == "nt":
        try:
            _user32().SetWindowPos(_revealed_hwnd, 0, _PARK_X, _PARK_X, 0, 0,
                                   _SWP_NOSIZE | _SWP_NOZORDER)
            _set_toolwindow(_revealed_hwnd, True)
            _revealed_hwnd = None
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    x = _x11()
    if x is None:
        return {"ok": False, "reason": _no_window_reason()}
    try:
        # Same order as the launch hide: mutter refuses to minimize a
        # skip-taskbar window, so iconify first, strip the button after.
        _iconify_until_hidden(x, [_revealed_hwnd], attempts=4)
        x.set_skip_taskbar(_revealed_hwnd, True)
        _revealed_hwnd = None
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
