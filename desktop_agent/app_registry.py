"""Load the launchable-app registry from data + an optional user overlay.

The allowlist of desktop apps is DATA, not code (the general-code invariant): the
shipped `data/apps.default.json` expresses every machine-specific install location
as an env-var TOKEN, so the same file resolves on any machine. A user can add apps
or repoint an app-key at a different install through an overlay JSON — no code edit.

    defaults (apps.default.json)
        deep-merged with
    overlay (QUILL_DESKTOP_APPS, default ~/.quill/apps.json)
        -> expand %VAR% / ${VAR} / ~ in every candidate
        -> drop apps whose `platforms` excludes this OS
        -> rebuild APP_CANDIDATES / APP_LAUNCH_ARGS / APP_CAPABILITIES

Security (this is a fail-closed allowlist):
  * the overlay MUST live OUTSIDE the jail — the agent must never author its own
    allowlist. An overlay under JAIL_ROOT is rejected (warned + ignored).
  * a malformed/unreadable overlay OR default fails SAFE (shipped defaults, or an
    empty registry) — it never crashes and never widens reach.
  * overlay `candidates` REPLACE per app-key (predictable), and every app-key is
    guaranteed a capabilities entry — a new app added with no capabilities gets the
    locked-down default (launch-only, opens nothing).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "data"
_DEFAULTS_PATH = _DATA_DIR / "apps.default.json"

# Locked-down capability contract for an app-key with no explicit capabilities —
# launch only, opens nothing. A missing capability fails safe (closed), never open.
LOCKED_CAPS: dict = {
    "display_name": "", "launch": True, "open_jailed_files": [],
    "opens_dirs": False, "pixel_ui": "off", "shell": False, "network": False,
    "risk": "unknown", "demo_safe": False, "notes": "",
}

_PCT_VAR = re.compile(r"%([^%]+)%")


def _expand(value: str) -> str:
    """Expand %VAR% (all platforms — os.path.expandvars only does this on Windows),
    then ${VAR}/$VAR, then a leading ~. An undefined %VAR% expands to empty so the
    candidate simply fails to resolve (skipped) rather than leaking a literal."""
    def _sub(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")
    out = _PCT_VAR.sub(_sub, value or "")
    out = os.path.expandvars(out)          # ${VAR} / $VAR
    out = os.path.expanduser(out)          # ~
    return out


def _load_json(path: Path, *, label: str) -> dict:
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[app_registry] {label} unreadable ({exc}); ignoring it.")
        return {}


def overlay_path() -> Path:
    raw = os.environ.get("QUILL_DESKTOP_APPS") or str(Path.home() / ".quill" / "apps.json")
    return Path(raw).expanduser()


def _overlay_is_safe(overlay: Path, jail_root: Path | None) -> bool:
    """Reject an overlay that lives inside the jail — the agent must not be able to
    author the very allowlist that constrains it."""
    if jail_root is None:
        return True
    try:
        overlay.resolve().relative_to(Path(jail_root).resolve())
        print(f"[app_registry] overlay {overlay} is inside the jail; refusing it "
              "(the agent may not author its own allowlist).")
        return False
    except Exception:
        return True  # not under the jail -> allowed


def _merge(defaults: dict, overlay: dict) -> dict:
    """Deep-merge overlay onto defaults. Per app-key: `candidates`, `launch_args`,
    and `platforms` REPLACE (predictable); `capabilities` fields shallow-merge."""
    merged: dict = {}
    for key in set(defaults) | set(overlay):
        if str(key).startswith("_"):
            continue
        d = defaults.get(key) or {}
        o = overlay.get(key) or {}
        if not isinstance(d, dict) or not isinstance(o, dict):
            continue
        entry = dict(d)
        for field in ("platforms", "candidates", "launch_args"):
            if field in o:
                entry[field] = o[field]
        caps = dict(d.get("capabilities") or {})
        caps.update(o.get("capabilities") or {})
        entry["capabilities"] = caps
        merged[key] = entry
    return merged


def _platform_ok(entry: dict) -> bool:
    plats = entry.get("platforms")
    if not plats:                       # unspecified -> available everywhere
        return True
    return os.name in plats


def build_registry(jail_root: Path | None = None):
    """Return (APP_CANDIDATES, APP_LAUNCH_ARGS, APP_CAPABILITIES) for THIS machine.

    Kept identical in shape to the old module constants so every downstream reader
    (resolve_app_path / capabilities / describe_apps / the parity test) is unchanged.
    """
    defaults = _load_json(_DEFAULTS_PATH, label="apps.default.json")
    overlay = {}
    try:
        ov = overlay_path()
        if _overlay_is_safe(ov, jail_root):
            overlay = _load_json(ov, label=f"overlay {ov}")
    except Exception as exc:                # can't even locate the overlay -> skip it
        print(f"[app_registry] overlay lookup skipped ({exc}).")
    merged = _merge(defaults, overlay)

    candidates: dict[str, list[str]] = {}
    launch_args: dict[str, list[str]] = {}
    capabilities: dict[str, dict] = {}
    for key, entry in merged.items():
        key = str(key).lower()
        if not _platform_ok(entry):
            continue                    # e.g. drop phonelink off-Windows
        cands = [_expand(str(c)) for c in (entry.get("candidates") or []) if str(c).strip()]
        candidates[key] = cands
        la = entry.get("launch_args") or []
        if la:
            launch_args[key] = [_expand(str(a)) for a in la]
        caps = dict(LOCKED_CAPS)
        caps.update(entry.get("capabilities") or {})
        caps.setdefault("display_name", key)
        if not caps.get("display_name"):
            caps["display_name"] = key
        capabilities[key] = caps
    return candidates, launch_args, capabilities


# --- extra resolution probes (used by config.resolve_app_path) --------------
def _basenames_for(candidates: list[str]) -> list[str]:
    """The .exe basenames to look up in the Windows App Paths registry — derived
    from the app's own candidates (bare names get an .exe suffix)."""
    out: list[str] = []
    for c in candidates:
        base = os.path.basename(c) or c
        if not base:
            continue
        if not base.lower().endswith(".exe"):
            base = base + ".exe"
        if base.lower() not in {b.lower() for b in out}:
            out.append(base)
    return out


def resolve_from_app_paths(candidates: list[str]) -> str | None:
    """Windows App Paths registry lookup (read-only). Returns a real exe path or
    None. No-op / safe on non-Windows or without winreg."""
    if os.name != "nt":
        return None
    try:
        import winreg  # type: ignore
    except Exception:
        return None
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for base in _basenames_for(candidates):
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, sub + "\\" + base) as k:
                    val, _ = winreg.QueryValueEx(k, None)   # default value = full path
                    val = _expand(str(val)).strip('"')
                    if val and Path(val).is_file():
                        return val
            except FileNotFoundError:
                continue
            except Exception:
                continue
    return None


def resolve_from_start_menu(display_name: str, candidates: list[str]) -> str | None:
    """Best-effort Start-Menu .lnk scan (optional). Resolves a matching shortcut's
    target if win32com is available; otherwise a no-op (returns None). Never raises."""
    if os.name != "nt" or not display_name:
        return None
    roots = [os.environ.get("APPDATA", ""), os.environ.get("PROGRAMDATA", "")]
    dirs = [Path(r) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            for r in roots if r]
    want = display_name.lower()
    lnks: list[Path] = []
    for d in dirs:
        try:
            if d.is_dir():
                lnks += [p for p in d.rglob("*.lnk") if want in p.stem.lower()]
        except Exception:
            continue
    if not lnks:
        return None
    try:
        import win32com.client  # type: ignore
    except Exception:
        return None             # can't resolve the target without pywin32 -> skip
    shell = win32com.client.Dispatch("WScript.Shell")
    for lnk in lnks:
        try:
            target = shell.CreateShortcut(str(lnk)).Targetpath
            if target and Path(target).is_file():
                return target
        except Exception:
            continue
    return None
