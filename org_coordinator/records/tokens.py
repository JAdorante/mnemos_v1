"""Secrets, credentials and short-lived access tokens (HS256 JWT, stdlib).

Three kinds of bearer material, each `<org_id>.<id>.<secret>` so the service
can bind the tenant BEFORE any lookup (RLS needs app.org_id first):

  invite code     one-time, 7 days, redeemed by a node at /join
  node credential long-lived, bound to (member, node); exchanged at
                  /auth/token; revoked on departure
  access token    JWT, 15 minutes, carries org/member/node; every API call

Only sha256 of a secret is stored. Secrets are 32 random bytes, so a plain
hash (no KDF) is the right tool — there is nothing to brute-force.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

ACCESS_TTL_S = 15 * 60.0
INVITE_TTL_S = 7 * 86400.0


class TokenError(ValueError):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def compose(org_id: str, row_id: str, secret: str) -> str:
    return f"{org_id}.{row_id}.{secret}"


def split(material: str) -> tuple[str, str, str]:
    parts = (material or "").strip().split(".", 2)
    if len(parts) != 3 or not all(parts):
        raise TokenError("malformed")
    return parts[0], parts[1], parts[2]


def secret_matches(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), stored_hash or "")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key() -> bytes:
    key = os.environ.get("QUILL_ORG_JWT_SECRET") or ""
    if len(key) < 32:
        raise TokenError("QUILL_ORG_JWT_SECRET must be set (>= 32 chars)")
    return key.encode("utf-8")


def sign(claims: dict[str, Any], *, ttl_s: float = ACCESS_TTL_S,
         now: float | None = None) -> tuple[str, float]:
    now = float(now if now is not None else time.time())
    exp = now + ttl_s
    body = {**claims, "iat": int(now), "exp": int(exp),
            "jti": secrets.token_hex(8), "iss": "sparrow-org"}
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"},
                           separators=(",", ":")).encode())
    payload = _b64(json.dumps(body, separators=(",", ":"),
                              sort_keys=True).encode())
    sig = _b64(hmac.new(_key(), f"{head}.{payload}".encode(),
                        hashlib.sha256).digest())
    return f"{head}.{payload}.{sig}", float(int(exp))


def verify(token: str, *, now: float | None = None) -> dict[str, Any]:
    try:
        head, payload, sig = (token or "").split(".")
    except ValueError as exc:
        raise TokenError("malformed") from exc
    want = _b64(hmac.new(_key(), f"{head}.{payload}".encode(),
                         hashlib.sha256).digest())
    if not hmac.compare_digest(want, sig):
        raise TokenError("bad signature")
    try:
        hdr = json.loads(_unb64(head))
        body = json.loads(_unb64(payload))
    except ValueError as exc:
        raise TokenError("malformed") from exc
    if hdr.get("alg") != "HS256" or body.get("iss") != "sparrow-org":
        raise TokenError("wrong algorithm or issuer")
    now = float(now if now is not None else time.time())
    if float(body.get("exp") or 0) <= now:
        raise TokenError("expired")
    return body
