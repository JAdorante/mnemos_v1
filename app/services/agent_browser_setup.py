"""Guided agent-browser connection — pick a browser, sign in once, persist.

Self-serve twin of the manual ``QUILL_AGENT_PROFILE`` / ``QUILL_AGENT_CHANNEL``
env vars: onboarding (or any page) calls this to

1. detect which real browsers are installed (Chrome / Edge; the bundled
   Chromium always works) and which one is the OS default,
2. open a visible sign-in window running the *agent's own* persistent profile
   (``sessions/profiles/main``) so the user logs into Google/whatever once,
3. persist the choice to the credentials file only after the window flow
   completes (the icloud_account validate-live-then-persist pattern).

The agent profile is deliberately separate from the user's personal browser
profile — the user signs in *as themselves* inside it, but their day-to-day
browser state is never touched or shipped.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROFILE_NAME = "main"
_PROFILE_ENV = "QUILL_AGENT_PROFILE"
_CHANNEL_ENV = "QUILL_AGENT_CHANNEL"
_SIGNIN_URL = "https://accounts.google.com/"
_SIGNIN_TIMEOUT_S = 15 * 60

_lock = threading.Lock()
_signin: dict[str, Any] = {"running": False, "done": False, "error": "",
                           "channel": "", "started_at": 0.0}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def _win_paths(channel: str) -> list[Path]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    rel = {
        "chrome": r"Google\Chrome\Application\chrome.exe",
        "msedge": r"Microsoft\Edge\Application\msedge.exe",
    }[channel]
    return [Path(p) / rel for p in (pf, pf86, local) if p]


def _mac_paths(channel: str) -> list[Path]:
    rel = {
        "chrome": "Google Chrome.app/Contents/MacOS/Google Chrome",
        "msedge": "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    }[channel]
    return [Path("/Applications") / rel,
            Path.home() / "Applications" / rel]


def _installed(channel: str) -> bool:
    if sys.platform == "win32":
        return any(p.is_file() for p in _win_paths(channel))
    if sys.platform == "darwin":
        return any(p.is_file() for p in _mac_paths(channel))
    exe = {"chrome": ("google-chrome", "google-chrome-stable"),
           "msedge": ("microsoft-edge", "microsoft-edge-stable")}[channel]
    return any(shutil.which(e) for e in exe)


def default_channel() -> str:
    """Best-effort OS default browser -> Playwright channel ('' if unknown)."""
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\https\UserChoice")
            progid, _ = winreg.QueryValueEx(key, "ProgId")
            pid = (progid or "").lower()
            if "chrome" in pid:
                return "chrome"
            if "edge" in pid:
                return "msedge"
        except OSError:
            pass
    return ""


def headed_available() -> bool:
    """A visible sign-in window needs a display (desktop install)."""
    if os.environ.get("QUILL_HEADLESS", "") in ("1", "true", "True"):
        return False
    if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return True


def profile_dir() -> Path:
    from browser_agent.config import PROFILES_ROOT
    return Path(PROFILES_ROOT) / PROFILE_NAME


def _profile_populated() -> bool:
    d = profile_dir()
    try:
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False


def status() -> dict[str, Any]:
    default = default_channel()
    channels = [{"id": "", "label": "Built-in browser (Chromium)",
                 "installed": True, "default": False}]
    for cid, label in (("chrome", "Google Chrome"),
                       ("msedge", "Microsoft Edge")):
        channels.append({"id": cid, "label": label,
                         "installed": _installed(cid),
                         "default": cid == default})
    with _lock:
        s = dict(_signin)
    if s["running"] and time.time() - s["started_at"] > _SIGNIN_TIMEOUT_S:
        s["running"] = False
    return {
        "available": headed_available(),
        "channels": channels,
        "configured": bool(os.environ.get(_PROFILE_ENV)),
        "channel": os.environ.get(_CHANNEL_ENV, ""),
        "profile_populated": _profile_populated(),
        "signin": {"running": s["running"], "done": s["done"],
                   "error": s["error"], "channel": s["channel"]},
    }


# ---------------------------------------------------------------------------
# Persistence (credentials file — same store as parent model / iCloud)
# ---------------------------------------------------------------------------
def _persist(channel: str) -> None:
    from app.services.icloud_account import _cred_path
    path = _cred_path()
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    drop = (f"{_PROFILE_ENV}=", f"{_CHANNEL_ENV}=")
    lines = [ln for ln in existing.splitlines() if not ln.startswith(drop)]
    lines.append(f"{_PROFILE_ENV}={PROFILE_NAME}")
    if channel:
        lines.append(f"{_CHANNEL_ENV}={channel}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[_PROFILE_ENV] = PROFILE_NAME
    if channel:
        os.environ[_CHANNEL_ENV] = channel
    else:
        os.environ.pop(_CHANNEL_ENV, None)


def disconnect() -> dict[str, Any]:
    """Forget the choice (agent goes back to ephemeral). Profile dir kept —
    deleting a signed-in profile is a separate, explicit act."""
    from app.services.icloud_account import _cred_path
    path = _cred_path()
    if path.is_file():
        drop = (f"{_PROFILE_ENV}=", f"{_CHANNEL_ENV}=")
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith(drop)]
        path.write_text("\n".join(lines) + ("\n" if lines else ""),
                        encoding="utf-8")
    os.environ.pop(_PROFILE_ENV, None)
    os.environ.pop(_CHANNEL_ENV, None)
    return {"ok": True, "profile_dir": str(profile_dir()),
            "note": "profile kept on disk; delete it to sign out fully"}


# ---------------------------------------------------------------------------
# Sign-in window
# ---------------------------------------------------------------------------
def _signin_worker(channel: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
        d = profile_dir()
        d.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            kwargs: dict[str, Any] = {"headless": False,
                                      "viewport": {"width": 1100, "height": 800}}
            if channel:
                kwargs["channel"] = channel
            ctx = pw.chromium.launch_persistent_context(str(d), **kwargs)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(_SIGNIN_URL, timeout=30000)
            except Exception:
                pass  # offline start page is fine; the window still works
            closed = threading.Event()
            ctx.on("close", lambda *_: closed.set())
            # The user signs in, then closes the window. Poll rather than
            # block forever; the timeout abandons a forgotten window.
            deadline = time.time() + _SIGNIN_TIMEOUT_S
            while not closed.is_set() and time.time() < deadline:
                if not ctx.pages:      # every tab closed = done
                    break
                time.sleep(1.0)
            try:
                ctx.close()
            except Exception:
                pass
        _persist(channel)
        with _lock:
            _signin.update(running=False, done=True, error="")
    except Exception as exc:
        with _lock:
            _signin.update(running=False, done=False, error=str(exc)[:300])


def start_signin(channel: str = "") -> dict[str, Any]:
    """Open the one-time sign-in window; persists the choice on completion."""
    if channel not in ("", "chrome", "msedge"):
        return {"ok": False, "error": f"unknown browser: {channel}"}
    if not headed_available():
        return {"ok": False, "error":
                "no display — sign-in window needs the desktop app"}
    if channel and not _installed(channel):
        return {"ok": False, "error": f"{channel} is not installed"}
    with _lock:
        if _signin["running"]:
            return {"ok": True, "already": True}
        _signin.update(running=True, done=False, error="",
                       channel=channel, started_at=time.time())
    threading.Thread(target=_signin_worker, args=(channel,),
                     name="agent-browser-signin", daemon=True).start()
    return {"ok": True}


def reset_for_tests() -> None:
    with _lock:
        _signin.update(running=False, done=False, error="",
                       channel="", started_at=0.0)
