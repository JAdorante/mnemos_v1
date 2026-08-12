"""Node auth for the Org Coordinator — bearer tokens minted at registration."""
from __future__ import annotations

import hashlib
from typing import Any

from fastapi import Header, HTTPException

from org_coordinator import store


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def authenticate(authorization: str | None) -> dict[str, Any]:
    """Resolve Authorization: Bearer <token> to a directory node."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    if len(token) < 16:
        raise HTTPException(status_code=401, detail="invalid token")
    digest = hash_token(token)
    for node in store.list_nodes().values():
        if node.get("token_sha256") == digest:
            return node
    raise HTTPException(status_code=401, detail="unknown node token")


def require_node(authorization: str | None = Header(None)) -> dict[str, Any]:
    return authenticate(authorization)
