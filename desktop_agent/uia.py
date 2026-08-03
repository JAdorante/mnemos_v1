"""UI Automation actuator — drive a specific app window without the mouse.

The pixel path (pixel.py) hijacks the real cursor and keyboard focus; this path
talks to a window's accessibility tree instead: controls are enumerated like the
browser agent's perception (indexed list, act by id) and driven via UIA patterns
(Invoke / SetValue / Toggle), which need neither the cursor nor foreground focus.
The user keeps working while the agent acts.

Security mirrors the rest of the desktop agent (fail-closed):
  * only windows whose process executable matches an ALLOWLISTED app's
    candidates can be scanned or driven — anything else is refused;
  * scans expose text/state read-only; mutations go through DesktopDriver's
    approval gate before any method here is called.

Built directly on comtypes (already a dependency) — no pywinauto.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from . import config as cfg

_lock = threading.Lock()
_last_controls: list = []          # element registry from the latest scan
_last_window = None                # IUIAutomationElement of the scanned window
_last_app = ""

MAX_CONTROLS = 120
_INTERESTING = {}                  # control-type id -> name (filled lazily)


def available() -> bool:
    if os.name != "nt":
        return False
    try:
        _uia()
        return True
    except Exception:
        return False


def _ensure_com() -> None:
    """Per-thread COM init, tolerant of 'already initialized' in either mode."""
    try:
        import comtypes
        comtypes.CoInitializeEx(getattr(comtypes, "COINIT_APARTMENTTHREADED", 2))
    except Exception:
        pass


_uia_obj = None


def _mod():
    import comtypes.client
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA
    return UIA


def _uia():
    global _uia_obj, _INTERESTING
    _ensure_com()
    if _uia_obj is not None:
        return _uia_obj
    import comtypes.client
    UIA = _mod()
    _uia_obj = comtypes.client.CreateObject(
        UIA.CUIAutomation, interface=UIA.IUIAutomation)
    _INTERESTING = {
        UIA.UIA_ButtonControlTypeId: "button",
        UIA.UIA_CheckBoxControlTypeId: "checkbox",
        UIA.UIA_ComboBoxControlTypeId: "combobox",
        UIA.UIA_EditControlTypeId: "edit",
        UIA.UIA_HyperlinkControlTypeId: "link",
        UIA.UIA_ListItemControlTypeId: "listitem",
        UIA.UIA_MenuItemControlTypeId: "menuitem",
        UIA.UIA_RadioButtonControlTypeId: "radio",
        UIA.UIA_TabItemControlTypeId: "tab",
        UIA.UIA_TreeItemControlTypeId: "treeitem",
        UIA.UIA_DocumentControlTypeId: "document",
        UIA.UIA_SplitButtonControlTypeId: "splitbutton",
    }
    return _uia_obj


# --- window resolution (allowlist-scoped) -----------------------------------
def _exe_for_pid(pid: int) -> str:
    """Full image path of a process (ctypes; empty string on failure)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    # HANDLE is pointer-sized: without explicit types ctypes truncates to int.
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        k32.CloseHandle(h)


def _app_basenames(app: str) -> set[str]:
    """Executable basenames an allowlisted app key may resolve to."""
    names: set[str] = set()
    for c in cfg.APP_CANDIDATES.get(app, []):
        base = Path(c).name.lower()
        if base and not base.endswith(".exe"):
            base += ".exe"
        if base:
            names.add(base)
    resolved = cfg.resolve_app_path(app)
    if resolved:
        names.add(Path(resolved).name.lower())
    return names


def find_windows(app: str) -> list:
    """Top-level windows belonging to the allowlisted app, or [] (never raises
    for unknown apps — the caller refuses first)."""
    UIA = _mod()
    auto = _uia()
    want = _app_basenames(app)
    if not want:
        return []
    root = auto.GetRootElement()
    cond = auto.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_WindowControlTypeId)
    wins = root.FindAll(UIA.TreeScope_Children, cond)
    out = []
    for i in range(wins.Length):
        w = wins.GetElement(i)
        try:
            exe = Path(_exe_for_pid(w.CurrentProcessId)).name.lower()
            if exe in want:
                out.append(w)
        except Exception:
            continue
    return out


# --- scanning ----------------------------------------------------------------
def _pattern(el, pattern_id, iface):
    try:
        p = el.GetCurrentPattern(pattern_id)
        return p.QueryInterface(iface) if p else None
    except Exception:
        return None


def _value_of(el, UIA) -> str:
    vp = _pattern(el, UIA.UIA_ValuePatternId, UIA.IUIAutomationValuePattern)
    if vp is not None:
        try:
            return (vp.CurrentValue or "")[:120]
        except Exception:
            pass
    tp = _pattern(el, UIA.UIA_TextPatternId, UIA.IUIAutomationTextPattern)
    if tp is not None:
        try:
            return (tp.DocumentRange.GetText(200) or "")[:120]
        except Exception:
            pass
    return ""


def scan(app: str, title_hint: str = "") -> dict:
    """Enumerate the app window's interactive controls into an indexed registry.

    Returns {ok, window:{title,pid}, controls:[{id,type,name,automation_id,
    enabled,value?,patterns}]} — act on a control with invoke()/set_value()
    using its id. A new scan replaces the registry (ids are per-scan)."""
    global _last_controls, _last_window, _last_app
    UIA = _mod()
    wins = find_windows(app)
    if not wins:
        return {"ok": False, "reason": f"no open window for app '{app}'"}
    win = wins[0]
    if title_hint:
        for w in wins:
            try:
                if title_hint.lower() in (w.CurrentName or "").lower():
                    win = w
                    break
            except Exception:
                continue

    auto = _uia()
    cond = auto.CreateTrueCondition()
    els = win.FindAll(UIA.TreeScope_Descendants, cond)
    controls, registry = [], []
    for i in range(els.Length):
        if len(registry) >= MAX_CONTROLS:
            break
        el = els.GetElement(i)
        try:
            ctype = el.CurrentControlType
            if ctype not in _INTERESTING:
                continue
            if el.CurrentIsOffscreen:
                continue
            name = (el.CurrentName or "").strip()[:120]
            auto_id = (el.CurrentAutomationId or "")[:80]
            if not name and not auto_id:
                continue
            patterns = []
            if _pattern(el, UIA.UIA_InvokePatternId,
                        UIA.IUIAutomationInvokePattern) is not None:
                patterns.append("invoke")
            vp = _pattern(el, UIA.UIA_ValuePatternId,
                          UIA.IUIAutomationValuePattern)
            if vp is not None:
                try:
                    if not vp.CurrentIsReadOnly:
                        patterns.append("set_text")
                except Exception:
                    pass
            if _pattern(el, UIA.UIA_TogglePatternId,
                        UIA.IUIAutomationTogglePattern) is not None:
                patterns.append("toggle")
            if _pattern(el, UIA.UIA_SelectionItemPatternId,
                        UIA.IUIAutomationSelectionItemPattern) is not None:
                patterns.append("select")
            item = {
                "id": len(registry),
                "type": _INTERESTING[ctype],
                "name": name,
                "automation_id": auto_id,
                "enabled": bool(el.CurrentIsEnabled),
                "patterns": patterns,
            }
            if item["type"] in ("edit", "document", "combobox"):
                item["value"] = _value_of(el, UIA)
            controls.append(item)
            registry.append(el)
        except Exception:
            continue
    with _lock:
        _last_controls = registry
        _last_window = win
        _last_app = app
    try:
        title = win.CurrentName or ""
    except Exception:
        title = ""
    return {"ok": True,
            "window": {"title": title[:160], "pid": int(win.CurrentProcessId)},
            "controls": controls,
            "truncated": len(registry) >= MAX_CONTROLS}


def render(scan_result: dict) -> str:
    """Text observation for the executor, mirroring browser perception."""
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
        aid = f" #{c['automation_id']}" if c.get("automation_id") else ""
        lines.append(f"[{c['id']}] {c['type']}: {c['name']}{aid}{val}{pats}{dis}")
    return "\n".join(lines)


# --- acting ------------------------------------------------------------------
def _control(control_id: int):
    with _lock:
        if not _last_controls:
            raise LookupError("no ui_scan yet — call ui_scan first")
        if not (0 <= int(control_id) < len(_last_controls)):
            raise LookupError(f"control id {control_id} not in the last scan "
                              f"(0..{len(_last_controls) - 1})")
        return _last_controls[int(control_id)]


def last_window_title() -> str:
    """Title of the window the last scan targeted (for approval prompts)."""
    with _lock:
        win = _last_window
    if win is None:
        return ""
    try:
        return (win.CurrentName or "")[:120]
    except Exception:
        return _last_app


def last_window_hwnd() -> int:
    """Native handle of the last-scanned window (0 if unavailable) — lets the
    ghost-desktop path capture frames of the window the agent is driving."""
    with _lock:
        win = _last_window
    if win is None:
        return 0
    try:
        return int(win.CurrentNativeWindowHandle or 0)
    except Exception:
        return 0


def describe(control_id: int) -> str:
    """Short human label for approval prompts ('button: Don't Save')."""
    try:
        el = _control(control_id)
        UIA = _mod()
        t = _INTERESTING.get(el.CurrentControlType, "control")
        return f"{t}: {(el.CurrentName or el.CurrentAutomationId or '?')[:80]}"
    except Exception:
        return f"control #{control_id}"


def invoke(control_id: int) -> str:
    """Press/activate a control (Invoke > Toggle > SelectionItem > Legacy)."""
    UIA = _mod()
    el = _control(control_id)
    ip = _pattern(el, UIA.UIA_InvokePatternId, UIA.IUIAutomationInvokePattern)
    if ip is not None:
        ip.Invoke()
        return "invoked"
    tp = _pattern(el, UIA.UIA_TogglePatternId, UIA.IUIAutomationTogglePattern)
    if tp is not None:
        tp.Toggle()
        return "toggled"
    sp = _pattern(el, UIA.UIA_SelectionItemPatternId,
                  UIA.IUIAutomationSelectionItemPattern)
    if sp is not None:
        sp.Select()
        return "selected"
    lp = _pattern(el, UIA.UIA_LegacyIAccessiblePatternId,
                  UIA.IUIAutomationLegacyIAccessiblePattern)
    if lp is not None:
        lp.DoDefaultAction()
        return "default action"
    raise RuntimeError("control supports no activation pattern; "
                       "use pixel click_at as fallback")


def set_value(control_id: int, text: str) -> str:
    """Set an editable control's text (no focus/keyboard needed)."""
    UIA = _mod()
    el = _control(control_id)
    vp = _pattern(el, UIA.UIA_ValuePatternId, UIA.IUIAutomationValuePattern)
    if vp is not None:
        try:
            if not vp.CurrentIsReadOnly:
                vp.SetValue(text)
                return "set"
        except Exception:
            pass
    lp = _pattern(el, UIA.UIA_LegacyIAccessiblePatternId,
                  UIA.IUIAutomationLegacyIAccessiblePattern)
    if lp is not None:
        lp.SetValue(text)
        return "set (legacy)"
    raise RuntimeError("control is not settable via UIA; "
                       "use pixel click_at + type_text as fallback")
