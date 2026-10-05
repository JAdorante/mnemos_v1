"""One HTTP seam for every connector, so tests (and the contract suites) can
swap in an in-process fake of HubSpot / Google Docs / a webhook receiver.

A transport is `fn(method, url, headers, body: bytes | None) -> (status,
headers, body: bytes)`. Status mapping is shared: 429 and 5xx are transient,
any other 4xx is permanent.
"""
from __future__ import annotations

import json
from typing import Any, Callable
from urllib import error, request

from org_coordinator.connectors.base import PermanentError, TransientError

Transport = Callable[[str, str, dict, "bytes | None"], tuple[int, dict, bytes]]
_transport: Transport | None = None
TIMEOUT_S = 20.0


def set_transport(fn: Transport | None) -> None:
    global _transport
    _transport = fn


def _urllib(method: str, url: str, headers: dict,
            body: bytes | None) -> tuple[int, dict, bytes]:
    req = request.Request(url, data=body, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return int(resp.status), dict(resp.headers), resp.read()
    except error.HTTPError as exc:
        return int(exc.code), dict(exc.headers or {}), exc.read() or b""
    except (error.URLError, TimeoutError, OSError) as exc:
        raise TransientError(f"network: {exc}") from exc


def call(method: str, url: str, *, headers: dict | None = None,
         json_body: Any = None, form: dict | None = None,
         raw: bytes | None = None, ok: tuple[int, ...] = ()) -> tuple[int, Any]:
    """Send, classify, decode. Returns (status, decoded body)."""
    hdrs = dict(headers or {})
    body = raw
    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":")).encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        from urllib.parse import urlencode
        body = urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    status, _h, data = (_transport or _urllib)(method, url, hdrs, body)
    try:
        decoded = json.loads(data) if data else {}
    except ValueError:
        decoded = data.decode("utf-8", "replace")
    if status in ok or 200 <= status < 300:
        return status, decoded
    msg = f"{method} {url.split('?')[0]} -> {status}: {str(decoded)[:300]}"
    if status == 429 or status >= 500:
        raise TransientError(msg)
    raise PermanentError(msg)
