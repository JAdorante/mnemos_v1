"""Guided iCloud account connection — validate, then write the credentials file.

Public-product flow: a user should never hand-edit a dotfile. The UI collects
their Apple ID + an APP-SPECIFIC password (never the real Apple password —
Apple's own basic-auth endpoints reject real passwords on 2FA accounts anyway),
this module PROVES the pair works with a live CalDAV probe against Apple's
server, and only then upserts it into the credentials file (the same
QUILL_CREDENTIALS_FILE app/config.py loads at boot). The password is write-only
from the UI's perspective: status reports a masked account name, never secrets.

Revocation is Apple-side and instant: deleting the app-specific password at
appleid.apple.com cuts Sparrow off, whatever state the file is in.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

CALDAV_ROOT = "https://caldav.icloud.com/"
_USER_KEY = "QUILL_ICLOUD_USER"
_PASS_KEY = "QUILL_ICLOUD_APP_PASSWORD"

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<propfind xmlns="DAV:"><prop><current-user-principal/></prop></propfind>'
)


def _cred_path() -> Path:
    """Same resolution rule as app/config.py: env override, else project root."""
    raw = os.environ.get("QUILL_CREDENTIALS_FILE", ".credentials.env")
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path(__file__).resolve().parents[2] / raw


def verify(user: str, app_password: str) -> dict:
    """Prove the credentials against Apple's CalDAV endpoint. No side effects."""
    user = (user or "").strip()
    app_password = (app_password or "").strip()
    if "@" not in user:
        return {"ok": False, "error": "enter the Apple ID email address"}
    if len(app_password) < 8:
        return {"ok": False, "error": "that doesn't look like an app-specific "
                                      "password (format: xxxx-xxxx-xxxx-xxxx)"}
    try:
        r = requests.request(
            "PROPFIND", CALDAV_ROOT, auth=(user, app_password),
            data=_PROPFIND_BODY,
            headers={"Depth": "0", "Content-Type": "text/xml; charset=utf-8"},
            timeout=20, allow_redirects=True)
    except requests.RequestException as exc:
        return {"ok": False, "error": f"could not reach Apple ({exc.__class__.__name__}) "
                                      "— check the internet connection"}
    if r.status_code in (200, 207):
        return {"ok": True}
    if r.status_code in (401, 403):
        return {"ok": False, "error": "Apple rejected the sign-in. Check the "
                                      "email, and make sure this is an APP-SPECIFIC "
                                      "password from appleid.apple.com — the real "
                                      "Apple password will not work here."}
    return {"ok": False, "error": f"unexpected reply from Apple (HTTP {r.status_code})"}


def _upsert(lines: list[str], key: str, value: str) -> list[str]:
    """Replace `key=` line in place, or append; every other line untouched."""
    out, done = [], False
    for ln in lines:
        if ln.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    return out


def save(user: str, app_password: str) -> str:
    """Upsert the two keys into the credentials file; apply to the live env."""
    p = _cred_path()
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else [
        "# Secrets that should never live in .env or code. Loaded automatically",
        "# at startup (see app/config.py QUILL_CREDENTIALS_FILE).",
    ]
    lines = _upsert(lines, _USER_KEY, user.strip())
    lines = _upsert(lines, _PASS_KEY, app_password.strip())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Take effect now — no restart needed for services that read the env.
    os.environ[_USER_KEY] = user.strip()
    os.environ[_PASS_KEY] = app_password.strip()
    return str(p)


def connect(user: str, app_password: str) -> dict:
    """The one UI entry point: verify against Apple, then persist."""
    res = verify(user, app_password)
    if not res.get("ok"):
        return res
    path = save(user, app_password)
    return {"ok": True, "user": _mask(user), "stored_in": path}


def _read_saved() -> tuple[str, str]:
    """Current values, file first (the durable truth), env as fallback."""
    user = pwd = ""
    p = _cred_path()
    if p.is_file():
        try:
            for ln in p.read_text(encoding="utf-8").splitlines():
                key, _, val = ln.partition("=")
                if key.strip() == _USER_KEY:
                    user = val.strip()
                elif key.strip() == _PASS_KEY:
                    pwd = val.strip()
        except OSError:
            pass
    return (user or os.environ.get(_USER_KEY, ""),
            pwd or os.environ.get(_PASS_KEY, ""))


def _mask(user: str) -> str:
    name, _, domain = (user or "").partition("@")
    if not domain:
        return "***"
    return (name[:1] + "***@" + domain) if name else "***@" + domain


def status() -> dict:
    """Connected or not, masked identity — never a secret."""
    user, pwd = _read_saved()
    return {"connected": bool(user and pwd),
            "user": _mask(user) if user else ""}


def disconnect() -> dict:
    """Blank the stored keys (and the live env). The file's other lines stay."""
    p = _cred_path()
    if p.is_file():
        lines = p.read_text(encoding="utf-8").splitlines()
        lines = _upsert(lines, _USER_KEY, "")
        lines = _upsert(lines, _PASS_KEY, "")
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.pop(_USER_KEY, None)
    os.environ.pop(_PASS_KEY, None)
    return {"ok": True, "connected": False}
