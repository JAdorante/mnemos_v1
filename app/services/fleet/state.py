"""This Sparrow's fleet identity and its relay credentials.

`instance_tag` prefixes every origin_id this Sparrow mints, so a person can
read a log line and see where a signal started. Own-origin detection does not
trust the prefix (a peer could forge it); dedup.py keeps a ledger instead.

The relay block holds the node token this Sparrow presents to the relay
(plaintext, like org_client's state, because it is sent as a Bearer) and only
the SHA-256 of the inbound token the relay presents back to us.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from app.config import settings
from app.services.fleet import _files


def _load() -> dict:
    data = _files.load(settings.fleet.state_path, {})
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    _files.save(settings.fleet.state_path, data)


def instance_tag() -> str:
    with _files.lock:
        st = _load()
        tag = st.get("instance_tag")
        if not tag:
            tag = "s" + secrets.token_hex(4)
            st["instance_tag"] = tag
            _save(st)
        return tag


def relay() -> dict:
    """{url, node_id, token, inbound_token_sha256, registered_at} or {}."""
    r = _load().get("relay")
    return r if isinstance(r, dict) else {}


def set_relay(**fields) -> dict:
    with _files.lock:
        st = _load()
        cur = st.get("relay") if isinstance(st.get("relay"), dict) else {}
        cur.update(fields)
        st["relay"] = cur
        _save(st)
        return cur


def clear_relay() -> None:
    with _files.lock:
        st = _load()
        st.pop("relay", None)
        _save(st)


def relay_url() -> str:
    return (relay().get("url") or settings.fleet.relay_url or "").rstrip("/")


def inbound_key() -> str:
    """The stored SHA-256 of the relay's inbound token — also the HMAC key
    the relay signs forwarded signals with (envelope.link_key)."""
    return relay().get("inbound_token_sha256") or ""


def inbound_token_matches(authorization: str | None) -> bool:
    want = inbound_key()
    if not want or not authorization:
        return False
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    have = hashlib.sha256(parts[1].strip().encode("utf-8")).hexdigest()
    return hmac.compare_digest(have, want)
