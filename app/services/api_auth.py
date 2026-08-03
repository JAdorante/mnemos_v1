"""LAN / open-bind API gate.

When the server is bound beyond loopback (e.g. QUILL_HOST=0.0.0.0 for phone
pairing), almost every route used to be reachable by anyone on the network.
This module requires a shared secret (QUILL_API_TOKEN, or a generated file at
data/.api_token) for non-loopback clients.

Phone device endpoints keep their own Bearer device tokens. Pairing claim and
the phone setup HTML stay open so a phone can finish pairing. Browser UIs unlock
via POST /auth/unlock, which sets an HttpOnly session cookie.
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings

COOKIE_NAME = "quill_api_session"

# Paths that must stay reachable without the LAN API token.
_EXACT_EXEMPT = frozenset({
    "/auth",
    "/auth/unlock",
    "/auth/status",
    "/auth/logout",
    "/welcome/status",
    "/onboarding/status",
    "/phone/pair/claim",
    "/peer/pair/claim",   # peer pairing: the single-use code IS the auth
    "/docs",
    "/openapi.json",
    "/redoc",
})

# Method-agnostic prefixes: these routes enforce their own device/peer Bearer.
_PREFIX_EXEMPT = (
    "/phone/ingest",
    "/phone/photo",
    "/phone/sync",
    "/peer/ask",      # peer traffic uses per-peer Bearer tokens
    "/peer/answer",
)

# GET-only prefixes: POST siblings (e.g. /phone/outbox/queue) need the LAN token.
_PREFIX_EXEMPT_GET = (
    "/phone/outbox",  # GET drain uses device Bearer; enqueue is desktop-gated
)

# HTML shells the phone / local browser loads before (or without) a session.
_HTML_EXEMPT = frozenset({
    "/",
    "/welcome",
    "/today",
    "/shell",   # 301 → /today
    "/chat",
    "/ui",      # 301 → /chat
    "/memory",  # HTML Memory Console (JSON dump is /memory/events)
    "/console", # 301 → /memory
    "/profile",
    "/phone",
    "/phone/setup",
    "/peer",
    "/onboarding",
    "/auth",
    "/desktop-access",
})


def bind_is_loopback() -> bool:
    return settings.host in ("127.0.0.1", "localhost", "::1")


def client_is_loopback(host: str | None) -> bool:
    if not host:
        return False
    h = host.split("%", 1)[0].lower()
    # Starlette/FastAPI TestClient peers as "testclient" — not a network client.
    return h in ("127.0.0.1", "::1", "localhost", "testclient", "testserver")


def token_path() -> Path:
    return Path(settings.storage.data_dir) / ".api_token"


def get_api_token() -> str:
    """Env wins; else durable file under the data dir (created on demand)."""
    env = (os.environ.get("QUILL_API_TOKEN") or "").strip()
    if env:
        return env
    path = token_path()
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def ensure_api_token() -> str:
    """Guarantee a token exists when the bind is network-reachable."""
    existing = get_api_token()
    if existing:
        return existing
    if bind_is_loopback():
        return ""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(
        "[security] QUILL_HOST is not loopback and QUILL_API_TOKEN was unset — "
        f"wrote a gate token to {path}. Unlock LAN browsers at /auth, or set "
        "QUILL_API_TOKEN in .env to pin it."
    )
    return token


def token_matches(candidate: str | None) -> bool:
    expected = get_api_token()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate.strip(), expected)


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def request_authorized(request: Request) -> bool:
    if token_matches(_bearer_token(request.headers.get("authorization"))):
        return True
    return token_matches(request.cookies.get(COOKIE_NAME))


def path_is_exempt(path: str, method: str) -> bool:
    if path in _EXACT_EXEMPT:
        return True
    method_u = (method or "").upper()
    if method_u == "GET" and path in _HTML_EXEMPT:
        return True
    for prefix in _PREFIX_EXEMPT:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    if method_u == "GET":
        for prefix in _PREFIX_EXEMPT_GET:
            if path == prefix or path.startswith(prefix + "/"):
                return True
    # Phone status is polled by the setup page on the phone itself.
    if path == "/phone/status" and method_u == "GET":
        return True
    # #region agent log
    if path.startswith("/phone/outbox"):
        try:
            import json as _json, time as _time
            from pathlib import Path as _P
            with _P("debug-2e9950.log").open("a", encoding="utf-8") as _f:
                _f.write(_json.dumps({
                    "sessionId": "2e9950", "runId": "post-fix", "hypothesisId": "C1",
                    "location": "api_auth.path_is_exempt", "message": "outbox exempt check",
                    "data": {"path": path, "method": method_u, "exempt": False},
                    "timestamp": int(_time.time() * 1000),
                }) + "\n")
        except Exception:
            pass
    # #endregion
    return False


class LanApiAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Loopback clients (typical desktop UI via 127.0.0.1) stay open.
        client_host = request.client.host if request.client else None
        if client_is_loopback(client_host):
            return await call_next(request)

        # Strict only when bound for the network (phone / Tailscale case).
        if bind_is_loopback():
            return await call_next(request)

        path = request.url.path
        if path_is_exempt(path, request.method.upper()):
            return await call_next(request)

        if request_authorized(request):
            return await call_next(request)

        token = get_api_token()
        detail = (
            "LAN access requires Authorization: Bearer <QUILL_API_TOKEN> "
            "or an unlocked session at /auth"
        )
        if not token:
            detail = (
                "Server is bound beyond localhost but no API token is configured. "
                "Set QUILL_API_TOKEN or restart to generate data/.api_token."
            )
        return JSONResponse({"detail": detail}, status_code=401)
