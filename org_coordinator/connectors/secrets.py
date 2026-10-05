"""Connector secrets at rest: AES-256-GCM under QUILL_ORG_SECRETS_KEY.

OAuth tokens and API keys never sit in Postgres as plaintext. The ciphertext
is bound to its org and connector id as associated data, so a row copied onto
another connector (or another org) fails to decrypt instead of quietly
working. Rotating the key = re-encrypting every connectors.secret_enc.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any


class SecretsError(RuntimeError):
    pass


def _key() -> bytes:
    raw = (os.environ.get("QUILL_ORG_SECRETS_KEY") or "").strip()
    if not raw:
        raise SecretsError("QUILL_ORG_SECRETS_KEY is not set")
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise SecretsError("QUILL_ORG_SECRETS_KEY must be base64") from exc
    if len(key) != 32:
        raise SecretsError("QUILL_ORG_SECRETS_KEY must decode to 32 bytes")
    return key


def _aad(org_id: str, connector_id: str) -> bytes:
    return f"sparrow-connector:{org_id}:{connector_id}".encode()


def seal(secret: dict[str, Any], *, org_id: str, connector_id: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, json.dumps(secret).encode(),
                                _aad(org_id, connector_id))
    return "v1:" + base64.b64encode(nonce + ct).decode()


def open_(sealed: str | None, *, org_id: str,
          connector_id: str) -> dict[str, Any]:
    if not sealed:
        return {}
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not sealed.startswith("v1:"):
        raise SecretsError("unknown secret format")
    blob = base64.b64decode(sealed[3:])
    try:
        pt = AESGCM(_key()).decrypt(blob[:12], blob[12:],
                                    _aad(org_id, connector_id))
    except InvalidTag as exc:
        raise SecretsError("secret does not belong to this connector") from exc
    return json.loads(pt)
