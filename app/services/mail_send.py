"""Send-only mail grant — Gmail ``gmail.send`` / Graph ``Mail.Send``.

The Google and Outlook connectors stay read-only: their tokens carry exactly
the metadata scopes and ``assert_metadata_scopes`` refuses anything else. The
right to send is a SECOND, separate grant with its own token file, asked for
only when the user presses "Allow sending" on a debrief, and holding only the
send scope. Revoking it never touches the read connector, and the read token
can never send.

Both grants share the provider's one registered redirect URI
(``/oauth/{provider}/callback``) and its oauth-state file — so the callback
relay between rotating seat hostnames works unchanged — and are told apart by
``purpose: "send"`` on the state entry (``is_send_state``).

Off unless ``QUILL_MAIL_SEND=1``. Nothing here decides *whether* to send:
callers (meeting_debrief.send_selected) own the human-approval check.
"""
from __future__ import annotations

import base64
import json
import os
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import settings

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GRAPH_SEND_SCOPE = "Mail.Send"
# On Microsoft, consent is per app: once the read connector is consented, a
# token minted for Mail.Send may also list the read scopes. Those are already
# the user's; anything else is refused.
_GRAPH_TOLERATED = {"Mail.ReadBasic", "Calendars.Read"}
_GRAPH_PLATFORM = {"offline_access", "openid", "profile", "email", "User.Read"}

PROVIDERS = ("google", "outlook")
LABELS = {"google": "Gmail", "outlook": "Outlook"}


def enabled() -> bool:
    return os.environ.get("QUILL_MAIL_SEND", "0") not in ("0", "false", "False", "")


# --- paths ---------------------------------------------------------------------
def _state_path(provider: str) -> Path:
    """The connector's own oauth-state file — the callback relay reads it."""
    if provider == "google":
        from app.services import exhaust_ingest
        return exhaust_ingest._oauth_state_path()
    from app.services.connectors import outlook
    return outlook._oauth_state_path()


def _token_path(provider: str) -> Path:
    return _state_path(provider).parent / "send_token.json"


def _load_json(path: Path, default):
    from app.services.exhaust_ingest import _load_json as _lj
    return _lj(path, default)


def _save_json(path: Path, data) -> None:
    from app.services.exhaust_ingest import _save_json as _sj
    _sj(path, data)


def configured(provider: str) -> bool:
    if provider == "google":
        from app.services import exhaust_ingest as ex
        return ex.oauth_configured()
    if provider == "outlook":
        from app.services.connectors import outlook
        return outlook.oauth_configured()
    return False


def load_tokens(provider: str) -> dict:
    return _load_json(_token_path(provider), {})


def granted(provider: str) -> bool:
    tok = load_tokens(provider)
    return bool(tok.get("access_token") or tok.get("refresh_token"))


def active_provider() -> str | None:
    """The provider a send goes through: the first one with a send grant."""
    for p in PROVIDERS:
        if granted(p):
            return p
    return None


def status() -> dict[str, Any]:
    return {
        "enabled": enabled(),
        "provider": active_provider(),
        "providers": [{
            "id": p, "label": LABELS[p], "configured": configured(p),
            "granted": granted(p),
            "account": load_tokens(p).get("account") or None,
        } for p in PROVIDERS],
    }


def revoke(provider: str) -> dict[str, Any]:
    path = _token_path(provider)
    try:
        if path.is_file():
            path.unlink()
            return {"ok": True, "removed": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "removed": False}


# --- scopes --------------------------------------------------------------------
def assert_send_scopes(provider: str, scope_string: str) -> None:
    """Refuse a send grant that is missing the send scope or carries more."""
    got = set()
    for raw in (scope_string or "").split():
        s = raw.strip()
        if provider == "outlook" and s.startswith("https://graph.microsoft.com/"):
            s = s[len("https://graph.microsoft.com/"):]
        if s:
            got.add(s)
    if provider == "google":
        need, ok = {GMAIL_SEND_SCOPE}, {GMAIL_SEND_SCOPE}
    else:
        got -= _GRAPH_PLATFORM
        need, ok = {GRAPH_SEND_SCOPE}, {GRAPH_SEND_SCOPE} | _GRAPH_TOLERATED
    extra = got - ok
    if extra:
        raise PermissionError(f"send grant includes disallowed scopes: {sorted(extra)}")
    missing = need - got
    if missing:
        raise PermissionError(f"send grant missing required scopes: {sorted(missing)}")


def _graph_scope_string() -> str:
    return " ".join(("offline_access", "User.Read", GRAPH_SEND_SCOPE))


# --- OAuth -----------------------------------------------------------------------
def _auth_url(provider: str, redirect: str, state: str) -> str:
    if provider == "google":
        q = urlencode({
            "client_id": settings.exhaust.client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": GMAIL_SEND_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        })
        return f"https://accounts.google.com/o/oauth2/v2/auth?{q}"
    from app.services.connectors import outlook
    q = urlencode({
        "client_id": outlook._cfg().client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "response_mode": "query",
        "scope": _graph_scope_string(),
        "prompt": "select_account",
        "state": state,
    })
    return f"{outlook._authority()}/authorize?{q}"


def _token_request(provider: str, fields: dict[str, str]) -> dict:
    body = dict(fields)
    if provider == "google":
        body["client_id"] = settings.exhaust.client_id
        body["client_secret"] = settings.exhaust.client_secret
        url = "https://oauth2.googleapis.com/token"
    else:
        from app.services.connectors import outlook
        body["client_id"] = outlook._cfg().client_id
        if outlook._cfg().client_secret:
            body["client_secret"] = outlook._cfg().client_secret
        body.setdefault("scope", _graph_scope_string())
        url = f"{outlook._authority()}/token"
    req = Request(url, data=urlencode(body).encode(), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def begin_grant(provider: str, *, public_base: str | None = None,
                return_path: str | None = None) -> dict[str, Any]:
    """Mint a send-purpose OAuth state and return the consent URL.

    Hosted (HTTPS) only: the desktop loopback flow is not offered for
    sending, so a send grant always lands through the registered callback.
    """
    from app.services.connectors.base import (
        oauth_redirect_base, public_base_url, safe_return_path)
    from app.services.exhaust_ingest import _encode_state, _prune_oauth_states
    if provider not in PROVIDERS:
        return {"ok": False, "error": f"unknown provider: {provider}"}
    if not enabled():
        return {"ok": False, "error": "sending is off on this install (QUILL_MAIL_SEND)"}
    if not configured(provider):
        return {"ok": False, "error": f"{LABELS[provider]} OAuth is not configured"}
    base = (public_base or "").strip().rstrip("/") or (public_base_url() or "")
    if not base.lower().startswith("https://"):
        return {"ok": False, "error": "sending needs the HTTPS (hosted) sign-in"}
    anchor = oauth_redirect_base() or base
    state = _encode_state(base)
    redirect = f"{anchor}/oauth/{provider}/callback"
    path = _state_path(provider)
    states = _prune_oauth_states(_load_json(path, {}))
    states[state] = {"created_at": time.time(), "redirect_uri": redirect,
                     "return_origin": base, "purpose": "send",
                     "return_path": safe_return_path(return_path, default="/meetings")}
    _save_json(path, states)
    return {"ok": True, "auth_url": _auth_url(provider, redirect, state),
            "state": state, "redirect_uri": redirect}


def is_send_state(provider: str, state: str) -> bool:
    if provider not in PROVIDERS or not state:
        return False
    from app.services.exhaust_ingest import _prune_oauth_states
    entry = _prune_oauth_states(_load_json(_state_path(provider), {})).get(state)
    return isinstance(entry, dict) and entry.get("purpose") == "send"


def complete_grant(provider: str, code: str, state: str) -> dict[str, Any]:
    """Exchange the callback code for a send-only token (consumes the state)."""
    from app.services.exhaust_ingest import _prune_oauth_states
    path = _state_path(provider)
    states = _prune_oauth_states(_load_json(path, {}))
    entry = states.pop(state or "", None)
    _save_json(path, states)
    if not entry or entry.get("purpose") != "send":
        return {"ok": False, "error": "oauth state mismatch or expired"}
    back = entry.get("return_path")
    if not code:
        return {"ok": False, "error": "no oauth code", "return_path": back}
    fields = {"code": code, "redirect_uri": entry.get("redirect_uri") or "",
              "grant_type": "authorization_code"}
    try:
        tokens = _token_request(provider, fields)
        fallback = GMAIL_SEND_SCOPE if provider == "google" else _graph_scope_string()
        assert_send_scopes(provider, tokens.get("scope") or fallback)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc), "return_path": back}
    except Exception as exc:
        return {"ok": False, "error": f"token exchange failed: {exc}",
                "return_path": back}
    tokens = dict(tokens)
    tokens["obtained_at"] = time.time()
    tokens["granted_at"] = tokens["obtained_at"]
    _save_json(_token_path(provider), tokens)
    return {"ok": True, "return_path": back}


def _access_token(provider: str) -> str:
    tok = load_tokens(provider)
    if not tok:
        raise RuntimeError(f"no {LABELS.get(provider, provider)} send grant")
    obtained = float(tok.get("obtained_at") or 0)
    expires = float(tok.get("expires_in") or 3600)
    if tok.get("refresh_token") and (time.time() - obtained > expires - 60):
        fields = {"refresh_token": tok["refresh_token"],
                  "grant_type": "refresh_token"}
        fresh = _token_request(provider, fields)
        tok.update(fresh)
        tok["obtained_at"] = time.time()
        _save_json(_token_path(provider), tok)
    return str(tok["access_token"])


# --- sending ---------------------------------------------------------------------
def build_mime(*, to: str, subject: str, body: str,
               to_name: str | None = None, from_addr: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = formataddr((to_name or "", to)) if to_name else to
    if from_addr:
        msg["From"] = from_addr
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    return msg


def _post_json(url: str, payload: dict, token: str) -> tuple[int, dict]:
    req = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8") or "{}"
        return resp.status, json.loads(raw)


def send(provider: str, *, to: str, subject: str, body: str,
         to_name: str | None = None) -> dict[str, Any]:
    """Send one plain-text message as the user. Returns {ok, message_id?, error?}."""
    if not enabled():
        return {"ok": False, "error": "sending is off on this install"}
    if provider not in PROVIDERS or not granted(provider):
        return {"ok": False, "error": "no send grant — allow sending first"}
    if not to or "@" not in to:
        return {"ok": False, "error": "no recipient address"}
    try:
        token = _access_token(provider)
        if provider == "google":
            raw = base64.urlsafe_b64encode(
                build_mime(to=to, subject=subject, body=body,
                           to_name=to_name).as_bytes()).decode("ascii")
            _, data = _post_json(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                {"raw": raw}, token)
            return {"ok": True, "message_id": data.get("id"),
                    "thread_id": data.get("threadId")}
        recipient = {"emailAddress": {"address": to}}
        if to_name:
            recipient["emailAddress"]["name"] = to_name
        status_code, _ = _post_json(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            {"message": {"subject": subject,
                         "body": {"contentType": "Text", "content": body},
                         "toRecipients": [recipient]},
             "saveToSentItems": True}, token)
        # Graph answers 202 Accepted with no body and no message id.
        return {"ok": status_code in (200, 202), "message_id": None}
    except Exception as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]  # type: ignore[attr-defined]
        except Exception:
            pass
        return {"ok": False, "error": f"{exc}{(' — ' + detail) if detail else ''}"}
