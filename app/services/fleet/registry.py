"""Per-agent identity for the owner's fleet.

Same token pattern as mcp_tools.py, but one token per agent so every signal
names its producer. Only the SHA-256 of a token is stored; the plaintext is
returned once by `register_agent` and never again.

Roles: "publisher" may POST /fleet/publish, "subscriber" may read the stream,
"both" may do either. An agent publishes and reads only its registered
topics, whatever the query string asks for.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

from app.config import settings
from app.services.fleet import _files

ROLES = ("publisher", "subscriber", "both")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _path() -> str:
    return settings.fleet.agents_path


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _load() -> dict:
    data = _files.load(_path(), {})
    return data if isinstance(data, dict) else {}


def _public(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k != "token_sha256"}


def _clean_topics(topics) -> list[str]:
    if isinstance(topics, str):
        topics = [t for t in topics.split(",")]
    out = []
    for t in topics or []:
        t = str(t).strip().lower()
        if not _TOPIC_RE.match(t):
            raise ValueError(f"bad topic {t!r}")
        if t not in out:
            out.append(t)
    if not out:
        raise ValueError("an agent needs at least one topic")
    return out


def register_agent(name: str, topics, role: str = "both") -> dict:
    """Mint a token for `name`. Returns the record plus the plaintext token,
    which is not recoverable afterwards. Re-registering a name rotates its
    token (the old one stops working immediately)."""
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise ValueError("agent name must be lowercase letters, digits, - or _")
    role = (role or "both").strip().lower()
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    clean = _clean_topics(topics)
    token = "fa_" + secrets.token_urlsafe(32)
    with _files.lock:
        reg = _load()
        prev = reg.get(name) or {}
        rec = {
            "name": name, "role": role, "topics": clean,
            "token_sha256": _hash(token),
            "created_at": prev.get("created_at") or time.time(),
            "rotated_at": time.time() if prev else None,
            "revoked": False,
        }
        reg[name] = rec
        _files.save(_path(), reg)
    return {**_public(rec), "token": token}


def revoke_agent(name: str) -> bool:
    with _files.lock:
        reg = _load()
        rec = reg.get((name or "").strip().lower())
        if not rec:
            return False
        rec["revoked"] = True
        rec["revoked_at"] = time.time()
        rec["token_sha256"] = ""
        _files.save(_path(), reg)
    return True


def list_agents() -> list[dict]:
    return [_public(r) for r in sorted(_load().values(),
                                       key=lambda r: r.get("name", ""))]


def get_agent(name: str) -> dict | None:
    rec = _load().get((name or "").strip().lower())
    return _public(rec) if rec else None


def authenticate(authorization: str | None) -> dict | None:
    """Resolve `Authorization: Bearer <token>` to a live agent record."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if len(token) < 16:
        return None
    want = _hash(token)
    for rec in _load().values():
        have = rec.get("token_sha256") or ""
        if have and not rec.get("revoked") and hmac.compare_digest(have, want):
            return _public(rec)
    return None


def can_publish(agent: dict, topic: str) -> bool:
    return (agent.get("role") in ("publisher", "both")
            and topic in (agent.get("topics") or []))


def can_read(agent: dict) -> bool:
    return agent.get("role") in ("subscriber", "both")
