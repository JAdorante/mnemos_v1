"""Sparrow front door for sparrow.ravenry.us.

Serves sign-in / sign-up, provisions one private Sparrow container per human,
and reverse-proxies every other request into the signed-in user's own seat.

Why a gateway instead of multi-user accounts in the app: the app is
single-tenant all the way down (one data dir, one account record, per-person
perception/peer/LoRA state). Tenancy therefore lives out here, and each user
gets a real instance rather than a row in a shared one.

The seat's upstream QUILL_API_TOKEN never reaches the browser. The gateway
injects it as a Bearer header on each hop, which clears the app's LAN gate
(api_auth.request_authorized). That same Bearer also exempts the hop from the
app's CSRF middleware, so the gateway performs the Origin check itself for
state-changing methods — see _origin_ok.
"""
from __future__ import annotations

import asyncio
import os
import re

import httpx
import websockets
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect

import pages
import provision
import store

COOKIE_NAME = "sparrow_gw"
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "sparrow.ravenry.us")
INVITE_CODE = os.environ.get("SIGNUP_INVITE_CODE", "").strip()
# Cookies are Secure by default: the only supported deployment is behind TLS.
# Set COOKIE_SECURE=0 only for a local http:// smoke test.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"
MIN_PASSWORD_LEN = 10
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Hop-by-hop headers must not be forwarded in either direction (RFC 9110).
_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

app = FastAPI(title="Sparrow Gateway")
_http: httpx.AsyncClient | None = None
_http_lock = asyncio.Lock()


async def http() -> httpx.AsyncClient:
    """Shared upstream client, built on first use.

    Lazy rather than a startup hook so the proxy works under any runner that
    does not drive the lifespan (TestClient without a context manager, for one).
    No read timeout: chat and /events are long-lived SSE streams that must not
    be cut off mid-answer.
    """
    global _http
    if _http is None:
        async with _http_lock:
            if _http is None:
                _http = httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=10.0, read=None,
                                          write=60.0, pool=10.0),
                    follow_redirects=False, max_redirects=0)
    return _http


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _http is not None:
        await _http.aclose()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def _client_key(request: Request) -> str:
    return request.client.host if request.client else "?"


def _current_user(request: Request) -> dict | None:
    email = store.session_email(request.cookies.get(COOKIE_NAME))
    return store.get_user(email) if email else None


def _set_session(response: Response, email: str, remember: bool) -> None:
    token = store.new_session(email, remember=remember)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=(store.SESSION_TTL_REMEMBER_S if remember
                 else store.SESSION_TTL_SHORT_S),
        httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/")


def _origin_ok(request: Request) -> bool:
    """Stand-in for the app's CSRF check, which our Bearer injection bypasses.

    A state-changing request must carry an Origin (or Referer) matching our own
    host. Same-origin fetches from the app's own pages always do; a cross-site
    form post does not.
    """
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return True
    raw = request.headers.get("origin") or request.headers.get("referer") or ""
    if not raw:
        # No Origin at all: browsers always send one on cross-origin writes,
        # so this is a non-browser client, which must authenticate anyway.
        return True
    host = request.headers.get("host", "")
    try:
        netloc = raw.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return False
    return netloc == host


# ---------------------------------------------------------------------------
# Front-door pages
# ---------------------------------------------------------------------------
@app.get("/signin", response_class=HTMLResponse)
def signin_page(request: Request) -> Response:
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(pages.SIGNIN_PAGE)


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request) -> Response:
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(pages.SIGNUP_PAGE)


@app.get("/provisioning", response_class=HTMLResponse)
def provisioning_page(request: Request) -> Response:
    if not _current_user(request):
        return RedirectResponse("/signin", status_code=303)
    return HTMLResponse(pages.PROVISIONING_PAGE)


@app.get("/gw-static/{name}")
def gw_static(name: str) -> Response:
    """Assets for the sign-in/sign-up pages, which are served before any seat
    exists. Confined to the image's own static dir — no user input reaches a
    path join beyond the basename."""
    safe = os.path.basename(name)
    path = os.path.join(os.path.dirname(__file__), "static", safe)
    if not os.path.isfile(path):
        return Response(status_code=404)
    return FileResponse(path)


@app.get("/api/config")
def api_config() -> dict:
    return {"invite_required": bool(INVITE_CODE),
            "public_host": PUBLIC_HOST}


class SignUpIn(BaseModel):
    email: str
    password: str
    invite: str = ""


class SignInIn(BaseModel):
    email: str
    password: str
    remember: bool = True


@app.post("/api/signup")
def api_signup(body: SignUpIn, response: Response) -> dict:
    email = store.normalize_email(body.email)
    if not _EMAIL_RE.match(email):
        return JSONResponse({"detail": "enter a valid email"}, status_code=400)
    if len(body.password) < MIN_PASSWORD_LEN:
        return JSONResponse(
            {"detail": f"password must be at least {MIN_PASSWORD_LEN} characters"},
            status_code=400)
    if INVITE_CODE and body.invite.strip() != INVITE_CODE:
        return JSONResponse({"detail": "invalid invite code"}, status_code=403)
    if store.get_user(email):
        return JSONResponse(
            {"detail": "an account with that email already exists"},
            status_code=409)
    try:
        seat, token = provision.create_seat()
    except RuntimeError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=503)
    except Exception:
        return JSONResponse(
            {"detail": "could not start your instance — try again shortly"},
            status_code=503)
    store.create_user(email, body.password, seat, token)
    _set_session(response, email, remember=True)
    return {"ok": True, "seat": seat}


@app.post("/api/signin")
def api_signin(body: SignInIn, request: Request, response: Response) -> dict:
    key = _client_key(request)
    if not store.throttle_ok(key):
        return JSONResponse(
            {"detail": "too many attempts — try again later"}, status_code=429)
    email = store.normalize_email(body.email)
    # One message for both "no such user" and "wrong password": a distinct
    # error would turn this form into an account-existence oracle.
    if not store.verify_password(email, body.password):
        store.record_failure(key)
        return JSONResponse(
            {"detail": "wrong email or password"}, status_code=401)
    rec = store.get_user(email) or {}
    try:
        provision.ensure_running(rec.get("seat", ""))
    except Exception:
        pass
    _set_session(response, email, remember=body.remember)
    return {"ok": True}


@app.post("/api/signout")
def api_signout(request: Request, response: Response) -> dict:
    store.revoke_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/seat/status")
async def api_seat_status(request: Request) -> Response:
    """Polled by the provisioning page until the seat answers /health."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"detail": "not signed in"}, status_code=401)
    seat = user["seat"]
    if not provision.seat_running(seat):
        return JSONResponse({"ready": False, "detail": "Starting container…"})
    try:
        client = await http()
        r = await client.get(f"{provision.seat_url(seat)}/health",
                            headers={"Authorization": f"Bearer {user['token']}"},
                            timeout=5.0)
        if r.status_code < 400:
            return JSONResponse({"ready": True})
    except Exception:
        pass
    return JSONResponse({"ready": False, "detail": "Warming up transcription…"})


# ---------------------------------------------------------------------------
# Audio ingest — WebSocket proxy
# ---------------------------------------------------------------------------
# Must be declared before the HTTP catch-all. Browsers cannot set an
# Authorization header on `new WebSocket()`, but this hop is server-to-server,
# so the seat token rides the upstream handshake (api_auth.ws_request_authorized
# accepts the raw Bearer).
@app.websocket("/ingest/audio")
async def ws_ingest(ws: WebSocket) -> None:
    email = store.session_email(ws.cookies.get(COOKIE_NAME))
    user = store.get_user(email) if email else None
    if not user:
        await ws.close(code=4401)
        return
    upstream = (provision.seat_url(user["seat"]).replace("http://", "ws://")
                + "/ingest/audio")
    await ws.accept()
    try:
        async with websockets.connect(
            upstream,
            additional_headers={"Authorization": f"Bearer {user['token']}"},
            max_size=None, open_timeout=20,
        ) as up:
            async def to_upstream() -> None:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(msg.get("code", 1000))
                    if msg.get("bytes") is not None:
                        await up.send(msg["bytes"])
                    elif msg.get("text") is not None:
                        await up.send(msg["text"])

            async def to_client() -> None:
                async for msg in up:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            done, pending = await asyncio.wait(
                {asyncio.create_task(to_upstream()),
                 asyncio.create_task(to_client())},
                return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Everything else — HTTP reverse proxy into the signed-in user's seat
# ---------------------------------------------------------------------------
@app.api_route("/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD",
                        "OPTIONS"])
async def proxy(path: str, request: Request) -> Response:
    user = _current_user(request)
    if not user:
        if request.method.upper() in {"GET", "HEAD"}:
            nxt = request.url.path
            if request.url.query:
                nxt += "?" + request.url.query
            suffix = f"?next={nxt}" if nxt not in ("/", "") else ""
            return RedirectResponse(f"/signin{suffix}", status_code=303)
        return JSONResponse({"detail": "not signed in"}, status_code=401)

    if not _origin_ok(request):
        return JSONResponse(
            {"detail": "CSRF rejected: cross-origin request"}, status_code=403)

    # Keys are lower-cased on the way in so the client's own "authorization"
    # cannot survive alongside the one we set — a dict treats "Authorization"
    # and "authorization" as two entries, and httpx would forward both.
    headers = {k.lower(): v for k, v in request.headers.items()
               if k.lower() not in _HOP and k.lower() != "authorization"}
    # The seat token is the credential for this hop; a client-supplied
    # Authorization is dropped above so nobody can present a token of their own.
    headers["authorization"] = f"Bearer {user['token']}"
    # Strip our session cookie so the gateway credential never reaches the app.
    cookie = "; ".join(
        f"{k}={v}" for k, v in request.cookies.items() if k != COOKIE_NAME)
    if cookie:
        headers["cookie"] = cookie
    else:
        headers.pop("cookie", None)
    headers["x-forwarded-proto"] = request.url.scheme
    headers["x-forwarded-host"] = request.headers.get("host", PUBLIC_HOST)

    url = f"{provision.seat_url(user['seat'])}/{path}"
    client = await http()
    req = client.build_request(
        request.method, url, headers=headers,
        params=request.url.query, content=request.stream())
    try:
        upstream = await client.send(req, stream=True)
    except httpx.ConnectError:
        return JSONResponse(
            {"detail": "your instance is starting — try again in a moment"},
            status_code=503)

    out = StreamingResponse(
        upstream.aiter_raw(), status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose))
    # raw_headers, not a dict: the app sets BOTH quill_api_session and
    # quill_csrf, and a dict would keep only the last Set-Cookie.
    # content-length is dropped because the body is re-chunked on this hop.
    out.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in upstream.headers.multi_items()
        if k.lower() not in _HOP and k.lower() != "content-length"
    ]
    return out

