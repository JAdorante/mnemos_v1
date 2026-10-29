"""Remote MCP + review inbox routes (see services/remote_mcp.py).

  GET  /.well-known/oauth-protected-resource[/mcp]   RFC 9728
  GET  /.well-known/oauth-authorization-server       RFC 8414
  GET  /.well-known/openid-configuration             same doc (ChatGPT probes it)
  POST /oauth/mcp/register                           RFC 7591
  GET  /oauth/mcp/authorize                          consent screen
  POST /oauth/mcp/authorize                          consent decision
  POST /oauth/mcp/token                              code / refresh exchange
  POST /mcp                                          JSON-RPC (Bearer only)
  GET  /assistants                                   connect + inbox page
  GET  /assistants/status | /assistants/inbox
  POST /assistants/inbox/{id}/accept | /reject | /remove
  POST /assistants/connections/{id}/revoke
  POST /assistants/tokens                            static token, shown once

The /oauth/mcp/* and /mcp routes are exempt from the LAN gate and check
their own credentials — including on loopback, because a tunnel daemon on
the same machine makes every public request look local.
"""
from __future__ import annotations

import base64
import html
import os
import re
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.services import remote_mcp

router = APIRouter()


_PREFIX_RE = re.compile(r"^/mcp/[a-z0-9][a-z0-9-]{0,39}$")


def public_base(request: Request) -> str:
    """The base assistants reach us on. QUILL_MCP_PUBLIC_URL pins it (a
    named tunnel); else the live forwarded origin, as the connectors do,
    plus the gateway's per-seat mount (X-Sparrow-Mcp-Mount: /mcp/<seat>,
    deploy/gateway/gateway.py) when the hop came through it."""
    pinned = (os.environ.get("QUILL_MCP_PUBLIC_URL") or "").strip().rstrip("/")
    if pinned:
        return pinned
    from app.services.connectors import request_public_base
    base = (request_public_base(request)
            or str(request.base_url).rstrip("/"))
    prefix = (request.headers.get("x-sparrow-mcp-mount") or "").strip()
    return base + prefix if _PREFIX_RE.match(prefix) else base


def _gate() -> None:
    reason = remote_mcp.unavailable_reason()
    if reason:
        raise HTTPException(404, reason)


# --- discovery ----------------------------------------------------------------

def _resource_metadata(request: Request) -> dict[str, Any]:
    base = public_base(request)
    return {"resource": f"{base}/mcp", "authorization_servers": [base],
            "scopes_supported": list(remote_mcp.SCOPES),
            "bearer_methods_supported": ["header"],
            "resource_name": "Sparrow"}


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource(request: Request) -> dict:
    _gate()
    return _resource_metadata(request)


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
@router.get("/.well-known/openid-configuration")
def authorization_server(request: Request) -> dict:
    _gate()
    base = public_base(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/mcp/authorize",
        "token_endpoint": f"{base}/oauth/mcp/token",
        "registration_endpoint": f"{base}/oauth/mcp/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": list(remote_mcp.SCOPES),
    }


# --- OAuth ----------------------------------------------------------------------

@router.post("/oauth/mcp/register")
async def oauth_register(request: Request) -> JSONResponse:
    _gate()
    try:
        meta = await request.json()
    except Exception:
        meta = None
    if not isinstance(meta, dict):
        return JSONResponse({"error": "invalid_client_metadata"}, 400)
    body, status = remote_mcp.register_client(meta)
    return JSONResponse(body, status)


_AUTH_PARAMS = ("client_id", "redirect_uri", "state", "scope",
                "code_challenge", "code_challenge_method", "response_type",
                "resource")


def _redirect_with(uri: str, **params: str) -> RedirectResponse:
    sep = "&" if "?" in uri else "?"
    clean = {k: v for k, v in params.items() if v}
    return RedirectResponse(uri + sep + urlencode(clean), status_code=302)


def _validate_authorize(p: dict[str, str]) -> tuple[dict | None, Any]:
    """(client, None) when the request may proceed; else (None, response).
    Bad client or redirect_uri renders an error here — never redirect to an
    unverified URI. Everything after that redirects back with ?error=."""
    client = remote_mcp.get_client(p.get("client_id") or "")
    uri = p.get("redirect_uri") or ""
    if client is None:
        return None, _page("Unknown app", "<p>This connection request names "
                           "an app Sparrow has not registered. Start the "
                           "connection again from your assistant.</p>", 400)
    if uri not in client["redirect_uris"]:
        return None, _page("Redirect mismatch", "<p>The app asked to return "
                           "to an address it did not register.</p>", 400)
    state = p.get("state") or ""
    if p.get("response_type") != "code":
        return None, _redirect_with(uri, error="unsupported_response_type",
                                    state=state)
    if (p.get("code_challenge_method") != "S256"
            or not p.get("code_challenge")):
        return None, _redirect_with(uri, error="invalid_request", state=state,
                                    error_description="PKCE S256 required")
    return client, None


@router.get("/oauth/mcp/authorize")
def oauth_authorize(request: Request) -> Response:
    _gate()
    p = {k: request.query_params.get(k) or "" for k in _AUTH_PARAMS}
    client, bad = _validate_authorize(p)
    if bad is not None:
        return bad
    return _consent_page(request, client, p)


@router.post("/oauth/mcp/authorize")
async def oauth_authorize_decide(request: Request) -> Response:
    _gate()
    form = await request.form()
    p = {k: str(form.get(k) or "") for k in _AUTH_PARAMS}
    client, bad = _validate_authorize(p)
    if bad is not None:
        return bad
    if form.get("decision") != "allow":
        return _redirect_with(p["redirect_uri"], error="access_denied",
                              state=p["state"])
    if not _signed_in(request, str(form.get("credential") or "")):
        return _consent_page(request, client, p,
                             error="Sign-in failed — check your password or "
                                   "access token.")
    scope = " ".join(s for s in remote_mcp.SCOPES if form.get(f"scope_{s}"))
    if not scope:
        return _consent_page(request, client, p,
                             error="Choose at least one thing to allow.")
    code = remote_mcp.issue_code(client, redirect_uri=p["redirect_uri"],
                                 challenge=p["code_challenge"], scope=scope)
    return _redirect_with(p["redirect_uri"], code=code, state=p["state"])


def _signed_in(request: Request, credential: str) -> bool:
    """An unlocked Sparrow session cookie (same-origin POST carries it), or
    the account password / API token typed on the consent screen. Loopback
    is NOT trusted here — see the module docstring."""
    from app.services import account, api_auth
    if api_auth.request_authorized(request):
        return True
    credential = credential.strip()
    if not credential:
        return False
    if api_auth.token_matches(credential):
        return True
    if account.exists():
        key = request.client.host if request.client else "?"
        if not account.throttle_ok(key):
            return False
        if account.verify_password(credential):
            return True
        account.record_failure(key)
    return False


@router.post("/oauth/mcp/token")
async def oauth_token(request: Request) -> JSONResponse:
    _gate()
    form = {k: str(v) for k, v in (await request.form()).items()}
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("basic "):
        try:
            raw = base64.b64decode(auth[6:]).decode("utf-8")
            cid, _, secret = raw.partition(":")
            from urllib.parse import unquote
            form.setdefault("client_id", unquote(cid))
            form["client_secret"] = unquote(secret)
        except Exception:
            return JSONResponse({"error": "invalid_client"}, 401)
    body, status = remote_mcp.exchange(form)
    return JSONResponse(body, status, headers={"Cache-Control": "no-store",
                                               "Pragma": "no-cache"})


# --- MCP endpoint -------------------------------------------------------------

def _unauthorized(request: Request) -> JSONResponse:
    meta = f"{public_base(request)}/.well-known/oauth-protected-resource"
    return JSONResponse(
        {"error": "invalid_token"}, 401,
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    _gate()
    auth = request.headers.get("authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    grant = remote_mcp.verify_access(token)
    if grant is None:
        return _unauthorized(request)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(remote_mcp._rpc_error(None, -32700, "parse error"),
                            400)
    if isinstance(payload, list):
        out = [r for r in (remote_mcp.handle_message(m, grant)
                           for m in payload) if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    reply = remote_mcp.handle_message(payload, grant)
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(reply)


@router.get("/mcp")
@router.delete("/mcp")
def mcp_no_stream() -> Response:
    # Stateless JSON-only server: no server-initiated SSE stream, no sessions.
    return Response(status_code=405, headers={"Allow": "POST"})


# --- /assistants (behind the normal LAN gate) ---------------------------------

@router.get("/assistants", response_class=HTMLResponse)
def assistants_page() -> HTMLResponse:
    from app.api.assistants_page import ASSISTANTS_PAGE
    return HTMLResponse(ASSISTANTS_PAGE)


@router.get("/assistants/status")
def assistants_status(request: Request) -> dict:
    from app.services import assistant_inbox
    base = public_base(request)
    out: dict[str, Any] = {
        "enabled": remote_mcp.enabled(),
        "unavailable": remote_mcp.unavailable_reason(),
        "mcp_url": f"{base}/mcp",
        "https": base.startswith("https://"),
        "pinned": bool((os.environ.get("QUILL_MCP_PUBLIC_URL") or "").strip()),
        "ephemeral": "trycloudflare.com" in base,
    }
    if out["unavailable"] is None:
        out["connections"] = remote_mcp.connections()
        out["counts"] = assistant_inbox.counts()
    return out


@router.get("/assistants/inbox")
def assistants_inbox(status: str = "pending", limit: int = 200) -> dict:
    _gate()
    from app.services import assistant_inbox
    if status not in ("pending", "accepted", "rejected", "removed"):
        raise HTTPException(
            400, "status must be pending|accepted|rejected|removed")
    return {"items": assistant_inbox.items(status, limit)}


class AcceptIn(BaseModel):
    text: str | None = None
    project: str | None = None


@router.post("/assistants/inbox/{item_id}/accept")
def assistants_accept(item_id: int, body: AcceptIn | None = None) -> dict:
    _gate()
    from app.services import assistant_inbox
    body = body or AcceptIn()
    return assistant_inbox.accept(item_id, text=body.text, project=body.project)


@router.post("/assistants/inbox/{item_id}/reject")
def assistants_reject(item_id: int) -> dict:
    _gate()
    from app.services import assistant_inbox
    return assistant_inbox.reject(item_id)


@router.post("/assistants/inbox/{item_id}/remove")
def assistants_remove(item_id: int) -> dict:
    _gate()
    from app.services import assistant_inbox
    return assistant_inbox.remove(item_id)


@router.post("/assistants/connections/{grant_id}/revoke")
def assistants_revoke(grant_id: str) -> dict:
    _gate()
    return remote_mcp.revoke(grant_id)


class TokenIn(BaseModel):
    label: str = ""
    scope: str = "read capture"


@router.post("/assistants/tokens")
def assistants_mint_token(body: TokenIn) -> dict:
    _gate()
    return remote_mcp.mint_static_token(body.label, body.scope)


# --- consent page ---------------------------------------------------------------

def _page(title: str, inner: str, status: int = 200) -> HTMLResponse:
    from app.api.mnemos_theme import apply_plain
    doc = apply_plain(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@BRAND@@ — {html.escape(title)}</title>
@@FONTS@@
<style>
@@ROOT@@
*{{box-sizing:border-box}}
body{{margin:0;font:16px/1.55 var(--font);color:var(--text);background:var(--paper);
  display:flex;justify-content:center;padding:48px 16px}}
.card{{max-width:480px;width:100%;background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:28px;box-shadow:var(--shadow)}}
h1{{font-family:var(--display);font-weight:400;font-size:1.6rem;margin:0 0 8px;color:var(--navy)}}
.mut{{color:var(--mut);font-size:14px}}
label.opt{{display:flex;gap:10px;align-items:flex-start;margin:12px 0}}
input[type=password]{{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);
  background:var(--paper);color:var(--text);font:inherit}}
.row{{display:flex;gap:10px;margin-top:20px}}
button{{appearance:none;border:0;cursor:pointer;font:inherit;font-weight:600;border-radius:12px;
  padding:11px 20px;background:var(--acc);color:var(--acc-fg)}}
button.ghost{{background:transparent;color:var(--text);border:1px solid var(--line)}}
.err{{color:var(--danger);font-size:14px;margin:8px 0}}
code{{font-family:var(--mono);font-size:13px}}
</style></head><body><div class="card">{inner}</div></body></html>""")
    return HTMLResponse(doc, status_code=status, headers={
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'",
        "Cache-Control": "no-store"})


def _consent_page(request: Request, client: dict, p: dict[str, str],
                  *, error: str = "") -> HTMLResponse:
    from urllib.parse import urlparse
    from app.services import account
    esc = html.escape
    who = remote_mcp.ASSISTANT_LABELS.get(client["assistant"], "Other")
    name = client.get("client_name") or who
    host = urlparse(p["redirect_uri"]).hostname or "?"
    asked = set(remote_mcp.normalize_scope(p.get("scope")).split())
    hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(p[k])}">'
                     for k in _AUTH_PARAMS)
    from app.services import api_auth
    cred_label = ("your Sparrow password" if account.exists()
                  else "your Sparrow access token")
    # Signed in already (gateway hop, or the session cookie made it across):
    # no credential box. Otherwise ask; the strict cookie often does not ride
    # a cross-site navigation from the assistant, though it does ride the POST.
    signin = "" if api_auth.request_authorized(request) else f"""
<p class="mut" style="margin-top:18px">Not signed in on this browser? Enter {cred_label}.</p>
<input type="password" name="credential" autocomplete="current-password"
  aria-label="{cred_label}">"""
    checked = lambda s: " checked" if s in asked else ""  # noqa: E731
    inner = f"""
<h1>Connect {esc(name)} to Sparrow?</h1>
<p class="mut">Requested by <b>{esc(who)}</b> · returns to <code>{esc(host)}</code></p>
{f'<p class="err" role="alert">{esc(error)}</p>' if error else ''}
<form method="post" action="authorize">
{hidden}
<label class="opt"><input type="checkbox" name="scope_capture" value="1"{checked("capture")}>
<span><b>Save to your review inbox.</b> <span class="mut">It can propose notes from your
chats. Nothing becomes memory until you accept it.</span></span></label>
<label class="opt"><input type="checkbox" name="scope_read" value="1"{checked("read")}>
<span><b>Read your Sparrow memory.</b> <span class="mut">Search, people, open loops — filtered by
your disclosure policy; personal and sensitive items stay home. Results go to
{esc(who)}'s servers.</span></span></label>
{signin}
<div class="row">
<button type="submit" name="decision" value="allow">Allow</button>
<button type="submit" name="decision" value="deny" class="ghost">Deny</button>
</div>
<p class="mut" style="margin-top:16px">You can disconnect it any time on the Assistants page.</p>
</form>"""
    return _page("Connect assistant", inner)
