"""Onboarding system scan (phase 1) — pre-fill the profile wizard from the
machine itself, so a new user isn't staring at a blank form.

This reads only LOW-sensitivity local signals and produces a *draft* in the
exact shape the wizard already posts to /onboarding/ingest:

    installed apps  -> tools[]        (Start-Menu shortcut names, filtered)
    git identity    -> identity.name  (+ email as a note line)
    dev folders     -> projects[]     (dirs holding a .git / package.json / …)

Crucially it NEVER ingests. The draft is returned for the user to review, edit,
and confirm in the wizard; only then does the normal ingest run and mark the
facts ACCEPTED. That keeps the epistemic contract honest — inferred signals
don't masquerade as things the human stated. Higher-sensitivity sources
(browser history, email, calendar attendees) are deliberately out of scope here
and belong behind their own consent step.

Every function is best-effort and never raises: a scan that finds nothing just
returns an empty draft, and the wizard falls back to manual entry.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings

# Shortcut names that aren't "apps you use" — updaters, docs, uninstallers.
_APP_NOISE = re.compile(
    r"(?i)\b(uninstall|uninstaller|update|updater|readme|read\s?me|"
    r"release\s?notes|documentation|docs|help|website|home\s?page|on\s?the\s?web|"
    r"licen[cs]e|changelog|repair|modify|setup|installer|manual|"
    r"getting\s?started|report\s?a\s?bug|feedback|application\s?verifier)\b")
# Start-Menu SUBFOLDERS that hold Windows plumbing, not the user's apps. Any
# shortcut nested under one of these is skipped wholesale.
_SYSTEM_FOLDERS = frozenset({
    "administrative tools", "windows administrative tools", "windows tools",
    "system tools", "windows system", "windows powershell", "accessibility",
    "maintenance", "startup", "windows accessories", "accessories",
})
# Built-in Windows utilities that slip through as top-level shortcuts.
_APP_NAME_DENY = frozenset({
    "access", "character map", "command prompt", "control panel",
    "computer management", "component services", "create usb recovery",
    "debuggable package manager", "task manager", "registry editor",
    "event viewer", "system information", "resource monitor", "disk cleanup",
    "run", "file explorer", "this pc", "remote desktop connection",
    "steps recorder", "quick assist", "math input panel", "narrator",
    "magnifier", "on-screen keyboard", "speech recognition", "xps viewer",
    "windows media player legacy", "odbc data sources", "administrative tools",
    "more...", "office language preferences", "run",
})
_MAX_APPS = 60

# A directory is a "project" if it directly contains one of these markers.
_PROJECT_MARKERS = (
    ".git", "package.json", "pyproject.toml", "requirements.txt", "Cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json",
    "CMakeLists.txt", ".sln", ".csproj",
)
# Folder names to never treat as a project root child (dependency/build junk).
_PROJECT_SKIP = frozenset({
    "node_modules", "venv", ".venv", "env", ".git", "__pycache__", "dist",
    "build", "target", ".idea", ".vscode", "site-packages",
})
_MAX_PROJECTS = 30
_MAX_CHILDREN_PER_ROOT = 300


# --- installed apps ----------------------------------------------------------
def _start_menu_roots() -> list[Path]:
    roots = []
    for base in (os.environ.get("APPDATA", ""), os.environ.get("PROGRAMDATA", "")):
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu"
                         / "Programs")
    return roots


def clean_app_names(names: list[str], cap: int | None = _MAX_APPS) -> list[str]:
    """Filter shortcut stems to real, deduped app names (pure — unit-tested).
    `cap=None` returns the full list so a caller can rank before truncating."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip()
        key = name.lower()
        if not name or key in _APP_NAME_DENY or _APP_NOISE.search(name):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    out.sort(key=str.lower)
    return out[:cap] if cap else out


def installed_apps(roots: list[Path] | None = None,
                   cap: int | None = _MAX_APPS) -> list[str]:
    """App display names from Start-Menu .lnk shortcut filenames (no COM needed —
    the stem IS the display name). Shortcuts nested under a Windows system
    subfolder are skipped. Empty off-Windows or when nothing is found."""
    roots = roots if roots is not None else _start_menu_roots()
    stems: list[str] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for lnk in root.rglob("*.lnk"):
                try:
                    parents = {seg.lower() for seg in
                               lnk.relative_to(root).parts[:-1]}
                    if parents & _SYSTEM_FOLDERS:
                        continue
                except Exception:
                    pass
                stems.append(lnk.stem)
        except Exception:
            continue
    return clean_app_names(stems, cap=cap)


# --- recency ranking (reuse the desktop-activity trail) ----------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def usage_score(name: str, unorm: dict) -> float:
    """Focus-seconds for `name` against a NORMALIZED usage map, matched exactly
    or by substring (window-title apps: 'Google Chrome' lifts 'Chrome')."""
    n = _norm(name)
    best = unorm.get(n, 0.0)
    if best:
        return best
    for uk, uv in unorm.items():
        if uk in n or n in uk:
            best = max(best, uv)
    return best


def rank_apps(names: list[str], usage: dict) -> list[str]:
    """Order apps by observed focus-time (desc), then the rest alphabetically.
    Pure — unit-tested."""
    if not usage:
        return list(names)
    unorm = {_norm(k): v for k, v in usage.items() if _norm(k)}
    scored = [(usage_score(nm, unorm), nm) for nm in names]
    used = sorted((s for s in scored if s[0] > 0),
                  key=lambda x: (-x[0], x[1].lower()))
    rest = sorted((nm for sc, nm in scored if sc == 0), key=str.lower)
    return [nm for _s, nm in used] + rest


def used_apps(names: list[str], usage: dict) -> list[str]:
    """Only the apps with observed focus-time, most-used first — the high-signal
    subset for enrichment (skips the long tail of installed-but-never-opened)."""
    if not usage:
        return []
    unorm = {_norm(k): v for k, v in usage.items() if _norm(k)}
    scored = [(usage_score(nm, unorm), nm) for nm in names]
    return [nm for sc, nm in sorted((s for s in scored if s[0] > 0),
                                    key=lambda x: (-x[0], x[1].lower()))]


def _app_usage(days: int = 30) -> dict:
    """Focus-seconds per app over the recent window, best-effort (empty when no
    desktop capture has run — a brand-new user just gets alphabetical apps)."""
    try:
        from app.storage import get_store
        return get_store().app_usage(time.time() - days * 86400)
    except Exception:
        return {}


# --- git identity ------------------------------------------------------------
def _run_git(args: list[str]) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True,
                             timeout=5, shell=False)
        return (out.stdout or "").strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def git_identity(run=None) -> dict:
    """{'name','email'} from global git config, or {} — a strong, cheap read of
    who the developer is. `run` is injectable for tests."""
    run = run or _run_git
    name = run(["config", "--global", "user.name"])
    email = run(["config", "--global", "user.email"])
    out = {}
    if name:
        out["name"] = name.strip()[:120]
    if email and "@" in email:
        out["email"] = email.strip()[:160]
    return out


# --- dev-folder projects -----------------------------------------------------
def _project_roots() -> list[Path]:
    home = Path.home()
    names = ("source", "source/repos", "src", "dev", "code", "projects",
             "repos", "git", "workspace", "Documents", "Desktop")
    roots = [home / n for n in names]
    # Siblings of the current working directory surface the active project's
    # neighbors (e.g. the folder Sparrow itself lives in).
    try:
        roots.append(Path.cwd().parent)
    except Exception:
        pass
    return roots


def _is_project_dir(d: Path) -> bool:
    for marker in _PROJECT_MARKERS:
        try:
            if (d / marker).exists():
                return True
        except Exception:
            continue
    return False


def dev_projects(roots: list[Path] | None = None) -> list[dict]:
    """Directories that look like code projects (hold a VCS/build marker), as
    {name, kind, aliases, note} draft entries. Shallow + bounded."""
    roots = roots if roots is not None else _project_roots()
    found: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        try:
            rp = root.expanduser()
            if not rp.is_dir():
                continue
            for i, child in enumerate(sorted(rp.iterdir())):
                if i >= _MAX_CHILDREN_PER_ROOT:
                    break
                if not child.is_dir():
                    continue
                nm = child.name
                if nm.startswith(".") or nm.lower() in _PROJECT_SKIP:
                    continue
                key = nm.lower()
                if key in seen or not _is_project_dir(child):
                    continue
                seen.add(key)
                found.append({"name": nm, "kind": "project", "aliases": [],
                              "note": ""})
                if len(found) >= _MAX_PROJECTS:
                    return found
        except Exception:
            continue
    return found


# --- browser bookmarks (opt-in) ---------------------------------------------
# General knowledge, not user-specific (like the app registry): a curated map
# of common productivity/SaaS domains -> tool name. ONLY bookmarks matching one
# of these surface — a user's personal bookmarks (titles, URLs) never enter the
# profile. Substring-matched against the host, so "www.github.com" -> GitHub.
_BOOKMARK_TOOLS: dict[str, str] = {
    "github.com": "GitHub", "gitlab.com": "GitLab", "bitbucket.org": "Bitbucket",
    "notion.so": "Notion", "figma.com": "Figma", "linear.app": "Linear",
    "atlassian.net": "Jira", "trello.com": "Trello", "asana.com": "Asana",
    "slack.com": "Slack", "discord.com": "Discord", "zoom.us": "Zoom",
    "docs.google.com": "Google Docs", "sheets.google.com": "Google Sheets",
    "drive.google.com": "Google Drive", "mail.google.com": "Gmail",
    "calendar.google.com": "Google Calendar", "outlook.office.com": "Outlook",
    "office.com": "Microsoft 365", "aws.amazon.com": "AWS",
    "console.cloud.google.com": "Google Cloud", "portal.azure.com": "Azure",
    "vercel.com": "Vercel", "netlify.com": "Netlify",
    "huggingface.co": "Hugging Face", "openai.com": "OpenAI",
    "claude.ai": "Claude", "chatgpt.com": "ChatGPT",
    "stackoverflow.com": "Stack Overflow", "linkedin.com": "LinkedIn",
    "tableau.com": "Tableau", "databricks.com": "Databricks",
    "snowflake.com": "Snowflake", "salesforce.com": "Salesforce",
    "hubspot.com": "HubSpot", "airtable.com": "Airtable",
    "dropbox.com": "Dropbox", "canva.com": "Canva", "miro.com": "Miro",
    "loom.com": "Loom", "tradingview.com": "TradingView",
    "alpaca.markets": "Alpaca", "coinbase.com": "Coinbase",
    "robinhood.com": "Robinhood", "jupyter.org": "Jupyter",
    "colab.research.google.com": "Google Colab",
}
_MAX_BOOKMARK_URLS = 8000


def _bookmark_files() -> list[Path]:
    """Chromium-family Bookmarks JSON files across installed browsers/profiles."""
    la = os.environ.get("LOCALAPPDATA", "")
    if not la:
        return []
    bases = [
        Path(la) / "Google" / "Chrome" / "User Data",
        Path(la) / "Microsoft" / "Edge" / "User Data",
        Path(la) / "BraveSoftware" / "Brave-Browser" / "User Data",
    ]
    files: list[Path] = []
    for base in bases:
        try:
            if not base.is_dir():
                continue
            for prof in base.iterdir():
                bm = prof / "Bookmarks"
                if bm.is_file():
                    files.append(bm)
        except Exception:
            continue
    return files


def _walk_bookmarks(node, urls: list[str]) -> None:
    if len(urls) >= _MAX_BOOKMARK_URLS or not isinstance(node, dict):
        return
    if node.get("type") == "url" and node.get("url"):
        urls.append(node["url"])
    for child in node.get("children") or []:
        _walk_bookmarks(child, urls)


def bookmark_tools(files: list[Path] | None = None) -> list[str]:
    """Recognized productivity tools present in browser bookmarks, most-frequent
    first. Only mapped domains surface; everything else is ignored (privacy).
    `files` is injectable for tests."""
    files = files if files is not None else _bookmark_files()
    urls: list[str] = []
    for f in files:
        try:
            data = json.loads(Path(f).read_text(encoding="utf-8"))
            for root in (data.get("roots") or {}).values():
                _walk_bookmarks(root, urls)
        except Exception:
            continue
    counts: dict[str, int] = {}
    for u in urls:
        try:
            host = (urlparse(u).hostname or "").lower()
        except Exception:
            continue
        if not host:
            continue
        for key, tool in _BOOKMARK_TOOLS.items():
            if key in host:
                counts[tool] = counts.get(tool, 0) + 1
                break
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _merge_tools(*lists: list[str]) -> list[str]:
    """Concatenate tool lists preserving order, deduped case-insensitively."""
    out: list[str] = []
    seen: set[str] = set()
    for lst in lists:
        for t in lst or []:
            k = t.lower()
            if t and k not in seen:
                seen.add(k)
                out.append(t)
    return out


# --- assemble the draft ------------------------------------------------------
def scan(sources=None) -> dict:
    """Build a reviewable draft profile from the enabled low-sensitivity signals.

    Returns {ok, profile, found, sources}. Never ingests, never raises.
    `sources` overrides the configured set (tests / callers that want a subset).
    """
    if not settings.onboarding.scan_enabled:
        return {"ok": False, "error": "system scan disabled "
                "(QUILL_ONBOARDING_SCAN=0)", "profile": {}, "found": {}}
    allowed = set(sources) if sources is not None else set(
        settings.onboarding.scan_sources)

    profile: dict = {}
    found = {"tools": 0, "projects": 0, "identity": False, "bookmark_tools": 0}
    ran: list[str] = []

    if "git" in allowed:
        ran.append("git")
        ident = git_identity()
        if ident.get("name"):
            profile.setdefault("identity", {})["name"] = ident["name"]
            found["identity"] = True
        if ident.get("email"):
            profile["notes"] = f"Email: {ident['email']}"

    # Apps + bookmark tools merge into one "tools" list, ranked by real usage.
    app_tools: list[str] = []
    bm_tools: list[str] = []
    if "apps" in allowed:
        ran.append("apps")
        # Full list (uncapped) -> rank by observed usage -> then cap, so a
        # heavily-used but late-alphabet app isn't truncated away.
        app_tools = rank_apps(installed_apps(cap=None), _app_usage())[:_MAX_APPS]
    if "bookmarks" in allowed:
        ran.append("bookmarks")
        bm_tools = bookmark_tools()
        found["bookmark_tools"] = len(bm_tools)
    # Bookmark tools first — higher-signal (curated SaaS you saved, and often
    # web apps with no Start-Menu entry, so they'd be invisible otherwise).
    # Final cap keeps the review list bounded.
    tools = _merge_tools(bm_tools, app_tools)[:_MAX_APPS]
    if tools:
        profile["tools"] = tools
        found["tools"] = len(tools)

    if "projects" in allowed:
        ran.append("projects")
        projs = dev_projects()
        if projs:
            profile["projects"] = projs
            found["projects"] = len(projs)

    return {"ok": True, "profile": profile, "found": found, "sources": ran}


# --- enrichment: source scan signals into memory as OBSERVED context ---------
# Distinct from the survey (onboarding.py). The survey is what the USER STATES
# (epistemic=accepted, human-approved). This is what the machine OBSERVES about
# them — installed tools, project folders, git identity — sourced as background
# context the assistant can draw on, never presented as user-stated fact and
# never auto-approved. Traceable (source below) and reversible in the console.
ENRICH_SOURCE = "onboarding.scan"
_ENRICH_MAX_TOOL_NAMES = 20      # how many tool names to name in the summary claim


def _load_scan_state() -> dict:
    import json as _json
    p = Path(settings.onboarding.scan_state_path)
    try:
        return _json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except Exception:
        return {}


def _save_scan_state(state: dict) -> None:
    import json as _json
    p = Path(settings.onboarding.scan_state_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[onboarding_scan] state save skipped ({exc}).")


def _key(kind: str, payload) -> str:
    import hashlib
    blob = json.dumps([kind, payload], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def enrich(sources=None, store=None) -> dict:
    """Grab additional CONTEXT from the machine and add it to memory as observed
    knowledge — projects the user works on, tools they actually use, their git
    identity. Does NOT touch the onboarding survey/form and does NOT mark
    onboarding complete: it's a separate, honest enrichment layer.

    Idempotent (content-keyed dedup) and best-effort. Returns counts.
    """
    if not settings.onboarding.scan_enabled:
        return {"ok": False, "error": "system scan disabled "
                "(QUILL_ONBOARDING_SCAN=0)"}
    allowed = set(sources) if sources is not None else set(
        settings.onboarding.scan_sources)

    from app.events import Event, Modality
    from app.services import confidence as _conf
    from app.storage import get_store

    store = store or get_store()
    live = False
    index = None
    try:
        live = store is get_store()
        if live:
            from app.services.memory import memory
            index = memory.index_fact
    except Exception:
        index = None

    state = _load_scan_state()
    seen = set(state.get("item_keys") or [])
    new_keys: list[str] = []
    counts = {"projects": 0, "tools": 0, "identity": 0, "skipped": 0}
    now = time.time()

    def fresh(kind, payload) -> bool:
        k = _key(kind, payload)
        if k in seen:
            counts["skipped"] += 1
            return False
        seen.add(k)
        new_keys.append(k)
        return True

    def observe(text: str, section: str) -> int:
        """One OBSERVED provenance event for a piece of enrichment context."""
        ev = Event(time=now, modality=Modality.SYSTEM, raw=text,
                   summary=f"[scan] {text}", source=ENRICH_SOURCE,
                   meta={"section": section})
        _conf.attach(ev, _conf.OBSERVED, model=0.9)
        return store.insert(ev)

    def claim(text: str, section: str) -> None:
        eid = observe(text, section)
        # Unreviewed on purpose: observed context, not a human-accepted fact.
        fid = store.add_claim(text, source_event_id=eid, source_span=text,
                              confidence=0.9, extracted_at=now)
        if index is not None:
            try:
                index(fid, "claim", text, now)
            except Exception as exc:
                print(f"[onboarding_scan] index skipped ({exc}).")

    # --- projects the user works on -------------------------------------------
    if "projects" in allowed:
        for proj in dev_projects():
            name = proj.get("name") or ""
            if not name or not fresh("project", name):
                continue
            store.resolve_entity(name, "project", ts=now)
            claim(f"The user has a code project named '{name}' on their computer.",
                  "projects")
            counts["projects"] += 1

    # --- tools they actually use (usage-signal + curated bookmarks) -----------
    if "apps" in allowed or "bookmarks" in allowed:
        tool_names: list[str] = []
        if "apps" in allowed:
            tool_names += used_apps(installed_apps(cap=None), _app_usage())
        if "bookmarks" in allowed:
            tool_names += bookmark_tools()
        tools = _merge_tools(tool_names)
        for t in tools:
            if fresh("tool", t):
                store.resolve_entity(t, "tool", ts=now)
                counts["tools"] += 1
        if tools and fresh("tools_summary", tools[:_ENRICH_MAX_TOOL_NAMES]):
            named = ", ".join(tools[:_ENRICH_MAX_TOOL_NAMES])
            claim(f"Tools and apps the user actively uses include: {named}.",
                  "tools")

    # --- git identity as context ----------------------------------------------
    if "git" in allowed:
        ident = git_identity()
        who = ident.get("name") or ""
        email = ident.get("email") or ""
        if who and fresh("git_identity", [who, email]):
            tail = f" (git email {email})" if email else ""
            claim(f"The machine's git identity is {who}{tail} — likely the user.",
                  "identity")
            counts["identity"] += 1

    if new_keys:
        state["item_keys"] = sorted(seen)
        state["last_run"] = now
        _save_scan_state(state)
    added = counts["projects"] + counts["tools"] + counts["identity"]
    print(f"[onboarding_scan] enrich: {counts}")
    return {"ok": True, "added": added, **counts}
