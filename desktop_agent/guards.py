"""Pure guard logic — the decisions, with no side effects, so they're testable.

Every function here answers "is this allowed, and at what risk tier?" without
touching the filesystem or spawning anything. The driver executes; the guards
decide. Keeping them pure means the security-critical logic can be unit-tested
exhaustively in isolation.
"""
from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path

from . import config as cfg


class Tier(str, Enum):
    READ_ONLY = "read_only"   # no prompt
    MUTATING = "mutating"     # human approval required
    BLOCKED = "blocked"       # refused outright, no prompt can unlock


# --- path jail -------------------------------------------------------------
# POSIX treats "\" and drive letters as ordinary filename characters, so a
# Windows-style escape ("..\\..\\x", "C:\\Users\\x", "\\\\server\\share")
# would resolve as ONE relative component *inside* the jail. The jail
# invariant must not be platform-conditional: on non-Windows hosts, deny
# drive/UNC prefixes outright and treat backslashes as separators.
_WIN_DRIVE_OR_UNC = re.compile(r"^[A-Za-z]:|^[\\/]{2}")


def within_jail(path: Path | str, root: Path | None = None) -> bool:
    """True iff `path` resolves to something inside the jail root."""
    root = (root or cfg.JAIL_ROOT).resolve()
    if os.name != "nt" and _WIN_DRIVE_OR_UNC.match(str(path)):
        return False
    try:
        resolved = Path(path).resolve()
    except Exception:
        return False
    return resolved == root or root in resolved.parents


# Windows resolves these basenames to devices in ANY directory, so "jail/NUL"
# is the NUL device, not a file inside the jail. The device is reached
# regardless of extension, trailing dot, or trailing space ("NUL.txt", "nul ").
_WIN_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _has_reserved_name(path: Path) -> bool:
    """True if any component of `path` is a Windows reserved device name."""
    for part in path.parts:
        stem = part.split(".", 1)[0].strip().rstrip(".").lower()
        if stem in _WIN_RESERVED:
            return True
    return False


def safe_child(root: Path | None, name: str) -> Path | None:
    """Resolve `name` as a child of the jail, or None if it escapes.

    Rejects absolute paths, `..` traversal, and Windows reserved device names;
    the result must stay inside. Fails closed: a malformed name (embedded null,
    illegal characters) that can't be resolved returns None rather than raising,
    so a bad path is refused, never crashes the caller.
    """
    root = (root or cfg.JAIL_ROOT).resolve()
    try:
        if not name:
            return None
        if os.name != "nt":
            # Apply Windows path syntax on every platform (see
            # _WIN_DRIVE_OR_UNC): drive/UNC prefixes are absolute → refuse;
            # backslashes are separators → normalize so ".." hidden behind
            # them is caught below instead of passing as one filename.
            if _WIN_DRIVE_OR_UNC.match(name):
                return None
            name = name.replace("\\", "/")
        if ".." in Path(name).parts:
            return None
        candidate = (root / name).resolve()
    except (ValueError, OSError):
        return None
    if _has_reserved_name(candidate):
        return None
    return candidate if within_jail(candidate, root) else None


# --- command screening -----------------------------------------------------
def _first_verb(argv: list[str]) -> str:
    v = (argv[0] if argv else "").strip().lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if v.endswith(suffix):
            v = v[: -len(suffix)]
    return v


def _risk_table_blocks_verb(verb: str) -> str | None:
    """Consult RISK_TABLE (via agent_planner) — one policy source (plan 0.7).

    Returns a refusal reason when the shell verb maps to a blocked action kind
    (delete/remove). Falls back to None when app.* is unavailable so the
    desktop package stays importable standalone; local BLOCKED_VERBS still apply.
    """
    try:
        from app.services.agent_planner import (
            kind_for_shell_verb, policy_block_reason,
        )
    except Exception:
        return None
    kind = kind_for_shell_verb(verb)
    if not kind:
        return None
    return policy_block_reason(kind=kind, label=verb)


def scan_danger(argv: list[str]) -> str | None:
    """Return a reason string if the command is categorically unsafe, else None.

    This runs BEFORE allowlist classification: a hit here is a hard block that no
    approval can override — including autonomous mode (plan 0.7).
    """
    if not argv:
        return "empty command"
    verb = _first_verb(argv)
    # RISK_TABLE first — single semantic policy for delete/remove.
    policy = _risk_table_blocks_verb(verb)
    if policy:
        return policy
    if verb in cfg.BLOCKED_VERBS:
        return f"blocked verb: {verb!r} (destructive / elevation / nested shell)"
    for arg in argv:
        low = str(arg).lower()
        if any(ch in cfg.SHELL_METACHARS for ch in str(arg)):
            return f"shell metacharacter in argument: {arg!r}"
        if any(marker in low for marker in cfg.SECRET_MARKERS):
            return f"argument reaches a sensitive/secret path: {arg!r}"
        if ".." in Path(str(arg)).parts:
            return f"path traversal in argument: {arg!r}"
    return None


def policy_blocks(*, verb: str | None = None, summary: str = "",
                  label: str = "", fields: dict | None = None) -> str | None:
    """Desktop-facing RISK_TABLE check for mutating actions (plan 0.7).

    Used by the driver before the approval ask so autonomous auto-approve
    cannot unlock delete/remove. Returns a refusal reason or None.
    """
    fields = fields or {}
    action = (fields.get("action") or verb or "").strip()
    try:
        from app.services.agent_planner import (
            kind_for_shell_verb, policy_block_reason,
        )
    except Exception:
        # Standalone fallback: block obvious delete/remove labels only.
        import re
        text = f"{action} {summary} {label}"
        if re.search(r"\b(delete|remove|erase|uninstall)\b", text, re.I):
            return f"blocked by policy (delete) — autonomous mode cannot override"
        return None
    kind = ""
    if verb:
        kind = kind_for_shell_verb(verb) or ""
    return policy_block_reason(
        kind=kind or action, goal=summary or "",
        label=label or action, summary=summary or "")


def classify_command(argv: list[str]) -> tuple[Tier, str]:
    """Screen for danger, then place an allowlisted command in a risk tier.

    Returns (tier, reason). Unknown verbs are BLOCKED, not gated — default-deny.
    """
    danger = scan_danger(argv)
    if danger:
        return Tier.BLOCKED, danger

    verb = _first_verb(argv)
    if verb == "git":
        sub = (argv[1].strip().lower() if len(argv) > 1 else "")
        if sub in cfg.GIT_READ_SUBS:
            return Tier.READ_ONLY, f"git {sub} (read-only)"
        if sub in cfg.GIT_MUTATE_SUBS:
            return Tier.MUTATING, f"git {sub} (mutating)"
        return Tier.MUTATING, f"git {sub or '?'} (unrecognized — gated)"
    if verb in cfg.READ_VERBS:
        return Tier.READ_ONLY, f"{verb} (read-only)"
    if verb in cfg.MUTATE_VERBS:
        return Tier.MUTATING, f"{verb} (mutating — runs code)"
    return Tier.BLOCKED, f"{verb!r} is not on the command allowlist"


def check_launch_args(args: list[str], root: Path | None = None) -> str | None:
    """Validate args passed to a launched app: any path-like arg must be jailed.

    Flags (leading '-') pass through; anything that looks like a path must
    resolve inside the jail so "open Cursor at <arbitrary dir>" can't escape.
    """
    for a in args or []:
        s = str(a)
        if s.startswith("-"):
            continue
        if ("/" in s) or ("\\" in s) or (":" in s) or Path(s).exists():
            if not within_jail(s, root):
                return f"path argument outside jail: {s!r}"
    return None


def open_target_allowed(key: str, suffix: str, is_dir: bool) -> str | None:
    """Capability policy: may app `key` be opened ON this target? Pure.

    The jail guarantees WHERE (inside the sandbox); this guarantees WHAT — an
    app is only pointed at file types (or folders) its capability entry declares.
    `suffix` is the target's extension (".flp"); `is_dir` says it's a folder.
    Returns a refusal reason, or None if allowed. The driver supplies the
    filesystem fact (`is_dir`); the decision stays here and side-effect-free.
    """
    caps_exts = cfg.openable_extensions(key)
    name = cfg.app_display_name(key)
    if is_dir:
        if cfg.app_opens_dirs(key):
            return None
        opens = ", ".join(sorted(caps_exts)) if caps_exts else "no targets"
        return f"{name} does not open folders (it opens {opens})"
    ext = (suffix or "").lower()
    if ext in caps_exts:
        return None
    allowed = ", ".join(sorted(caps_exts)) if caps_exts else "no file types"
    return (f"{name} cannot open {ext or 'extensionless'} targets "
            f"(it opens: {allowed})")
