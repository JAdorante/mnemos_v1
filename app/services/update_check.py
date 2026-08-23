"""Version manifest check (WS-C) — a notification, never an updater.

During a fast-iteration pilot, "update by extracting a ZIP over the folder"
guarantees stale installs and bug reports nobody can attribute to a build. This
tells a tester that a newer build exists. It does not fetch it, does not run
anything, and does not touch the install.

What crosses the network, exactly once a day at most:

    GET <QUILL_UPDATE_MANIFEST_URL>

No query parameters, no install id, no version header, no cookies — an
unconditional GET of a static file. The operator learns that some IP asked for
a file, which is what any static host logs anyway. That is the whole exposure,
and ``QUILL_UPDATE_CHECK=0`` (or the Privacy controls toggle) removes it.

The manifest the operator hosts::

    {"latest": "0.4.2", "url": "https://…/Mnemos-0.4.2.zip",
     "notes": "…", "min_supported": "0.3.0"}

The answer is cached in ``data/update_check.json`` and served from there for
``QUILL_UPDATE_CHECK_HOURS`` (default 24). Failure — offline, DNS, timeout,
garbage JSON — is silent-with-log and leaves the cached answer alone.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from app.atomic_json import write_json
from app.config import settings
from app.version import __version__

_lock = threading.RLock()
# Persisted opt-out lives beside the other user-set state; the env var is the
# deployment default and the file is the user's decision, so the file wins.
_STATE_FILE = "update_check.json"


def _data_dir() -> Path:
    return Path(os.environ.get("QUILL_DATA_DIR") or settings.storage.data_dir)


def cache_path() -> Path:
    return _data_dir() / _STATE_FILE


def _env_enabled() -> bool:
    raw = os.environ.get("QUILL_UPDATE_CHECK")
    if raw is not None:
        return raw not in ("0", "false", "False")
    return bool(settings.update_check.enabled)


def manifest_url() -> str:
    raw = os.environ.get("QUILL_UPDATE_MANIFEST_URL")
    if raw is None:
        raw = settings.update_check.manifest_url
    return (raw or "").strip()


def _load() -> dict[str, Any]:
    out: dict[str, Any] = {"checked_at": None, "manifest": None,
                           "error": None, "user_enabled": None,
                           "dismissed": None}
    try:
        p = cache_path()
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                out.update({k: raw.get(k) for k in out if k in raw})
    except Exception as exc:
        print(f"[update_check] cache read skipped ({exc}).")
    return out


def _save(**fields: Any) -> dict[str, Any]:
    with _lock:
        state = _load()
        state.update(fields)
        try:
            write_json(cache_path(), state)
        except Exception as exc:
            print(f"[update_check] cache write skipped ({exc}).")
        return state


def enabled() -> bool:
    """Env default, overridden by the user's stored Privacy-controls choice."""
    stored = _load().get("user_enabled")
    if stored is not None:
        return bool(stored)
    return _env_enabled()


def set_enabled(on: bool) -> dict[str, Any]:
    _save(user_enabled=bool(on))
    return status()


# --------------------------------------------------------------------------
# semver comparison (packaging.version — never hand-rolled)
# --------------------------------------------------------------------------
def _parse(v: str | None):
    """Parse a version, or None. Strings only — a number in the manifest's
    `latest` is a malformed manifest, and coercing 4.2 into a version that
    outranks every real build is exactly the wrong failure mode."""
    from packaging.version import InvalidVersion, Version
    if not isinstance(v, str):
        return None
    try:
        return Version(v.strip())
    except (InvalidVersion, TypeError, ValueError):
        return None


def is_newer(latest: str | None, current: str | None) -> bool:
    """True when `latest` is strictly newer. Unparseable either side -> False.

    Prereleases sort below their release (0.4.1rc1 < 0.4.1), so a tester on
    0.4.1 is not nagged to "upgrade" to 0.4.1rc1.
    """
    lv, cv = _parse(latest), _parse(current)
    return bool(lv and cv and lv > cv)


def is_unsupported(current: str | None, min_supported: str | None) -> bool:
    """True when `current` is below the operator's floor (a hard nag)."""
    cv, mv = _parse(current), _parse(min_supported)
    return bool(cv and mv and cv < mv)


def banner(manifest: dict[str, Any] | None,
           current: str = __version__) -> dict[str, Any] | None:
    """The Console banner payload, or None when nothing should be shown."""
    if not isinstance(manifest, dict):
        return None
    latest = manifest.get("latest")
    unsupported = is_unsupported(current, manifest.get("min_supported"))
    if not unsupported and not is_newer(latest, current):
        return None
    return {
        "level": "critical" if unsupported else "info",
        "current": current,
        "latest": latest,
        "url": manifest.get("url") or "",
        "notes": manifest.get("notes") or "",
        "min_supported": manifest.get("min_supported"),
        "unsupported": unsupported,
        # Dismissal is per-version: a new `latest` shows the banner again.
        "dismiss_key": f"update:{latest}",
        "message": (
            f"This build ({current}) is below the minimum supported version "
            f"{manifest.get('min_supported')}. Update to {latest}."
            if unsupported else
            f"Mnemos {latest} is available (you have {current})."),
    }


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------
def _fetch(url: str, timeout: float) -> dict[str, Any]:
    from urllib.request import Request, urlopen
    # No headers beyond what urllib must send. Nothing identifies this install.
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read(64_000)
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest is not an object")
    return {k: data.get(k) for k in ("latest", "url", "notes", "min_supported")}


def check(*, now: float | None = None, force: bool = False,
          transport=None) -> dict[str, Any]:
    """Refresh the cached manifest if due. Never raises.

    `transport(url, timeout) -> dict` is injectable so tests can assert that a
    disabled check makes exactly zero requests.
    """
    now = float(now if now is not None else time.time())
    state = _load()
    if not enabled():
        return {**status(now=now), "reason": "disabled"}
    url = manifest_url()
    if not url:
        return {**status(now=now), "reason": "no_url"}
    if not force:
        last = state.get("checked_at")
        every = float(settings.update_check.every_hours) * 3600.0
        if last is not None and (now - float(last)) < every:
            return {**status(now=now), "reason": "cached"}
    try:
        fetch = transport or _fetch
        manifest = fetch(url, float(settings.update_check.timeout_s))
        _save(checked_at=now, manifest=manifest, error=None)
    except Exception as exc:
        # Offline is the common case, not an error worth surfacing. The cached
        # manifest (if any) survives, so a flaky network never blanks the UI.
        print(f"[update_check] skipped ({exc}).")
        _save(checked_at=now, error=f"{type(exc).__name__}: {exc}")
    return status(now=now)


def status(*, now: float | None = None) -> dict[str, Any]:
    """What `/update/status` returns. `state` is unknown until a check lands."""
    state = _load()
    manifest = state.get("manifest") if isinstance(state.get("manifest"), dict) else None
    on = enabled()
    url = manifest_url()
    b = banner(manifest) if on else None
    if not on:
        st = "disabled"
    elif not url:
        st = "unconfigured"
    elif manifest is None:
        st = "unknown"          # never reached the manifest (offline, or new)
    elif b is None:
        st = "current"
    else:
        st = "update_available"
    return {
        "ok": True,
        "state": st,
        "enabled": on,
        "url_configured": bool(url),
        "current": __version__,
        "latest": (manifest or {}).get("latest"),
        "manifest": manifest,
        "banner": b,
        "checked_at": state.get("checked_at"),
        "error": state.get("error"),
        "dismissed": state.get("dismissed"),
    }


def dismiss(version: str | None = None) -> dict[str, Any]:
    """Dismiss the banner for one version; a newer `latest` shows it again."""
    _save(dismissed=str(version) if version else None)
    return status()


# --------------------------------------------------------------------------
# startup wiring
# --------------------------------------------------------------------------
_timer: threading.Timer | None = None


def _tick() -> None:
    global _timer
    try:
        check()
    except Exception as exc:  # pragma: no cover - defensive by design
        print(f"[update_check] tick skipped ({exc}).")
    _schedule()


def _schedule() -> None:
    global _timer
    if not enabled() or not manifest_url():
        return
    every = max(60.0, float(settings.update_check.every_hours) * 3600.0)
    t = threading.Timer(every, _tick)
    t.daemon = True
    with _lock:
        _timer = t
    t.start()


def start_background() -> None:
    """Check once at startup, then every `every_hours`, off the request path.

    A boot with no network must be indistinguishable from a boot with one, so
    the first check runs on its own daemon thread — the server is serving long
    before the 3 s timeout could expire.
    """
    if not enabled() or not manifest_url():
        return
    t = threading.Thread(target=_tick, name="update-check", daemon=True)
    t.start()


def stop_background() -> None:
    global _timer
    with _lock:
        t, _timer = _timer, None
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass
