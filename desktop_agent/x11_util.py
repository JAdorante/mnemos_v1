"""Shared X11 helpers for Linux desktop automation (AT-SPI + ghost pane).

Best-effort only — every entry point fails soft. Requires DISPLAY and an X11
session (Wayland returns empty hands until a portal path exists).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

_display_lock = threading.Lock()
_display = None


def session_ok() -> bool:
    """True when an X11 display is available (ghost + window capture)."""
    if os.name != "posix":
        return False
    if not os.environ.get("DISPLAY"):
        return False
    # Wayland compositors often set DISPLAY for XWayland, but parking is
    # unreliable there — honour an explicit session type when present.
    st = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if st == "wayland":
        return False
    return True


def display():
    """Lazy singleton X display."""
    global _display
    if not session_ok():
        raise RuntimeError("no X11 display")
    with _display_lock:
        if _display is None:
            from Xlib import display as xdisplay

            _display = xdisplay.Display()
        return _display


def exe_for_pid(pid: int) -> str:
    try:
        return os.path.basename(os.readlink(f"/proc/{int(pid)}/exe"))
    except Exception:
        return ""


def app_basenames(app: str) -> set[str]:
    """Executable basenames an allowlisted app key may resolve to."""
    from . import config as cfg

    names: set[str] = set()
    for c in cfg.APP_CANDIDATES.get(app, []):
        base = Path(c).name.lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if base:
            names.add(base)
    resolved = cfg.resolve_app_path(app)
    if resolved:
        base = Path(resolved).name.lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if base:
            names.add(base)
    return names


def client_windows() -> list[tuple[int, int, str]]:
    """Visible top-level windows: (xid, pid, title)."""
    if not session_ok():
        return []
    try:
        d = display()
        root = d.screen().root
        prop = root.get_full_property(
            d.intern_atom("_NET_CLIENT_LIST"), d.intern_atom("WINDOW"))
        if not prop:
            return []
        out: list[tuple[int, int, str]] = []
        for wid in prop.value:
            try:
                w = d.create_resource_object("window", wid)
                pid_p = w.get_full_property(
                    d.intern_atom("_NET_WM_PID"), d.intern_atom("CARDINAL"))
                pid = int(pid_p.value[0]) if pid_p else 0
                title = w.get_wm_name() or ""
                out.append((int(wid), pid, title))
            except Exception:
                continue
        return out
    except Exception:
        return []


def snapshot_window_ids() -> set[int]:
    return {xid for xid, _pid, _title in client_windows()}


def windows_for_app(app: str) -> list[tuple[int, int, str]]:
    want = app_basenames(app)
    if not want:
        return []
    out = []
    for xid, pid, title in client_windows():
        exe = exe_for_pid(pid).lower()
        if exe in want:
            out.append((xid, pid, title))
    return out


def window_title(xid: int) -> str:
    if not session_ok() or not xid:
        return ""
    try:
        d = display()
        w = d.create_resource_object("window", int(xid))
        return w.get_wm_name() or ""
    except Exception:
        return ""


def move_offscreen(xid: int) -> bool:
    """Park a window at (-32000,-32000); skip taskbar when the WM allows."""
    if not session_ok() or not xid:
        return False
    park = -32000
    try:
        d = display()
        w = d.create_resource_object("window", int(xid))
        # Skip taskbar / pager when supported.
        for state in ("_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER"):
            try:
                w.change_property(d.intern_atom("_NET_WM_STATE"),
                                  d.intern_atom("ATOM"), 32, [d.intern_atom(state)],
                                  d.intern_atom("_NET_WM_STATE_ADD"))
            except Exception:
                pass
        w.configure(x=park, y=park)
        d.sync()
        # EWMH moveresize — some WMs ignore a bare configure.
        try:
            from Xlib import X

            ev = X.ClientMessage(
                display=d, window=int(xid),
                client_type=d.intern_atom("_NET_MOVERESIZE_WINDOW"),
                data=(32, [0, park, park, 0, 0, 0]))
            root = d.screen().root
            root.send_event(ev, (X.SubstructureRedirectMask
                                 | X.SubstructureNotifyMask))
            d.sync()
        except Exception:
            pass
        return True
    except Exception:
        return False
