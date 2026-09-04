"""Owner account — password sign-in for returning browsers.

Single-tenant by design: one Mnemos instance serves one human, so this is a
single credential record (``data/.account.json``) plus a server-side session
store (``data/.web_sessions.json``), not a users table. It layers on top of
the LAN token gate in :mod:`app.services.api_auth`:

* the raw ``QUILL_API_TOKEN`` Bearer path keeps working (scripts, first run);
* once an account exists, browsers sign in at ``/auth`` with the password and
  get a random server-side session token in the same HttpOnly cookie;
* logout revokes just that session; sessions expire on their own.

Passwords are scrypt-hashed with a per-account salt. Session tokens are
stored hashed (SHA-256) so a leaked store file cannot be replayed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from app.config import settings

SESSION_TTL_REMEMBER_S = 60 * 60 * 24 * 30   # "remember me": 30 days
SESSION_TTL_SHORT_S = 60 * 60 * 12           # otherwise: 12 hours
MAX_SESSIONS = 50                            # oldest evicted beyond this

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1

_lock = threading.RLock()


def account_path() -> Path:
    return Path(settings.storage.data_dir) / ".account.json"


def sessions_path() -> Path:
    return Path(settings.storage.data_dir) / ".web_sessions.json"


def _read_json(path: Path) -> Any:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Credential record
# ---------------------------------------------------------------------------
def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
    ).hex()


def exists() -> bool:
    rec = _read_json(account_path())
    return bool(isinstance(rec, dict) and rec.get("pw_hash"))


def account_email() -> str:
    rec = _read_json(account_path())
    return (rec or {}).get("email", "") if isinstance(rec, dict) else ""


def create(password: str, *, email: str = "") -> dict[str, Any]:
    """Create the owner account. Refuses when one already exists."""
    password = (password or "").strip()
    if len(password) < 8:
        return {"ok": False, "error": "password must be at least 8 characters"}
    with _lock:
        if exists():
            return {"ok": False, "error": "an account already exists"}
        salt = secrets.token_bytes(16)
        rec = {
            "email": (email or "").strip().lower(),
            "salt": salt.hex(),
            "pw_hash": _hash_password(password, salt),
            "created_at": time.time(),
        }
        _write_json(account_path(), rec)
    return {"ok": True}


def verify_password(password: str) -> bool:
    rec = _read_json(account_path())
    if not isinstance(rec, dict) or not rec.get("pw_hash"):
        return False
    try:
        salt = bytes.fromhex(rec.get("salt") or "")
    except ValueError:
        return False
    candidate = _hash_password((password or "").strip(), salt)
    return hmac.compare_digest(candidate, rec["pw_hash"])


# ---------------------------------------------------------------------------
# Server-side sessions
# ---------------------------------------------------------------------------
def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").strip().encode("utf-8")).hexdigest()


def _load_sessions() -> list[dict[str, Any]]:
    rows = _read_json(sessions_path())
    return rows if isinstance(rows, list) else []


def _save_sessions(rows: list[dict[str, Any]]) -> None:
    _write_json(sessions_path(), rows)


def _prune(rows: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    live = [r for r in rows
            if isinstance(r, dict) and float(r.get("expires") or 0) > now]
    live.sort(key=lambda r: float(r.get("created") or 0))
    return live[-MAX_SESSIONS:]


def new_session(*, remember: bool = True, label: str = "") -> str:
    """Mint a session token; only its hash is persisted. Returns the token."""
    token = "s_" + secrets.token_urlsafe(32)
    now = time.time()
    ttl = SESSION_TTL_REMEMBER_S if remember else SESSION_TTL_SHORT_S
    with _lock:
        rows = _prune(_load_sessions(), now)
        rows.append({
            "th": _token_hash(token),
            "created": now,
            "expires": now + ttl,
            "label": (label or "")[:120],
        })
        _save_sessions(rows)
    return token


def session_valid(token: str | None) -> bool:
    if not (token or "").strip():
        return False
    th = _token_hash(token)
    now = time.time()
    for r in _load_sessions():
        if not isinstance(r, dict):
            continue
        if float(r.get("expires") or 0) <= now:
            continue
        if hmac.compare_digest(r.get("th") or "", th):
            return True
    return False


def revoke_session(token: str | None) -> None:
    if not (token or "").strip():
        return
    th = _token_hash(token)
    with _lock:
        rows = [r for r in _load_sessions()
                if isinstance(r, dict) and not hmac.compare_digest(
                    r.get("th") or "", th)]
        _save_sessions(rows)


def revoke_all_sessions() -> None:
    with _lock:
        _save_sessions([])


# ---------------------------------------------------------------------------
# Brute-force throttle (in-memory; per client key)
# ---------------------------------------------------------------------------
_FAIL_WINDOW_S = 15 * 60
_FAIL_MAX = 10
_failures: dict[str, list[float]] = {}


def throttle_ok(key: str) -> bool:
    now = time.time()
    with _lock:
        hits = [t for t in _failures.get(key or "?", [])
                if now - t < _FAIL_WINDOW_S]
        _failures[key or "?"] = hits
        return len(hits) < _FAIL_MAX


def record_failure(key: str) -> None:
    with _lock:
        _failures.setdefault(key or "?", []).append(time.time())


def reset_throttle() -> None:
    with _lock:
        _failures.clear()


def status() -> dict[str, Any]:
    """Public shape for /auth/account — never leaks the email or hashes."""
    return {"configured": exists()}
