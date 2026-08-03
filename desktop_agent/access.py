"""Desktop Access read-model (strategic doc #5) — make the allowlist visible.

Everything the agent may do on the desktop is enumerated in config (the app
allowlist, the capability registry, the autonomy ceiling). This module turns
that plus the live environment and the audit trail into one inspectable state
for a "Desktop Access" panel, and adds the one runtime override the panel needs:
a per-app disable switch that the driver enforces.

Read-model over config + the audit JSONL; the only writable state is the disable
override, persisted next to the audit log so it survives a restart. It never
widens what the driver allows — disabling an app only ever refuses a launch.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from . import config as cfg


def _overrides_path() -> Path:
    # Resolved at call time so tests that repoint SESSIONS_ROOT are honoured.
    return cfg.SESSIONS_ROOT / "desktop_overrides.json"


def _load_overrides() -> dict:
    try:
        return json.loads(_overrides_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_overrides(data: dict) -> None:
    path = _overrides_path()
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


def disabled_apps() -> set[str]:
    """The set of app keys currently disabled via the panel."""
    ov = _load_overrides()
    return {k for k, v in ov.items() if isinstance(v, dict) and v.get("disabled")}


def app_disabled(key: str) -> bool:
    return (key or "").lower() in disabled_apps()


def set_app_disabled(key: str, disabled: bool) -> bool:
    """Enable/disable an app. Registry keys always qualify; with discovery on,
    any bare name may be disabled ahead of time (a standing "never this one")."""
    key = (key or "").lower()
    if not key:
        return False
    if key not in cfg.APP_CANDIDATES and not cfg.APP_DISCOVERY \
            and key not in granted_apps():
        return False
    ov = _load_overrides()
    ov.setdefault(key, {})["disabled"] = bool(disabled)
    _save_overrides(ov)
    return True


# --- discovered-app grants --------------------------------------------------
# A grant records that a human approved the first launch of an app the registry
# doesn't know (found by runtime discovery). It never widens capabilities —
# discovered apps stay launch-only — it only lets later launches follow normal
# autonomy rules instead of re-asking every time. Stored in the same overrides
# file (outside the jail), so the agent cannot author its own grants via
# write_file any more than it can author the allowlist.

def granted_apps() -> dict[str, dict]:
    """Map of discovered app keys a human has approved -> grant record."""
    ov = _load_overrides()
    return {k: v["granted"] for k, v in ov.items()
            if isinstance(v, dict) and isinstance(v.get("granted"), dict)}


def app_granted(key: str) -> bool:
    return (key or "").lower() in granted_apps()


def grant_app(key: str, path: str, source: str = "") -> None:
    """Record a human-approved first launch of a discovered app."""
    key = (key or "").lower()
    if not key:
        return
    ov = _load_overrides()
    ov.setdefault(key, {})["granted"] = {
        "path": str(path), "source": source, "ts": time.time()}
    _save_overrides(ov)


def _ui_control(key: str) -> str:
    """How pixel UI applies to this app: on / off / n-a (not UI-driven)."""
    if cfg.capabilities(key).get("pixel_ui") != "approval_required":
        return "n/a"
    return "on" if cfg.PIXEL_UI else "off"


def _app_row(key: str, disabled: set[str]) -> dict:
    caps = cfg.capabilities(key)
    path = cfg.resolve_app_path(key)
    installed = path is not None
    is_disabled = key in disabled
    return {
        "key": key,
        "display_name": cfg.app_display_name(key),
        "installed": installed,
        "resolved_path": path,
        "disabled": is_disabled,
        "launch_allowed": installed and not is_disabled,
        "opens_dirs": cfg.app_opens_dirs(key),
        "opens_files": sorted(cfg.openable_extensions(key)),
        "ui_control": _ui_control(key),
        "risk": caps.get("risk", "unknown"),
        "special": caps.get("pixel_ui") == "special",
        "discovered": False,
        "notes": caps.get("notes", ""),
    }


def _granted_row(key: str, grant: dict, disabled: set[str]) -> dict:
    """A Desktop Access row for a runtime-discovered app a human has granted."""
    path = grant.get("path") or None
    installed = bool(path) and Path(path).is_file()
    is_disabled = key in disabled
    return {
        "key": key,
        "display_name": key,
        "installed": installed,
        "resolved_path": path,
        "disabled": is_disabled,
        "launch_allowed": installed and not is_disabled,
        "opens_dirs": False,
        "opens_files": [],
        "ui_control": "n/a",
        "risk": "unknown",
        "special": False,
        "discovered": True,
        "notes": (f"discovered at runtime via {grant.get('source') or '?'}; "
                  "launch-only"),
    }


def _environment() -> dict:
    verbs = ["launch_app", "make_dir", "write_file", "run_command",
             "click_at", "type_text", "press_key"]
    auto_verbs = [v for v in verbs if cfg.desktop_autoapprove(v)]
    return {
        "jail": str(cfg.JAIL_ROOT),
        "jail_exists": cfg.JAIL_ROOT.exists(),
        "pixel_ui": bool(cfg.PIXEL_UI),
        "pixel_vision": bool(cfg.PIXEL_VISION),
        "approval_required": bool(cfg.REQUIRE_APPROVAL),
        "autonomy_desktop": cfg.AGENT_AUTONOMY_DESKTOP,
        "autonomy_shell": bool(cfg.AGENT_AUTONOMY_SHELL),
        "auto_verbs": auto_verbs,
        "gated_verbs": [v for v in verbs if v not in auto_verbs],
        "max_actions": int(cfg.MAX_ACTIONS_PER_TASK),
    }


def desktop_access_state() -> dict:
    """The full Desktop Access snapshot: environment + one row per app."""
    disabled = disabled_apps()
    apps = [_app_row(k, disabled) for k in sorted(cfg.APP_CANDIDATES)]
    apps += [_granted_row(k, g, disabled)
             for k, g in sorted(granted_apps().items())
             if k not in cfg.APP_CANDIDATES]
    return {"environment": _environment(), "apps": apps}


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return ""


def recent_actions(limit: int = 10) -> list[dict]:
    """The last `limit` audited desktop actions, newest first."""
    path = cfg.SESSIONS_ROOT / "desktop_audit.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        target = rec.get("app") or rec.get("path") or (
            " ".join(rec.get("argv")) if isinstance(rec.get("argv"), list) else "")
        out.append({
            "ts": rec.get("ts"),
            "when": _fmt_ts(rec.get("ts")),
            "action": rec.get("action") or rec.get("outcome") or "?",
            "outcome": rec.get("outcome", ""),
            "target": target,
            "detail": rec.get("detail", ""),
        })
        if len(out) >= max(1, int(limit)):
            break
    return out
