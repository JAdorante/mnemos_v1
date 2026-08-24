"""Promote discovered apps into the user overlay — Console writes, agent proposes.

The agent never authors ~/.quill/apps.json. A human-approved launch_unlisted_app
decision triggers promotion here (from the API layer), hash-audited via packet_id.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from . import app_registry
from . import app_templates
from . import config as cfg


def provenance_label(discovered: dict) -> str:
    """Human-readable path provenance for the approval card."""
    source = (discovered.get("source") or "").strip().lower()
    path = discovered.get("path") or ""
    if source in ("path",):
        if path and os.path.sep in path:
            parent = Path(path).parent.name
            if parent.lower() in ("bin", "sbin", "usr"):
                return "found on your system PATH"
            return f"found at {path}"
        return "found on your system PATH"
    if source in ("app paths", "app_paths"):
        return "found in Windows App Paths (installer registration)"
    if source in ("start menu", "start_menu"):
        return "found in your Start Menu"
    if source in ("desktop_entry", "desktop entry"):
        return "found in your Applications menu"
    return discovered.get("source") or "found on this machine"


def _overlay_path() -> Path:
    return app_registry.overlay_path()


def _load_overlay() -> dict:
    return app_registry._load_json(_overlay_path(), label="apps overlay")


def _save_overlay(data: dict) -> None:
    path = _overlay_path()
    if not app_registry._overlay_is_safe(path, cfg.JAIL_ROOT):
        raise PermissionError("overlay path is inside the jail")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json.tmp",
                               prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def reload_registry() -> None:
    """Re-read overlay and refresh module-level APP_* maps (Console hot reload)."""
    cands, launch_args, caps = app_registry.build_registry(cfg.JAIL_ROOT)
    cfg.APP_CANDIDATES.clear()
    cfg.APP_CANDIDATES.update(cands)
    cfg.APP_LAUNCH_ARGS.clear()
    cfg.APP_LAUNCH_ARGS.update(launch_args)
    cfg.APP_CAPABILITIES.clear()
    cfg.APP_CAPABILITIES.update(caps)


def is_promoted(key: str) -> bool:
    key = (key or "").lower()
    ov = _load_overlay()
    entry = ov.get(key)
    return isinstance(entry, dict) and bool(entry.get("_promoted"))


def promotion_meta(key: str) -> dict:
    key = (key or "").lower()
    ov = _load_overlay()
    entry = ov.get(key) if isinstance(ov.get(key), dict) else {}
    return dict(entry.get("_promoted") or {})


def promote_app(*, key: str, exe_path: str, template_id: str,
                display_name: str = "", platforms: list[str] | None = None,
                packet_id: int | None = None,
                approved_via: str = "button") -> dict:
    """Write a remembered app to the user overlay and reload the registry."""
    key = (key or "").lower().strip()
    if not key:
        return {"ok": False, "error": "empty app key"}
    exe_path = str(exe_path or "").strip()
    if not exe_path:
        return {"ok": False, "error": "empty executable path"}

    tid = (template_id or "text_notes").strip().lower()
    if tid not in app_templates.TEMPLATES:
        tid = "text_notes"
    caps = app_templates.capabilities_for(tid, display_name=display_name or key.title())

    plats = platforms or (["nt"] if os.name == "nt" else ["posix"])
    entry = {
        "platforms": plats,
        "candidates": [exe_path],
        "capabilities": caps,
        "_promoted": {
            "ts": time.time(),
            "template": tid,
            "packet_id": packet_id,
            "approved_via": approved_via,
        },
    }

    ov = _load_overlay()
    ov[key] = entry
    _save_overlay(ov)
    reload_registry()

    from . import access
    # Drop launch-only grant — full registry entry now owns the key.
    ov2 = access._load_overrides()
    if key in ov2 and isinstance(ov2[key], dict):
        ov2[key].pop("granted", None)
        if not ov2[key]:
            ov2.pop(key, None)
        access._save_overrides(ov2)

    return {"ok": True, "key": key, "template": tid,
            "overlay": str(_overlay_path())}


def revoke_promotion(key: str) -> dict:
    """Remove a user-promoted app from the overlay (shipped defaults unaffected)."""
    key = (key or "").lower().strip()
    ov = _load_overlay()
    if key not in ov:
        return {"ok": False, "error": "not in overlay"}
    if key in app_registry._load_json(
            app_registry._DEFAULTS_PATH, label="defaults"):
        # Still in shipped defaults — only remove overlay override slice.
        pass
    ov.pop(key, None)
    _save_overlay(ov)
    reload_registry()
    return {"ok": True, "key": key}


def maybe_promote_from_approval(fields: dict | None, *,
                                packet_id: int | None = None,
                                approved_via: str = "button") -> dict | None:
    """If the human chose Remember, promote after a launch_unlisted_app approve."""
    f = dict(fields or {})
    action = (f.get("action") or "").strip().lower()
    if action != "launch_unlisted_app":
        return None
    if f.get("remember_app") in (False, "false", "0", 0):
        return None
    key = (f.get("app") or "").strip().lower()
    exe = f.get("exe") or ""
    template = f.get("app_template") or app_templates.infer_template(
        key, exe, f.get("args") or [], f.get("discovery_source") or "")
    display = f.get("display_name") or key.title()
    try:
        return promote_app(key=key, exe_path=exe, template_id=template,
                           display_name=display, packet_id=packet_id,
                           approved_via=approved_via)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
