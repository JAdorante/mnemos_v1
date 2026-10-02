"""Relay topics and information barriers (fleet federation, Phase 4).

A topic lists its member nodes and the barrier groups it admits. A node may
publish to or receive from a topic only when BOTH hold: it is a member, and
its barrier group (assigned by an admin, never self-declared) is admitted.
A node with no group sees nothing. Membership changes are admin-only.

Admin and compliance credentials come from the environment
(QUILL_RELAY_ADMIN_TOKEN, QUILL_RELAY_COMPLIANCE_TOKEN). When one is unset,
its endpoints refuse every caller rather than open up.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time

from fastapi import Header, HTTPException

from org_coordinator import store

_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def _topics() -> dict:
    data = store._load("topics.json", {})
    return data if isinstance(data, dict) else {}


def _groups() -> dict:
    data = store._load("barrier_groups.json", {})
    return data if isinstance(data, dict) else {}


def list_topics() -> dict:
    with store._lock:
        return _topics()


def set_topic(name: str, *, members: list[str], groups: list[str]) -> dict:
    name = (name or "").strip().lower()
    if not _TOPIC_RE.match(name):
        raise ValueError(f"bad topic {name!r}")
    clean_groups = []
    for g in groups or []:
        g = str(g).strip().lower()
        if not _GROUP_RE.match(g):
            raise ValueError(f"bad barrier group {g!r}")
        if g not in clean_groups:
            clean_groups.append(g)
    clean_members = sorted({str(m).strip() for m in members or [] if str(m).strip()})
    with store._lock:
        topics = _topics()
        row = {"name": name, "members": clean_members, "groups": clean_groups,
               "created_at": (topics.get(name) or {}).get("created_at")
               or time.time(),
               "updated_at": time.time()}
        topics[name] = row
        store._save("topics.json", topics)
    return row


def delete_topic(name: str) -> bool:
    with store._lock:
        topics = _topics()
        if topics.pop((name or "").strip().lower(), None) is None:
            return False
        store._save("topics.json", topics)
    return True


def set_node_group(node_id: str, group: str | None) -> dict:
    """Assign (or clear, with None/"") a node's barrier group."""
    if store.get_node(node_id) is None:
        raise KeyError(node_id)
    g = (group or "").strip().lower()
    if g and not _GROUP_RE.match(g):
        raise ValueError(f"bad barrier group {g!r}")
    with store._lock:
        groups = _groups()
        if g:
            groups[node_id] = g
        else:
            groups.pop(node_id, None)
        store._save("barrier_groups.json", groups)
    return {"node_id": node_id, "group": g or None}


def node_group(node_id: str) -> str | None:
    return _groups().get(node_id) or None


def allowed(node_id: str, topic: str) -> bool:
    row = _topics().get(topic)
    if not row:
        return False
    group = node_group(node_id)
    return (group is not None and node_id in row.get("members", [])
            and group in row.get("groups", []))


def recipients(topic: str, sender_id: str) -> list[str]:
    row = _topics().get(topic) or {}
    return [m for m in row.get("members", [])
            if m != sender_id and allowed(m, topic)]


def topics_for(node_id: str) -> list[str]:
    return sorted(t for t in _topics() if allowed(node_id, t))


# --- role tokens -----------------------------------------------------------------
def _role_token_ok(env_key: str, authorization: str | None) -> bool:
    want = os.environ.get(env_key, "")
    if len(want) < 16 or not authorization:
        return False
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    a = hashlib.sha256(parts[1].strip().encode("utf-8")).digest()
    b = hashlib.sha256(want.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


def require_admin(authorization: str | None = Header(None)) -> None:
    if not _role_token_ok("QUILL_RELAY_ADMIN_TOKEN", authorization):
        raise HTTPException(status_code=401, detail="relay admin token required")


def require_compliance(authorization: str | None = Header(None)) -> None:
    if not _role_token_ok("QUILL_RELAY_COMPLIANCE_TOKEN", authorization):
        raise HTTPException(status_code=401,
                            detail="compliance token required")
