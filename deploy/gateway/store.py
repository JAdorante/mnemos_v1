"""Gateway identity store — the ONE multi-tenant table in the system.

The Sparrow app itself is single-tenant by construction (app/services/account.py:
one credential record, one session store, one data dir per instance). Rather than
rearchitect that, the gateway keeps tenancy entirely outside the app: this module
maps a human (email + password) to a SEAT, and a seat is a whole private Sparrow
container with its own volume and its own QUILL_API_TOKEN.

Two files, both 0600, both in the gateway's own volume:

* ``users.json``    — email -> {seat, scrypt hash, salt, created}
* ``sessions.json`` — sha256(token) -> {email, expires}

Session tokens are stored hashed so a leaked store file cannot be replayed as a
cookie, and the seat's upstream API token is NEVER sent to the browser: the
gateway injects it server-side on every proxied hop.
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

SESSION_TTL_REMEMBER_S = 60 * 60 * 24 * 30   # "keep me signed in"
SESSION_TTL_SHORT_S = 60 * 60 * 12
MAX_FAILURES = 8                             # per-IP sign-in throttle
FAILURE_WINDOW_S = 15 * 60

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1

_lock = threading.RLock()
_failures: dict[str, list[float]] = {}


def data_dir() -> Path:
    return Path(os.environ.get("GATEWAY_DATA_DIR", "/srv/gateway/data"))


def users_path() -> Path:
    return data_dir() / "users.json"


def sessions_path() -> Path:
    return data_dir() / "sessions.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            out = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(out, dict):
                return out
    except (OSError, ValueError):
        pass
    return {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
    ).hex()


def get_user(email: str) -> dict[str, Any] | None:
    return _read(users_path()).get(normalize_email(email))


def user_count() -> int:
    return len(_read(users_path()))


def create_user(email: str, password: str, seat: str, token: str) -> dict[str, Any]:
    """Record a new human and the seat minted for them. Caller has already
    validated the password and provisioned the container."""
    key = normalize_email(email)
    salt = secrets.token_bytes(16)
    with _lock:
        users = _read(users_path())
        if key in users:
            raise ValueError("account already exists")
        users[key] = {
            "email": key,
            "seat": seat,
            "token": token,          # upstream Bearer; never leaves the server
            "salt": salt.hex(),
            "hash": _hash_password(password, salt),
            "created": time.time(),
        }
        _write(users_path(), users)
        return users[key]


def verify_password(email: str, password: str) -> bool:
    rec = get_user(email)
    if not rec:
        return False
    try:
        salt = bytes.fromhex(rec["salt"])
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(_hash_password(password, salt), rec.get("hash", ""))


# ---------------------------------------------------------------------------
# Throttle — per-IP, so one scripted attacker cannot grind every account
# ---------------------------------------------------------------------------
def throttle_ok(key: str) -> bool:
    now = time.time()
    with _lock:
        hits = [t for t in _failures.get(key, []) if now - t < FAILURE_WINDOW_S]
        _failures[key] = hits
        return len(hits) < MAX_FAILURES


def record_failure(key: str) -> None:
    with _lock:
        _failures.setdefault(key, []).append(time.time())


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _prune(rows: dict[str, Any], now: float) -> dict[str, Any]:
    return {k: v for k, v in rows.items() if float(v.get("expires", 0)) > now}


def new_session(email: str, *, remember: bool = True) -> str:
    token = secrets.token_urlsafe(32)
    ttl = SESSION_TTL_REMEMBER_S if remember else SESSION_TTL_SHORT_S
    now = time.time()
    with _lock:
        rows = _prune(_read(sessions_path()), now)
        rows[_token_hash(token)] = {
            "email": normalize_email(email),
            "expires": now + ttl,
        }
        _write(sessions_path(), rows)
    return token


def session_email(token: str | None) -> str | None:
    """Resolve a cookie to its owner, or None. This is the ONLY way a request
    acquires a seat — a seat is never read off the request itself, so no user
    can address another user's container by fiddling with a path or header."""
    if not token:
        return None
    now = time.time()
    rec = _read(sessions_path()).get(_token_hash(token))
    if not rec or float(rec.get("expires", 0)) <= now:
        return None
    return str(rec.get("email") or "") or None


def revoke_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        rows = _prune(_read(sessions_path()), time.time())
        rows.pop(_token_hash(token), None)
        _write(sessions_path(), rows)
