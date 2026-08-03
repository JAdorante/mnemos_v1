"""Configuration for the guarded desktop driver — the allowlists ARE the policy.

Everything the agent may do is enumerated here; anything not listed is refused.
Tuning this file is how you widen (or narrow) the agent's reach. Env overrides
let you relocate the jail or disable the approval gate for a scripted demo.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def _get(key: str, default: str) -> str:
    return os.environ.get(key, default)


# --- the jail --------------------------------------------------------------
# The single most important guardrail: every file the agent creates, every cwd
# it runs a command in, and every path it opens must live under this root.
# Outside it, the agent can touch nothing. Default: a dedicated folder in $HOME.
JAIL_ROOT = Path(_get("QUILL_DESKTOP_JAIL", str(Path.home() / "quill_desktop"))).resolve()

# Master switch for the human approval gate on mutating actions.
REQUIRE_APPROVAL = _get("QUILL_DESKTOP_APPROVAL", "1") not in ("0", "false", "False")

# Bounds on a single task.
COMMAND_TIMEOUT_S = float(_get("QUILL_DESKTOP_TIMEOUT_S", "60"))
MAX_ACTIONS_PER_TASK = int(_get("QUILL_DESKTOP_MAX_ACTIONS", "25"))
# Largest single file the agent may author via write_file (bytes).
MAX_FILE_BYTES = int(_get("QUILL_DESKTOP_MAX_FILE_BYTES", "200000"))

# Pixel UI automation (screenshot + click/type). Windows only.
PIXEL_UI = (
    os.name == "nt"
    and _get("QUILL_DESKTOP_UI", "1") not in ("0", "false", "False")
)
PIXEL_PAUSE_S = float(_get("QUILL_DESKTOP_UI_PAUSE_S", "0.15"))
PIXEL_MAX_TYPE_CHARS = int(_get("QUILL_DESKTOP_UI_MAX_TYPE", "500"))
PIXEL_VISION = _get("QUILL_DESKTOP_UI_VISION", "1") not in ("0", "false", "False")

# Ghost desktop: launched app windows are parked off-screen (no taskbar
# button) and streamed into the chat's ghost pane; the agent drives them via
# UI Automation. Excluded apps launch visibly — their flows need the real
# screen (canvas/pixel UIs, special drivers).
GHOST_DESKTOP = (
    os.name == "nt"
    and _get("QUILL_GHOST_DESKTOP", "1") not in ("0", "false", "False")
)
GHOST_DESKTOP_EXCLUDE = frozenset(
    k.strip().lower()
    for k in _get("QUILL_GHOST_DESKTOP_EXCLUDE", "flstudio,phonelink").split(",")
    if k.strip()
)


# --- launchable apps -------------------------------------------------------
# The agent names a key ("cursor"), never a path. The allowlist is DATA, not code
# (the general-code invariant): it ships in desktop_agent/data/apps.default.json
# with machine-specific paths written as env-var tokens, and a user can add apps or
# repoint a key via an overlay (QUILL_DESKTOP_APPS) without editing this file.
# app_registry does the loading/merge/expand/platform-filter; the three module
# names below stay identical in shape so every downstream reader is unchanged.
from desktop_agent import app_registry as _registry  # noqa: E402

APP_CANDIDATES, APP_LAUNCH_ARGS, APP_CAPABILITIES = _registry.build_registry(JAIL_ROOT)


def allowed_app_keys() -> str:
    """Comma-separated allowlist for prompts and tool descriptions."""
    return ", ".join(sorted(APP_CANDIDATES))


def resolve_app_path(key: str) -> str | None:
    """Return the resolved executable path for an allowlisted app key, or None.

    Probe order (first hit wins): the app's own candidates (a bare name via PATH,
    an absolute path if it exists on disk) -> the Windows App Paths registry
    (read-only) -> an optional Start-Menu .lnk scan -> None.

    Registry keys ONLY: an unknown key returns None immediately — it must go
    through runtime discovery (desktop_agent/discovery.py), which vets what it
    finds, rather than riding the unvetted fallback probes here."""
    cands = APP_CANDIDATES.get(key.lower())
    if cands is None:
        return None
    for cand in cands:
        # A bare name resolves via PATH; an absolute path must exist on disk.
        if os.path.sep in cand or (":" in cand):
            if Path(cand).is_file():
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    # Fallbacks for machines where the app installed somewhere we didn't enumerate.
    reg = _registry.resolve_from_app_paths(cands)
    if reg:
        return reg
    return _registry.resolve_from_start_menu(app_display_name(key), cands)


# --- per-app capabilities --------------------------------------------------
# APP_CANDIDATES says WHICH apps exist; APP_CAPABILITIES (built above by the
# registry from the same JSON) says WHAT the agent may do with each. Same keys,
# kept in sync (a test enforces parity), because the registry guarantees every
# candidate key a capabilities entry. Metadata that only ever NARROWS: an app
# still can't launch unless it resolves on disk and clears the jail + approval
# gates. The driver reads `open_jailed_files`/`opens_dirs` to refuse pointing an
# app at a target it has no business opening; prompts read `describe_apps()` so the
# planner reasons over the real contract, not a guess.
#
#   open_jailed_files : file extensions the app may be opened ON (lowercased)
#   opens_dirs        : may the app be opened on a folder (editors, explorer)
#   pixel_ui          : "approval_required" if in-app click/type is meaningful,
#                       else "off"/"special" — informational, gating lives in
#                       driver + QUILL_DESKTOP_UI
#   shell/network/risk/demo_safe/notes : advisory context for planning + UI

# Locked-down fallback for any key without an explicit entry: launch-only,
# opens nothing. A missing capability fails safe (closed), never open.
_DEFAULT_CAPS: dict = _registry.LOCKED_CAPS


def capabilities(key: str) -> dict:
    """The capability contract for an app key, or a locked-down default."""
    caps = APP_CAPABILITIES.get((key or "").lower())
    if caps is None:
        return {**_DEFAULT_CAPS, "display_name": key or "?"}
    return caps


def app_display_name(key: str) -> str:
    return capabilities(key).get("display_name") or key


def openable_extensions(key: str) -> set[str]:
    """Extensions (lowercased, with dot) the app may be opened ON."""
    return {str(e).lower() for e in capabilities(key).get("open_jailed_files", [])}


def app_opens_dirs(key: str) -> bool:
    return bool(capabilities(key).get("opens_dirs", False))


def describe_apps() -> str:
    """One line per allowlisted app describing its capability contract — the
    single source the launch_app tool description and desktop prompts read, so
    config and prompt can't drift apart."""
    lines = []
    for key in sorted(APP_CANDIDATES):
        c = capabilities(key)
        opens = []
        if c.get("opens_dirs"):
            opens.append("a jailed folder")
        exts = c.get("open_jailed_files") or []
        if exts:
            opens.append("/".join(exts) + " files")
        target = "opens " + " or ".join(opens) if opens else "launch only"
        ui = (" (in-app click/type needs QUILL_DESKTOP_UI=1)"
              if c.get("pixel_ui") == "approval_required" else "")
        lines.append(f"- {key} ({app_display_name(key)}): {target}{ui}")
    if APP_DISCOVERY:
        lines.append(
            "- (any other installed app): request it by bare name (e.g. "
            "\"spotify\") — it is discovered from this machine (PATH / App "
            "Paths / Start Menu) and vetted: no shells, script hosts, "
            "interpreters, or admin tools, and nothing inside the jail, temp, "
            "or downloads. Discovered apps are launch only (no file/folder "
            "target), and their first use always asks the human.")
    return "\n".join(lines)


# --- shell command allowlist ----------------------------------------------
# Classified by the leading verb. READ verbs run without a prompt; MUTATE verbs
# require approval; anything not listed is refused (not merely gated).
READ_VERBS = {"ls", "dir", "echo", "where", "whoami", "hostname", "type", "cat"}
MUTATE_VERBS = {"npm", "npx", "pnpm", "yarn", "pip", "pip3",
                "python", "python3", "node"}

# `git` is split by subcommand: reads are free, mutations gated, unknown gated.
GIT_READ_SUBS = {"status", "log", "diff", "branch", "show", "remote", "config"}
GIT_MUTATE_SUBS = {"init", "add", "commit", "clone", "checkout", "switch",
                   "pull", "fetch", "merge", "restore", "tag", "stash"}


# --- hard blocks (never runnable, no prompt can unlock in the prototype) ----
# Destructive verbs, elevation, nested shells (which would escape the argv-list
# protection), and anything that reaches for the network or the registry.
BLOCKED_VERBS = {
    "rm", "rmdir", "rd", "del", "erase", "format", "mkfs", "diskpart",
    "reg", "regedit", "sc", "net", "netsh", "shutdown", "restart", "taskkill",
    "runas", "sudo", "su", "icacls", "attrib", "cipher", "fsutil", "bcdedit",
    "wmic", "powershell", "pwsh", "cmd", "bash", "sh", "curl", "wget",
    "iwr", "invoke-webrequest", "certutil", "bitsadmin", "schtasks", "mshta",
    "rundll32", "regsvr32",
}

# --- runtime app discovery (criteria, not enumeration) ----------------------
# The registry above is the fast path with rich capability contracts; discovery
# is how launch_app handles every OTHER installed app. Instead of refusing a key
# nobody enumerated, the driver resolves it from the machine's own registration
# channels (PATH / App Paths / Start Menu) and judges it against the criteria in
# desktop_agent/discovery.py. Discovered apps are launch-only (LOCKED_CAPS) and
# their FIRST use always asks the human, regardless of autonomy level.
APP_DISCOVERY = _get("QUILL_DESKTOP_APP_DISCOVERY", "1") not in ("0", "false", "False")

# Basenames that never launch as "apps" no matter how they were found: shells,
# script hosts, interpreters, installers, and admin consoles. Code execution
# belongs to run_command (with its own allowlist + gates), never launch_app.
DISCOVERY_DENY_BASENAMES = frozenset(BLOCKED_VERBS) | {
    "cscript", "wscript", "msiexec", "mmc", "control", "msconfig",
    "taskschd", "eventvwr", "compmgmt", "gpedit", "secpol", "perfmon",
    "regedt32", "hh", "ftp", "telnet", "ssh", "scp",
    "python", "pythonw", "py", "node", "java", "javaw", "ruby", "perl",
}

# Path substrings (lowercased, backslash-normalized) marking locations where
# unvetted binaries land — a discovered exe living under any of these is refused.
# The jail itself is checked separately (guards.within_jail).
DISCOVERY_UNTRUSTED_MARKERS = (
    "\\temp\\", "\\tmp\\", "\\downloads\\", "$recycle.bin", "\\recycler",
)

# Shell metacharacters: with shell=False these are inert, but their presence in
# an arg means something is trying to smuggle a second command — refuse outright.
SHELL_METACHARS = set(";|&`$><\n\r")

# Substrings that mark a path to secrets / sensitive system areas.
SECRET_MARKERS = (
    ".ssh", "id_rsa", ".env", ".pem", ".key", "credential", "secrets",
    "system32", "\\windows\\", "appdata\\roaming", ".aws", ".gnupg",
    ".config\\gcloud", "keychain", "cookies",
)

# Where the audit trail is written (append-only JSONL). Defaults inside the
# package for zero-config; set QUILL_DESKTOP_SESSIONS to relocate it off the repo
# (e.g. a per-user data dir) without editing code.
SESSIONS_ROOT = Path(_get("QUILL_DESKTOP_SESSIONS",
                          str(Path(__file__).resolve().parent / "sessions"))).expanduser()


# --- granular autonomy (strategic doc #4) ----------------------------------
# An autonomous run must still not auto-approve *everything*. Launching FL Studio
# is not the same trust as clicking around inside it, or running a shell command.
# This splits which desktop verbs may auto-run in an autonomous run, by a ceiling
# level. The driver's approval gate is unchanged — this only decides, per verb,
# whether the orchestrator answers that gate automatically or defers to the human.
#
# Levels, least -> most permissive (each includes the ones before it):
#   off          every desktop action needs a human, even in an autonomous run
#   launch_only  launch_app may auto-run
#   jailed_files + make_dir / write_file (jailed file authoring)
#   ui_control   + click_at / type_text / press_key (pixel UI can act off-jail!)
#   full         everything at the desktop layer
# The default draws the line at the jail boundary: verbs the jail contains
# auto-run; verbs that reach OUTSIDE it (pixel UI clicks anywhere on screen) stay
# gated. Shell is its own axis (below) and off even at `full`.
DESKTOP_AUTONOMY_LEVELS = ("off", "launch_only", "jailed_files", "ui_control", "full")
AGENT_AUTONOMY_DESKTOP = _get("AGENT_AUTONOMY_DESKTOP", "jailed_files").strip().lower()
if AGENT_AUTONOMY_DESKTOP not in DESKTOP_AUTONOMY_LEVELS:
    AGENT_AUTONOMY_DESKTOP = "jailed_files"

# run_command runs code — the highest-risk desktop verb — so its auto-approval is
# a separate, explicit opt-in that stays OFF even when desktop autonomy is `full`.
AGENT_AUTONOMY_SHELL = _get("AGENT_AUTONOMY_SHELL", "off").strip().lower() in (
    "on", "1", "true", "yes")

_LEVEL_RANK = {lvl: i for i, lvl in enumerate(DESKTOP_AUTONOMY_LEVELS)}
# Minimum desktop autonomy level at which each verb may auto-approve. run_command
# is intentionally absent — it is governed solely by AGENT_AUTONOMY_SHELL.
_VERB_MIN_LEVEL = {
    "launch_app": "launch_only",
    "make_dir": "jailed_files",
    "write_file": "jailed_files",
    "click_at": "ui_control",
    "type_text": "ui_control",
    "press_key": "ui_control",
}

# Desktop verbs that are always read-only — they never gate, autonomous or not.
READ_ONLY_VERBS = frozenset({"list_dir", "screenshot"})


def desktop_autoapprove(verb: str, level: str | None = None,
                        *, shell: bool | None = None) -> bool:
    """May this desktop verb auto-approve in an autonomous run? Pure policy.

    Answers only the "auto vs ask the human" question for an autonomous run; it
    never widens what the driver allows. Unknown verbs never auto-approve
    (fail-safe: ask the human). `level`/`shell` default to the env config.
    """
    verb = (verb or "").strip()
    if verb in READ_ONLY_VERBS:
        return True
    if verb == "run_command":
        return AGENT_AUTONOMY_SHELL if shell is None else bool(shell)
    need = _VERB_MIN_LEVEL.get(verb)
    if need is None:
        return False  # unknown mutating verb: fail safe, defer to the human
    lvl = (level or AGENT_AUTONOMY_DESKTOP)
    if lvl not in _LEVEL_RANK or lvl == "off":
        return False
    if lvl == "full":
        return True
    return _LEVEL_RANK[lvl] >= _LEVEL_RANK[need]
