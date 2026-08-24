"""AT-SPI actuator for Linux — drive allowlisted app windows without the mouse.

Mirrors the public API of uia.py so DesktopDriver stays platform-agnostic.
Uses PyGObject's Atspi bindings (system site-packages on Ubuntu/Debian).

Security: same fail-closed allowlist as Windows — only apps whose process exe
matches registry candidates can be scanned or driven.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from . import config as cfg
from . import x11_util

_lock = threading.Lock()
_last_controls: list = []
_last_window = None          # AT-SPI Accessible (app root)
_last_window_xid: int = 0
_last_app = ""

MAX_CONTROLS = 120
_ATSPI = None

_INTERESTING = {
    "push button": "button",
    "toggle button": "button",
    "check box": "checkbox",
    "combo box": "combobox",
    "entry": "edit",
    "text": "edit",
    "hyperlink": "link",
    "list item": "listitem",
    "menu item": "menuitem",
    "radio button": "radio",
    "page tab": "tab",
    "tree item": "treeitem",
    "document": "document",
}


def _ensure_atspi():
    global _ATSPI
    if _ATSPI is not None:
        return _ATSPI
    dist = "/usr/lib/python3/dist-packages"
    if os.path.isdir(dist) and dist not in sys.path:
        sys.path.insert(0, dist)
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    _ATSPI = Atspi
    return Atspi


def available() -> bool:
    if os.name != "posix":
        return False
    try:
        Atspi = _ensure_atspi()
        Atspi.get_desktop(0)
        return True
    except Exception:
        return False


def _exe_for_pid(pid: int) -> str:
    return x11_util.exe_for_pid(pid)


def _app_basenames(app: str) -> set[str]:
    return x11_util.app_basenames(app)


def find_windows(app: str) -> list:
    Atspi = _ensure_atspi()
    want = _app_basenames(app)
    if not want:
        return []
    desktop = Atspi.get_desktop(0)
    out = []
    for i in range(desktop.get_child_count()):
        try:
            node = desktop.get_child_at_index(i)
            if not node:
                continue
            pid = node.get_process_id()
            exe = _exe_for_pid(pid).lower()
            if exe in want:
                out.append(node)
        except Exception:
            continue
    return out


def _pick_xid(app: str, title_hint: str = "") -> int:
    wins = x11_util.windows_for_app(app)
    if not wins:
        return 0
    if title_hint:
        hint = title_hint.lower()
        for xid, _pid, title in wins:
            if hint in (title or "").lower():
                return xid
    return wins[0][0]


def _text_value(node, Atspi) -> str:
    try:
        n = Atspi.Text.get_character_count(node)
        if n <= 0:
            return ""
        return (Atspi.Text.get_text(node, 0, n) or "")[:120]
    except Exception:
        return ""


def _patterns(node, Atspi) -> list[str]:
    pats: list[str] = []
    try:
        n = node.get_n_actions()
        if n and n > 0:
            pats.append("invoke")
    except Exception:
        pass
    try:
        if node.get_editable_text_iface() is not None:
            pats.append("set_text")
    except Exception:
        pass
    try:
        ss = node.get_state_set()
        if ss.contains(Atspi.StateType.STATE_CHECKABLE):
            pats.append("toggle")
    except Exception:
        pass
    return pats


def _walk(node, registry: list, controls: list, Atspi) -> None:
    if len(registry) >= MAX_CONTROLS:
        return
    try:
        role = (node.get_role_name() or "").lower()
        ctype = _INTERESTING.get(role)
        if ctype:
            try:
                ss = node.get_state_set()
                if not ss.contains(Atspi.StateType.STATE_SHOWING):
                    ctype = None
            except Exception:
                pass
        if ctype:
            name = (node.name or "").strip()[:120]
            if not name and ctype in ("edit", "document"):
                try:
                    if node.get_editable_text_iface() is not None:
                        name = "(editor)"
                except Exception:
                    pass
            if name:
                item = {
                    "id": len(registry),
                    "type": ctype,
                    "name": name,
                    "automation_id": "",
                    "enabled": True,
                    "patterns": _patterns(node, Atspi),
                }
                if ctype in ("edit", "document", "combobox"):
                    item["value"] = _text_value(node, Atspi)
                controls.append(item)
                registry.append(node)
        for i in range(node.get_child_count()):
            _walk(node.get_child_at_index(i), registry, controls, Atspi)
    except Exception:
        return


def scan(app: str, title_hint: str = "") -> dict:
    global _last_controls, _last_window, _last_window_xid, _last_app
    Atspi = _ensure_atspi()
    wins = find_windows(app)
    if not wins:
        return {"ok": False, "reason": f"no open window for app '{app}'"}
    win = wins[0]
    if title_hint:
        # Prefer an X11 title match when multiple instances exist.
        xid = _pick_xid(app, title_hint)
        if xid:
            for node in wins:
                if node.get_process_id() == _pid_for_xid(xid):
                    win = node
                    break
    controls, registry = [], []
    _walk(win, registry, controls, Atspi)
    xid = _pick_xid(app, title_hint)
    title = x11_util.window_title(xid) if xid else (win.name or "")
    with _lock:
        _last_controls = registry
        _last_window = win
        _last_window_xid = xid
        _last_app = app
    pid = 0
    try:
        pid = int(win.get_process_id())
    except Exception:
        pass
    return {"ok": True,
            "window": {"title": (title or "")[:160], "pid": pid},
            "controls": controls,
            "truncated": len(registry) >= MAX_CONTROLS}


def _pid_for_xid(xid: int) -> int:
    for _xid, pid, _title in x11_util.client_windows():
        if _xid == xid:
            return pid
    return 0


def render(scan_result: dict) -> str:
    if not scan_result.get("ok"):
        return f"(ui_scan failed: {scan_result.get('reason')})"
    w = scan_result.get("window", {})
    lines = [f"WINDOW: {w.get('title')} (pid {w.get('pid')})",
             f"Controls ({len(scan_result.get('controls', []))}"
             f"{'+ truncated' if scan_result.get('truncated') else ''}):"]
    for c in scan_result.get("controls", []):
        val = f' value="{c["value"]}"' if c.get("value") else ""
        pats = f" [{','.join(c['patterns'])}]" if c.get("patterns") else ""
        dis = "" if c.get("enabled", True) else " (disabled)"
        lines.append(f"[{c['id']}] {c['type']}: {c['name']}{val}{pats}{dis}")
    return "\n".join(lines)


def _control(control_id: int):
    with _lock:
        if not _last_controls:
            raise LookupError("no ui_scan yet — call ui_scan first")
        if not (0 <= int(control_id) < len(_last_controls)):
            raise LookupError(f"control id {control_id} not in the last scan "
                              f"(0..{len(_last_controls) - 1})")
        return _last_controls[int(control_id)]


def last_window_title() -> str:
    with _lock:
        xid = _last_window_xid
        app = _last_app
    title = x11_util.window_title(xid) if xid else ""
    return title[:120] if title else app


def last_window_hwnd() -> int:
    with _lock:
        return int(_last_window_xid or 0)


def describe(control_id: int) -> str:
    try:
        el = _control(control_id)
        role = (el.get_role_name() or "control").lower()
        ctype = _INTERESTING.get(role, "control")
        label = (el.name or "").strip()
        if not label:
            try:
                if el.get_editable_text_iface() is not None:
                    label = "(editor)"
            except Exception:
                pass
        return f"{ctype}: {(label or '?')[:80]}"
    except Exception:
        return f"control #{control_id}"


def invoke(control_id: int) -> str:
    Atspi = _ensure_atspi()
    el = _control(control_id)
    try:
        if el.get_n_actions() > 0:
            el.do_action(0)
            return "invoked"
    except Exception:
        pass
    try:
        ss = el.get_state_set()
        if ss.contains(Atspi.StateType.STATE_CHECKABLE):
            el.do_action(0)
            return "toggled"
    except Exception:
        pass
    raise RuntimeError("control supports no activation pattern; "
                       "use pixel click_at as fallback")


def set_value(control_id: int, text: str) -> str:
    el = _control(control_id)
    try:
        ei = el.get_editable_text_iface()
        if ei is not None:
            ei.set_text_contents(text)
            return "set"
    except Exception:
        pass
    raise RuntimeError("control is not settable via AT-SPI; "
                       "use pixel click_at + type_text as fallback")
