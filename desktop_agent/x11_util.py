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
        try:
            net = w.get_full_property(
                d.intern_atom("_NET_WM_NAME"), d.intern_atom("UTF8_STRING"))
            if net and net.value:
                raw = net.value
                if isinstance(raw, bytes):
                    return raw.decode("utf-8", errors="replace").strip()
                return str(raw).strip()
        except Exception:
            pass
        title = w.get_wm_name() or ""
        if isinstance(title, bytes):
            title = title.decode("utf-8", errors="replace")
        return (title or "").strip()
    except Exception:
        return ""


def active_window() -> dict:
    """Foreground window as ``{hwnd, window}`` (hwnd = XID). Empty on failure.

    Shape matches Win32 ``_foreground_window`` in desktop_capture so callers
    can share intake filters without an OS branch.
    """
    if not session_ok():
        return {}
    try:
        d = display()
        root = d.screen().root
        prop = root.get_full_property(
            d.intern_atom("_NET_ACTIVE_WINDOW"), d.intern_atom("WINDOW"))
        if not prop or not prop.value:
            return {}
        xid = int(prop.value[0])
        if not xid:
            return {}
        out: dict = {"hwnd": xid}
        title = window_title(xid)
        if title:
            out["window"] = title
        return out
    except Exception:
        return {}


def _ewmh_message(xid: int, message: str, data: list[int]) -> None:
    """Send one EWMH ClientMessage to the root window on a mapped window's
    behalf — the ONLY way a client may change WM-owned state (_NET_WM_STATE,
    _NET_ACTIVE_WINDOW, …) once a window is mapped. (An earlier version built
    the event as ``X.ClientMessage(...)`` — an int constant, not a
    constructor — so the send always threw and was swallowed.)"""
    from Xlib import X
    from Xlib.protocol import event as xevent

    d = display()
    w = d.create_resource_object("window", int(xid))
    payload = [(int(v) & 0xFFFFFFFF) for v in (list(data) + [0] * 5)[:5]]
    ev = xevent.ClientMessage(window=w,
                              client_type=d.intern_atom(message),
                              data=(32, payload))
    d.screen().root.send_event(
        ev, event_mask=(X.SubstructureRedirectMask
                        | X.SubstructureNotifyMask))
    d.sync()


def set_skip_taskbar(xid: int, on: bool) -> bool:
    """Add/remove the skip-taskbar + skip-pager states (EWMH message)."""
    if not session_ok() or not xid:
        return False
    try:
        d = display()
        action = 1 if on else 0        # _NET_WM_STATE_ADD / _NET_WM_STATE_REMOVE
        _ewmh_message(xid, "_NET_WM_STATE",
                      [action,
                       d.intern_atom("_NET_WM_STATE_SKIP_TASKBAR"),
                       d.intern_atom("_NET_WM_STATE_SKIP_PAGER"),
                       1])             # source: normal application
        return True
    except Exception:
        return False


def _move(xid: int, x: int, y: int) -> bool:
    """Move a top-level window (configure + EWMH moveresize fallback)."""
    if not session_ok() or not xid:
        return False
    try:
        d = display()
        w = d.create_resource_object("window", int(xid))
        w.configure(x=int(x), y=int(y))
        d.sync()
        # Some WMs ignore a bare configure on managed windows; the EWMH
        # message asks the WM itself. l[0]: gravity NorthWest(1) | x-bit
        # (1<<8) | y-bit (1<<9) | source application (1<<12).
        try:
            _ewmh_message(xid, "_NET_MOVERESIZE_WINDOW",
                          [1 | (1 << 8) | (1 << 9) | (1 << 12),
                           int(x), int(y), 0, 0])
        except Exception:
            pass
        return True
    except Exception:
        return False


def move_offscreen(xid: int) -> bool:
    """Park a window at (-32000,-32000); skip taskbar when the WM allows."""
    if not session_ok() or not xid:
        return False
    set_skip_taskbar(xid, True)
    return _move(xid, -32000, -32000)


def iconify(xid: int) -> bool:
    """Minimize a window (ICCCM WM_CHANGE_STATE → IconicState). The reliable
    X11 hide: WMs like mutter CLAMP off-screen moves back onto the display,
    but honour iconify everywhere — and a Chromium driven over CDP keeps
    rendering (Playwright screenshots still work while iconified)."""
    if not session_ok() or not xid:
        return False
    try:
        _ewmh_message(xid, "WM_CHANGE_STATE", [3])   # IconicState
        return True
    except Exception:
        return False


def net_wm_state(xid: int) -> set[str]:
    """The window's _NET_WM_STATE atom names (empty set on any failure)."""
    if not session_ok() or not xid:
        return set()
    try:
        d = display()
        w = d.create_resource_object("window", int(xid))
        p = w.get_full_property(d.intern_atom("_NET_WM_STATE"),
                                d.intern_atom("ATOM"))
        if not p:
            return set()
        return {d.get_atom_name(a) for a in p.value}
    except Exception:
        return set()


def activate_window(xid: int) -> bool:
    """Raise + focus a window (EWMH activate; e.g. a sign-in handoff)."""
    if not session_ok() or not xid:
        return False
    try:
        from Xlib import X

        _ewmh_message(xid, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
        d = display()
        w = d.create_resource_object("window", int(xid))
        w.configure(stack_mode=X.Above)
        d.sync()
        return True
    except Exception:
        return False


def window_root_pos(xid: int) -> tuple[int, int] | None:
    """A window's top-left corner in root (screen) coordinates, or None."""
    if not session_ok() or not xid:
        return None
    try:
        d = display()
        w = d.create_resource_object("window", int(xid))
        p = d.screen().root.translate_coords(w, 0, 0)
        # translate_coords returns unsigned 16-bit ints; recover negatives
        # so a window parked at -32000 doesn't read as +33536.
        def _signed(v: int) -> int:
            v = int(v)
            return v - 0x10000 if v >= 0x8000 else v
        return _signed(p.x), _signed(p.y)
    except Exception:
        return None
