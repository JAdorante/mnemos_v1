"""LAN / open-bind API gate.

When the server is bound beyond loopback (e.g. QUILL_HOST=0.0.0.0 for phone
pairing), almost every route used to be reachable by anyone on the network.
This module requires a shared secret (QUILL_API_TOKEN, or a generated file at
data/.api_token) for non-loopback clients.

Phone device endpoints keep their own Bearer device tokens. Pairing claim and
the phone setup HTML stay open so a phone can finish pairing. Browser UIs unlock
via POST /auth/unlock, which sets an HttpOnly session cookie.

Plan 6.3: the cookie stores an HMAC-derived session token
(HMAC-SHA256(salt, api_token)), never the raw LAN token — so cookie theft
cannot be replayed as `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings

COOKIE_NAME = "quill_api_session"
COOKIE_MAX_AGE_S = 60 * 60 * 24 * 30

# Plan 6.4 — double-submit CSRF (readable by JS) + Origin/Referer check.
CSRF_COOKIE = "quill_csrf"
CSRF_HEADER = "x-csrf-token"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CSRF_EXACT_EXEMPT = frozenset({
    "/auth/unlock",          # establishes session; no prior CSRF cookie
    "/phone/pair/claim",     # single-use pairing code is the auth
    "/peer/pair/claim",
})

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
    "/static",        # brand assets on welcome / chrome before session unlock
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
    """True when `candidate` is the raw LAN API token (Bearer path)."""
    expected = get_api_token()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate.strip(), expected)


def session_salt_path() -> Path:
    return Path(settings.storage.data_dir) / ".session_salt"


def get_session_salt() -> str:
    """Stable salt for session HMAC. Env wins; else durable file under data/."""
    env = (os.environ.get("QUILL_SESSION_SALT") or "").strip()
    if env:
        return env
    path = session_salt_path()
    try:
        if path.is_file():
            salt = path.read_text(encoding="utf-8").strip()
            if salt:
                return salt
    except OSError:
        pass
    # Mint once so cookie values stay stable across restarts.
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_urlsafe(32)
    try:
        path.write_text(salt + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return salt


def session_token(api_token: str | None = None, *, salt: str | None = None) -> str:
    """HMAC-SHA256(salt, api_token) hex — what the browser cookie holds (6.3)."""
    token = (api_token if api_token is not None else get_api_token()) or ""
    s = (salt if salt is not None else get_session_salt()) or ""
    if not token or not s:
        return ""
    return hmac.new(
        s.encode("utf-8"),
        token.strip().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def session_matches(candidate: str | None) -> bool:
    """True when `candidate` is the derived session token (cookie path)."""
    expected = session_token()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate.strip(), expected)


def apply_session_cookie(response: Response, api_token: str | None = None) -> str:
    """Set HttpOnly/SameSite cookie to the derived session token. Returns it."""
    value = session_token(api_token)
    response.set_cookie(
        key=COOKIE_NAME,
        value=value,
        httponly=True,
        samesite="strict",
        max_age=COOKIE_MAX_AGE_S,
        path="/",
    )
    return value


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def mint_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def apply_csrf_cookie(response: Response, token: str | None = None) -> str:
    """Set non-HttpOnly CSRF cookie (double-submit; JS reads + sends header)."""
    value = (token or mint_csrf_token()).strip()
    response.set_cookie(
        key=CSRF_COOKIE,
        value=value,
        httponly=False,
        samesite="strict",
        max_age=COOKIE_MAX_AGE_S,
        path="/",
    )
    return value


def csrf_enabled() -> bool:
    return os.environ.get("QUILL_CSRF", "1") not in ("0", "false", "False")


def _host_only(host: str | None) -> str:
    """Normalize Host / netloc: lowercase, strip default ports."""
    h = (host or "").strip().lower()
    if not h:
        return ""
    # Drop userinfo if somehow present
    if "@" in h:
        h = h.rsplit("@", 1)[-1]
    if h.endswith(":80") and h.count(":") == 1:
        h = h[:-3]
    if h.endswith(":443") and h.count(":") == 1:
        h = h[:-4]
    return h


def _netloc_from_url(url: str | None) -> str:
    if not (url or "").strip():
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(url.strip())
        if not p.scheme or not p.netloc:
            return ""
        return _host_only(p.netloc)
    except Exception:
        return ""


def origin_ok(request: Request) -> bool:
    """True when Origin (or Referer) matches this request's Host."""
    host = _host_only(request.headers.get("host"))
    if not host:
        return False
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        # "null" is opaque origin (sandboxed) — never trust.
        if origin.lower() == "null":
            return False
        return _netloc_from_url(origin) == host
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        return _netloc_from_url(referer) == host
    return False


def csrf_header_ok(request: Request) -> bool:
    """Double-submit: X-CSRF-Token header must equal quill_csrf cookie."""
    cookie = (request.cookies.get(CSRF_COOKIE) or "").strip()
    header = (
        request.headers.get(CSRF_HEADER)
        or request.headers.get("x-mnemos-csrf")
        or ""
    ).strip()
    if not cookie or not header:
        return False
    return hmac.compare_digest(cookie, header)


def csrf_path_exempt(path: str) -> bool:
    if path in _CSRF_EXACT_EXEMPT:
        return True
    # Device/peer Bearer routes enforce their own auth — not browser cookie CSRF.
    for prefix in _PREFIX_EXEMPT:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def csrf_applies(request: Request) -> bool:
    """Whether this request must pass Origin or CSRF-token check."""
    if not csrf_enabled():
        return False
    method = (request.method or "").upper()
    if method not in _UNSAFE_METHODS:
        return False
    path = request.url.path
    if csrf_path_exempt(path):
        return False
    # Raw LAN Bearer (API clients / scripts) is not a cookie CSRF vector.
    if token_matches(_bearer_token(request.headers.get("authorization"))):
        return False
    return True


def csrf_ok(request: Request) -> bool:
    """Origin match OR double-submit CSRF header (plan 6.4)."""
    return origin_ok(request) or csrf_header_ok(request)


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def request_authorized(request: Request) -> bool:
    # Bearer: raw LAN token (API clients / scripts).
    if token_matches(_bearer_token(request.headers.get("authorization"))):
        return True
    # Cookie: derived session token only (plan 6.3) — never the raw token.
    return session_matches(request.cookies.get(COOKIE_NAME))


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
    return False


class CsrfProtectMiddleware(BaseHTTPMiddleware):
    """Plan 6.4 — reject cross-origin state-changing requests.

    Applies to POST/PUT/PATCH/DELETE unless the path is CSRF-exempt or the
    caller presents the raw LAN Bearer token. Passes when Origin/Referer
    matches Host, or when the double-submit CSRF header matches the cookie.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if csrf_applies(request) and not csrf_ok(request):
            return JSONResponse(
                {"detail": "CSRF rejected: cross-origin or missing token"},
                status_code=403,
            )
        response = await call_next(request)
        # Ensure browsers always have a CSRF cookie for subsequent POSTs.
        try:
            if CSRF_COOKIE not in (request.cookies or {}):
                apply_csrf_cookie(response)
        except Exception:
            pass
        return response


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
