"""Runtime app discovery — criteria-based vetting instead of a closed list.

The registry (apps.default.json + overlay) stays the fast path: known apps keep
their capability contracts. This module handles everything else. When the model
names an app the registry doesn't know, we resolve it from what is actually
INSTALLED on this machine and judge it against policy CRITERIA, rather than
refusing because nobody enumerated it in advance.

The criteria (every one fails closed):
  1. The model supplies a bare NAME, never a path — resolution goes through OS
     registration channels only (PATH, the App Paths registry, the Start Menu),
     so a launchable app is one an installer registered, not an arbitrary file.
  2. The executable is not a shell, script host, interpreter, installer, or
     admin tool (DISCOVERY_DENY_BASENAMES) — code execution belongs to
     run_command with its own gates, never to launch_app.
  3. The executable does not live inside the jail (the agent must never launch
     a binary it could have authored) nor in temp/downloads/recycle-bin style
     scratch locations where unvetted binaries land.
  4. A discovered app gets the LOCKED capability contract: launch-only, no
     file/folder targets, until a human widens it via the overlay.
  5. The FIRST use of each discovered app always asks the human (the driver
     gates it under a verb no autonomy level auto-approves); an approved launch
     is remembered as a grant, after which normal autonomy rules apply.

Pure decisions live in vet_path(); discover_app() only reads the OS.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import app_registry
from . import config as cfg
from . import guards


def _stem(path: Path) -> str:
    """Lowercased basename without a launcher suffix ("Spotify.exe" -> "spotify")."""
    name = path.name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def vet_path(path: str | Path, jail_root: Path | None = None) -> str | None:
    """Judge a resolved executable against the discovery criteria. Pure policy.

    Returns a refusal reason, or None if the executable may launch. Anything
    that can't be resolved or classified is refused, never allowed through.
    """
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return "unresolvable executable path"
    if not resolved.is_file():
        return "executable does not exist on disk"
    if os.name == "nt" and resolved.suffix.lower() != ".exe":
        return (f"not a directly launchable executable "
                f"({resolved.suffix or 'no extension'})")
    stem = _stem(resolved)
    if stem in cfg.DISCOVERY_DENY_BASENAMES:
        return (f"{stem!r} is a shell/script host/interpreter/admin tool — "
                "code runs through run_command, never launch_app")
    if guards.within_jail(resolved, jail_root or cfg.JAIL_ROOT):
        return "executable lives inside the jail (agent-authored binaries never launch)"
    low = str(resolved).lower().replace("/", "\\")
    for marker in cfg.DISCOVERY_UNTRUSTED_MARKERS:
        if marker in low:
            return f"executable lives in an untrusted location (matches {marker!r})"
    return None


def discover_app(name: str,
                 jail_root: Path | None = None) -> tuple[dict | None, str | None]:
    """Resolve an app NAME to a vetted installed executable on this machine.

    Probes the OS registration channels in order — PATH, the Windows App Paths
    registry, then a Start-Menu shortcut scan — and vets the first hit that
    clears vet_path(). Returns (info, reason):
        ({key, display_name, path, source}, None)  vetted hit
        (None, reason)                             found but refused by policy,
                                                   or the name itself is invalid
        (None, None)                               nothing installed by that name
    """
    key = (name or "").strip().lower()
    if not key:
        return None, "empty app name"
    if any(ch in key for ch in "/\\:") or ".." in key:
        return None, "app must be a bare name, not a path"
    jail = jail_root or cfg.JAIL_ROOT

    channels = (
        ("PATH", lambda: shutil.which(key)),
        ("App Paths", lambda: app_registry.resolve_from_app_paths([key])),
        ("Start Menu", lambda: app_registry.resolve_from_start_menu(key, [key])),
    )
    last_reason: str | None = None
    for source, probe in channels:
        try:
            hit = probe()
        except Exception:
            hit = None
        if not hit:
            continue
        reason = vet_path(hit, jail)
        if reason is None:
            return {
                "key": key,
                "display_name": key,
                "path": str(Path(hit).resolve()),
                "source": source,
            }, None
        last_reason = f"{reason} (found via {source}: {hit})"
    return None, last_reason
