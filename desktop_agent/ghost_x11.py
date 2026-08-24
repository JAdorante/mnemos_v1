"""Ghost desktop on X11 — park windows off-screen and stream frames to chat.

Parallel to ghost_win.py: after launch_app, new top-level windows belonging to
the launched app are moved to (-32000,-32000) and marked skip-taskbar when the
WM supports it. The agent drives them via AT-SPI; frames are captured with
XGetImage and published to the same ghost relay the chat pane polls.

Requires an X11 session (not native Wayland). Fails soft elsewhere.
"""
from __future__ import annotations

import io
import threading
import time

from . import config as cfg
from . import x11_util

_PARK_X = -32000
_lock = threading.Lock()
_parked: dict[int, str] = {}      # xid -> app key


def enabled() -> bool:
    return x11_util.session_ok() and cfg.GHOST_DESKTOP


def ghostable(app: str) -> bool:
    if not enabled():
        return False
    return (app or "").strip().lower() not in cfg.GHOST_DESKTOP_EXCLUDE


def parked_apps() -> dict[int, str]:
    with _lock:
        return dict(_parked)


def snapshot_windows() -> set[int]:
    return x11_util.snapshot_window_ids()


def _window_app_matches(xid: int, app: str) -> bool:
    want = x11_util.app_basenames(app)
    if not want:
        return False
    for wid, pid, _title in x11_util.client_windows():
        if wid != xid:
            continue
        exe = x11_util.exe_for_pid(pid).lower()
        return exe in want
    return False


def park_new_windows(app: str, before: set[int], retries: int = 10,
                     delay_s: float = 0.4) -> dict:
    if not enabled():
        return {"ok": False, "reason": "ghost desktop disabled"}
    try:
        for _ in range(max(1, retries)):
            fresh = [xid for xid in snapshot_windows() - before
                     if x11_util.window_title(xid)
                     and _window_app_matches(xid, app)]
            if fresh:
                for xid in fresh:
                    x11_util.move_offscreen(xid)
                    with _lock:
                        _parked[xid] = (app or "").strip().lower()
                    _track_in_relay(xid)
                publish_frame(fresh[0])
                return {"ok": True, "windows": len(fresh), "hwnd": fresh[0]}
            time.sleep(delay_s)
        return {"ok": False, "reason": "no new window appeared (single-instance "
                                       "app reusing an existing window?)"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _track_in_relay(xid: int) -> None:
    try:
        from browser_agent import ghost as relay
        relay._ghost_hwnds.add(int(xid))
    except Exception:
        pass


def window_png(xid: int) -> bytes | None:
    """Render a top-level window to PNG (works when parked off-screen)."""
    if not xid or not x11_util.session_ok():
        return None
    try:
        from Xlib import X
        from PIL import Image

        d = x11_util.display()
        w = d.create_resource_object("window", int(xid))
        geom = w.get_geometry()
        width, height = int(geom.width), int(geom.height)
        if not (0 < width <= 8192 and 0 < height <= 8192):
            return None
        raw = w.get_image(0, 0, width, height, X.ZPixmap, 0xFFFFFFFF)
        img = Image.frombytes("RGB", (width, height), raw.data, "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def publish_frame(xid: int) -> bool:
    png = window_png(xid)
    if not png:
        return False
    try:
        from browser_agent import ghost as relay
        relay.publish(png, url="",
                      title=x11_util.window_title(xid) or "Agent desktop")
        return True
    except Exception:
        return False
