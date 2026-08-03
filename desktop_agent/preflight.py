"""Deterministic preflight — what is possible on THIS machine, computed BEFORE
the desktop loop plans anything.

The planner should not discover that UI automation is off by trying `click_at`
and getting refused, nor propose launching an app that isn't installed. Preflight
answers the up-front questions — installed? enabled? in-jail? approved?
affordable? — from the capability registry ([[quill-desktop-agent]] #2) plus the
live environment, so the model plans against reality instead of guessing.

Pure reporting over config + a filesystem existence probe: it never launches,
mutates, or spawns. The driver still enforces every one of these facts at
execution time; preflight just lets the planner avoid dead ends.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import access
from . import config as cfg

# Desktop verbs that never depend on pixel UI.
_FILE_ACTIONS = ["make_dir", "write_file", "launch_app", "run_command", "list_dir"]
_UI_ACTIONS = ["screenshot", "click_at", "type_text", "press_key"]
_META_ACTIONS = ["ask_human", "done"]

# High-precision goal phrases -> app key. The resolver decides which app a goal
# refers to; the model only suggests. (A fuller alias table is roadmap #10.)
_ALIASES = {
    "fl studio": "flstudio", "flstudio": "flstudio", "fruity loops": "flstudio",
    "cursor": "cursor",
    "vs code": "code", "vscode": "code", "visual studio code": "code",
    "notepad": "notepad",
    "file explorer": "explorer", "windows explorer": "explorer",
    "google chrome": "chrome", "chrome": "chrome",
    "windows terminal": "terminal",
    "phone link": "phonelink", "phonelink": "phonelink",
}

# Bare keys too generic to match as a lone word (they'd fire on unrelated text);
# these are still reachable via their display name or an explicit alias above.
_GENERIC_BARE = {"code", "terminal", "explorer"}


def detect_apps(goal: str) -> list[str]:
    """App keys referenced by the goal text, resolved deterministically.

    Word-boundary matching so "encode a video" does not match VS Code. Longest
    phrase wins ("fl studio" over "fl"). Best-effort hint, not authority.
    """
    text = (goal or "").lower()
    if not text:
        return []
    phrases = dict(_ALIASES)
    for key in cfg.APP_CANDIDATES:
        phrases.setdefault(cfg.app_display_name(key).lower(), key)
        if key not in _GENERIC_BARE:
            phrases.setdefault(key, key)
    found: list[str] = []
    for phrase in sorted(phrases, key=len, reverse=True):
        if phrase and re.search(rf"\b{re.escape(phrase)}\b", text):
            key = phrases[phrase]
            if key not in found:
                found.append(key)
    return found


def _app_snapshot(key: str, disabled: set[str]) -> dict:
    """Availability + capability of one app key (no side effects)."""
    path = cfg.resolve_app_path(key)
    is_disabled = key in disabled
    return {
        "installed": path is not None,
        "resolved_path": path,
        "disabled": is_disabled,
        "launch_allowed": path is not None and not is_disabled,
        "opens_dirs": cfg.app_opens_dirs(key),
        "opens_files": sorted(cfg.openable_extensions(key)),
        "risk": cfg.capabilities(key).get("risk", "unknown"),
    }


def _focus(fkey: str, snap: dict, apps: dict, pixel: bool) -> dict:
    """The doc's single-app result: can this specific app do the job here?"""
    can_launch = snap.get("launch_allowed", snap["installed"])
    can_open = cfg.app_opens_dirs(fkey) or bool(cfg.openable_extensions(fkey))
    recoveries: list[str] = []
    if not can_launch:
        usable = [k for k, v in apps.items() if v.get("launch_allowed")]
        if snap.get("disabled"):
            recoveries.append(f"Enable {cfg.app_display_name(fkey)} in Desktop Access")
        else:
            recoveries.append(f"Install {cfg.app_display_name(fkey)}")
            recoveries.append(f"Add {fkey!r} to APP_CANDIDATES if it lives elsewhere")
        if usable:
            recoveries.append("Use an available app: " + ", ".join(usable))
    return {
        "app": fkey,
        "display_name": cfg.app_display_name(fkey),
        "can_launch": can_launch,
        "resolved_path": snap["resolved_path"],
        "can_open_project": can_open,
        "can_click_type": pixel,
        "notes": cfg.capabilities(fkey).get("notes", ""),
        "recoveries": recoveries,
    }


def preflight(goal: str = "", app: str | None = None, *,
              autonomous: bool | None = None,
              jail: Path | str | None = None,
              actions_used: int = 0) -> dict:
    """Compute the deterministic preflight for a desktop goal.

    `autonomous` reflects the actual run mode (auto-approve vs gated); default
    derives from cfg.REQUIRE_APPROVAL. `jail`/`actions_used` let the caller pass
    the live driver's sandbox and budget so the numbers match the real loop.
    """
    if autonomous is None:
        autonomous = not cfg.REQUIRE_APPROVAL
    jail_path = Path(jail).resolve() if jail else cfg.JAIL_ROOT
    pixel = bool(cfg.PIXEL_UI)
    vision = bool(cfg.PIXEL_VISION)
    max_actions = int(cfg.MAX_ACTIONS_PER_TASK)
    remaining = max(0, max_actions - int(actions_used or 0))

    disabled = access.disabled_apps()
    apps = {k: _app_snapshot(k, disabled) for k in sorted(cfg.APP_CANDIDATES)}

    blocked: list[dict] = []
    allowed = list(_FILE_ACTIONS)
    if pixel:
        allowed += _UI_ACTIONS
    else:
        blocked += [{"action": a, "reason": "pixel UI disabled (QUILL_DESKTOP_UI=0)"}
                    for a in _UI_ACTIONS]
    allowed += _META_ACTIONS
    if remaining == 0:
        blocked.append({"action": "*", "reason":
                        "action budget exhausted; call done or new_task"})

    # Granular autonomy (#4): in an autonomous run, only some verbs auto-approve;
    # the rest still pause for the human. Report the split so the planner knows
    # which actions will stop for approval even in autonomous mode.
    autonomy = None
    if autonomous:
        _verbs = ["launch_app", "make_dir", "write_file", "run_command",
                  "click_at", "type_text", "press_key"]
        auto_verbs = [v for v in _verbs if cfg.desktop_autoapprove(v)]
        autonomy = {
            "level": cfg.AGENT_AUTONOMY_DESKTOP,
            "shell": cfg.AGENT_AUTONOMY_SHELL,
            "auto_verbs": auto_verbs,
            "gated_verbs": [v for v in _verbs if v not in auto_verbs],
        }

    targets = detect_apps(goal)
    if app:
        targets = [app] + [t for t in targets if t != app]

    focus = None
    fkey = app or (targets[0] if targets else None)
    if fkey:
        snap = apps.get(fkey) or _app_snapshot(fkey, disabled)
        focus = _focus(fkey, snap, apps, pixel)

    return {
        "surface": "desktop",
        "goal": goal,
        "jail": str(jail_path),
        "jail_exists": jail_path.exists(),
        "approval_required": not autonomous,
        "autonomy": autonomy,
        "pixel_ui_enabled": pixel,
        "pixel_vision": vision,
        "budget": {"max": max_actions, "remaining": remaining},
        "targets": targets,
        "focus": focus,
        "apps": apps,
        "allowed_next_actions": allowed,
        "blocked_actions": blocked,
    }


def summary_line(pf: dict) -> str:
    """One-line log summary of a preflight result."""
    f = pf.get("focus")
    if f:
        tgt = f"target={f['app']}({'launchable' if f['can_launch'] else 'NOT-installed'})"
    else:
        tgt = "no target detected"
    return (f"preflight: {tgt}, pixel_ui={'on' if pf['pixel_ui_enabled'] else 'off'}, "
            f"approval={'on' if pf['approval_required'] else 'off'}, "
            f"budget={pf['budget']['remaining']}/{pf['budget']['max']}")


def format_preflight(pf: dict) -> str:
    """Compact planner-facing block: what's possible right now, and what isn't."""
    L = ["PREFLIGHT (what's possible on this machine right now - plan within it):"]
    L.append(f"- sandbox: {pf['jail']} "
             f"({'exists' if pf['jail_exists'] else 'will be created on first write'})")
    L.append("- approval: " + ("required for each mutating action"
             if pf["approval_required"] else "autonomous run (see autonomy line)"))
    a = pf.get("autonomy")
    if a:
        L.append(f"- autonomy (desktop={a['level']}, shell={'on' if a['shell'] else 'off'}): "
                 f"auto-run = {', '.join(a['auto_verbs']) or 'none'}; "
                 f"still needs approval = {', '.join(a['gated_verbs']) or 'none'}")
    ui = "ON" if pf["pixel_ui_enabled"] else "OFF"
    if pf["pixel_ui_enabled"] and not pf.get("pixel_vision"):
        ui += " but vision OFF (screenshots not shown — avoid blind clicking)"
    L.append(f"- in-app click/type (pixel UI): {ui}")
    L.append(f"- action budget: {pf['budget']['remaining']}/{pf['budget']['max']} remaining")

    f = pf.get("focus")
    if f:
        L.append(f"- target: {f['app']} ({f['display_name']}): "
                 f"can_launch={f['can_launch']}, can_open_project={f['can_open_project']}, "
                 f"can_click_type={f['can_click_type']}")
        if not f["can_launch"] and f["recoveries"]:
            L.append("  NOT launchable → " + "; ".join(f["recoveries"]))
        elif f["resolved_path"]:
            L.append(f"  resolves to: {f['resolved_path']}")

    launchable = [k for k, v in pf["apps"].items() if v.get("launch_allowed")]
    missing = [k for k, v in pf["apps"].items() if not v["installed"]]
    disabled = [k for k, v in pf["apps"].items() if v.get("disabled")]
    L.append("- launchable apps: " + (", ".join(launchable) or "none"))
    if missing:
        L.append("- NOT installed (do not attempt to launch): " + ", ".join(missing))
    if disabled:
        L.append("- disabled in Desktop Access (do not attempt): " + ", ".join(disabled))
    if pf["blocked_actions"]:
        L.append("- unavailable actions: " + ", ".join(
            f"{b['action']} ({b['reason']})" for b in pf["blocked_actions"]))
    L.append("- usable actions: " + ", ".join(pf["allowed_next_actions"]))
    return "\n".join(L)
