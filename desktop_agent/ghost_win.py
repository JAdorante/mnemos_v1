"""Ghost desktop — launch apps parked off-screen and stream their frames.

The ghost-browser treatment, generalized to native windows: after launch_app,
any new top-level window belonging to the launched app is moved to the parking
spot (-32000,-32000) and stripped of its taskbar button, so the agent's apps
never take the user's screen. The agent drives them via UI Automation (which
doesn't need visibility), and after each action the window is rendered with
PrintWindow — which composites even off-screen windows — and published to the
same frame relay the chat's ghost pane already polls.

Exclusions (config GHOST_DESKTOP_EXCLUDE, default flstudio/phonelink): apps
whose flows genuinely need the real screen (pixel/canvas UIs, special drivers)
launch visibly as before.

Windows-only; every entry point no-ops or fails soft elsewhere.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import config as cfg

_PARK_X = -32000
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x0000_0080
_WS_EX_APPWINDOW = 0x0004_0000
_SW_HIDE, _SW_SHOWNOACTIVATE = 0, 4
_SWP_NOSIZE, _SWP_NOMOVE = 0x0001, 0x0002
_SWP_NOZORDER, _SWP_FRAMECHANGED = 0x0004, 0x0020
_PW_RENDERFULLCONTENT = 0x0000_0002

_lock = threading.Lock()
_parked: dict[int, str] = {}      # hwnd -> app key


def enabled() -> bool:
    return os.name == "nt" and cfg.GHOST_DESKTOP


def ghostable(app: str) -> bool:
    """May this app be parked? Pixel/canvas flows need the real screen."""
    if not enabled():
        return False
    return (app or "").strip().lower() not in cfg.GHOST_DESKTOP_EXCLUDE


def parked_apps() -> dict[int, str]:
    with _lock:
        return dict(_parked)


def _user32():
    import ctypes
    return ctypes.windll.user32


def snapshot_windows() -> set[int]:
    """All visible top-level windows (any class) before a launch."""
    if os.name != "nt":
        return set()
    try:
        import ctypes
        from ctypes import wintypes

        user32 = _user32()
        found: set[int] = set()

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lp):
            if user32.IsWindowVisible(hwnd):
                found.add(hwnd)
            return True

        user32.EnumWindows(_enum, 0)
        return found
    except Exception:
        return set()


def _set_toolwindow(hwnd: int, on: bool) -> None:
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


def _window_title(hwnd: int) -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(160)
    _user32().GetWindowTextW(hwnd, buf, 160)
    return buf.value


def _window_app_matches(hwnd: int, app: str) -> bool:
    """Does this window's process exe belong to the launched app?"""
    try:
        from . import uia
        exe = Path(uia._exe_for_pid(_pid_of(hwnd))).name.lower()
        return bool(exe) and exe in uia._app_basenames(app)
    except Exception:
        return False


def _pid_of(hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def park_new_windows(app: str, before: set[int], retries: int = 10,
                     delay_s: float = 0.4) -> dict:
    """Park every new visible window of `app` that appeared since `before`.

    Best-effort: single-instance apps (Win11 Notepad tabs) may open no new
    window at all — that's reported, not an error, and the app stays visible.
    """
    if not enabled():
        return {"ok": False, "reason": "ghost desktop disabled"}
    try:
        user32 = _user32()
        for _ in range(max(1, retries)):
            fresh = [h for h in snapshot_windows() - before
                     if _window_title(h) and _window_app_matches(h, app)]
            if fresh:
                for h in fresh:
                    # Style first, THEN park: the show inside the style flip
                    # makes some apps (Explorer) restore their saved placement,
                    # which would undo an earlier move.
                    _set_toolwindow(h, True)
                    user32.SetWindowPos(h, 0, _PARK_X, _PARK_X, 0, 0,
                                        _SWP_NOSIZE | _SWP_NOZORDER)
                    with _lock:
                        _parked[h] = (app or "").strip().lower()
                    _track_in_relay(h)
                publish_frame(fresh[0])
                return {"ok": True, "windows": len(fresh), "hwnd": fresh[0]}
            time.sleep(delay_s)
        return {"ok": False, "reason": "no new window appeared (single-instance "
                                       "app reusing an existing window?)"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _track_in_relay(hwnd: int) -> None:
    """Register with the browser ghost registry so /agent/ghost/reveal|park
    round-trips desktop windows too. Guarded — desktop_agent must stay
    importable without browser_agent."""
    try:
        from browser_agent import ghost as relay
        relay._ghost_hwnds.add(hwnd)
    except Exception:
        pass


# --- capture -----------------------------------------------------------------
def window_png(hwnd: int) -> bytes | None:
    """Render a window (even parked off-screen) to PNG via PrintWindow."""
    if os.name != "nt" or not hwnd:
        return None
    import ctypes
    from ctypes import wintypes

    user32, gdi32 = _user32(), ctypes.windll.gdi32
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if not (0 < w <= 8192 and 0 < h <= 8192):
        return None

    class _BMIH(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(hdc_mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, hdc_mem, _PW_RENDERFULLCONTENT):
            return None
        bmi = _BMIH(biSize=ctypes.sizeof(_BMIH), biWidth=w, biHeight=-h,
                    biPlanes=1, biBitCount=32, biCompression=0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bmi), 0) != h:
            return None
        import io

        from PIL import Image
        img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return None
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)


def publish_frame(hwnd: int) -> bool:
    """Capture the window and drop it into the chat's ghost-pane relay."""
    png = window_png(hwnd)
    if not png:
        return False
    try:
        from browser_agent import ghost as relay
        relay.publish(png, url="", title=_window_title(hwnd) or "Agent desktop")
        return True
    except Exception:
        return False
