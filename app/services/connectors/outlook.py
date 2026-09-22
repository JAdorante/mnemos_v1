"""Outlook connector — Microsoft Graph mail headers + calendar, read-only.

Mirrors the Google connector. OAuth is the Microsoft identity platform v2
(``login.microsoftonline.com/<tenant>``) with the narrowest scopes that give
metadata: ``Mail.ReadBasic`` (sender, recipients, subject, dates — Graph
never returns a body or attachment under it) and ``Calendars.Read``.
``offline_access`` yields the refresh token. Same two connect modes as
Google: hosted HTTPS redirect (``/oauth/outlook/callback``, relayable via
``QUILL_OAUTH_REDIRECT_BASE``) or desktop loopback. Tokens live at
``data/connectors/outlook/token.json``.

The generic OAuth-state helpers (``_encode_state`` / ``state_return_origin``
/ ``_prune_oauth_states``) and the People-seeding ingest are shared with
``exhaust_ingest``; nothing here writes to Microsoft.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from app.config import settings
from app.services.connectors.base import oauth_redirect_base, public_base_url

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE_PREFIX = "https://graph.microsoft.com/"

# Graph permission scopes we ask for and the only ones we accept back.
SCOPES = ("Mail.ReadBasic", "Calendars.Read")
# Identity-platform scopes that ride along on every grant; never data access.
_PLATFORM_SCOPES = {"offline_access", "openid", "profile", "email", "User.Read"}

SOURCE_MAIL = "exhaust.outlook"
SOURCE_CAL = "exhaust.mscalendar"

_lock = threading.Lock()
_progress: dict[str, Any] = {
    "running": False, "contacts": 0, "events": 0, "messages": 0, "error": None,
}


# --- paths / config -----------------------------------------------------------------
def _cfg():
    return settings.outlook


def _token_path() -> Path:
    return Path(_cfg().token_path)


def _oauth_state_path() -> Path:
    return Path(_cfg().oauth_state_path)


def oauth_configured() -> bool:
    """A client id is enough (public client); a secret makes it confidential."""
    return bool(_cfg().client_id)


def _load_json(path: Path, default):
    from app.services.exhaust_ingest import _load_json as _lj
    return _lj(path, default)


def _save_json(path: Path, data) -> None:
    from app.services.exhaust_ingest import _save_json as _sj
    _sj(path, data)


def progress() -> dict[str, Any]:
    with _lock:
        return dict(_progress)


def _set_progress(**kw) -> None:
    with _lock:
        _progress.update(kw)


# --- OAuth ----------------------------------------------------------------------------
def _authority() -> str:
    tenant = (_cfg().tenant or "common").strip() or "common"
    return f"https://login.microsoftonline.com/{quote(tenant)}/oauth2/v2.0"


def _scope_string() -> str:
    return " ".join(("offline_access", "User.Read") + SCOPES)


def _auth_url(redirect: str, state: str) -> str:
    q = urlencode({
        "client_id": _cfg().client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "response_mode": "query",
        "scope": _scope_string(),
        "prompt": "select_account",
        "state": state,
    })
    return f"{_authority()}/authorize?{q}"


def assert_metadata_scopes(scope_string: str) -> None:
    """Refuse a grant carrying anything beyond the read-only metadata pair.

    Graph echoes scopes either bare (``Mail.ReadBasic``) or fully qualified
    (``https://graph.microsoft.com/Mail.ReadBasic``); both are normalised.
    Identity-platform scopes (openid, profile, offline_access…) are ignored.
    """
    got = set()
    for raw in (scope_string or "").split():
        s = raw.strip()
        if s.startswith(GRAPH_SCOPE_PREFIX):
            s = s[len(GRAPH_SCOPE_PREFIX):]
        if s and s not in _PLATFORM_SCOPES:
            got.add(s)
    allowed = set(SCOPES)
    extra = got - allowed
    if extra:
        raise PermissionError(
            f"OAuth grant includes disallowed scopes: {sorted(extra)}")
    missing = allowed - got
    if missing:
        raise PermissionError(
            f"OAuth grant missing required scopes: {sorted(missing)}")


def _token_request(fields: dict[str, str]) -> dict:
    body = dict(fields)
    body["client_id"] = _cfg().client_id
    if _cfg().client_secret:
        body["client_secret"] = _cfg().client_secret
    req = Request(f"{_authority()}/token", data=urlencode(body).encode(),
                  method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _exchange_code(code: str, redirect: str) -> dict:
    data = _token_request({
        "code": code, "redirect_uri": redirect,
        "grant_type": "authorization_code", "scope": _scope_string(),
    })
    assert_metadata_scopes(data.get("scope") or _scope_string())
    return data


def _refresh_token(refresh: str) -> dict:
    return _token_request({
        "refresh_token": refresh, "grant_type": "refresh_token",
        "scope": _scope_string(),
    })


def load_tokens() -> dict:
    return _load_json(_token_path(), {})


def connected() -> bool:
    tok = load_tokens()
    return bool(tok.get("access_token") or tok.get("refresh_token"))


def _save_tokens(tokens: dict) -> None:
    tokens = dict(tokens)
    tokens["obtained_at"] = time.time()
    _save_json(_token_path(), tokens)


def clear_tokens() -> dict[str, Any]:
    removed = []
    path = _token_path()
    try:
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    except OSError:
        pass
    return {"ok": True, "removed": removed}


def peek_oauth_state(state: str) -> dict:
    from app.services.exhaust_ingest import _prune_oauth_states
    if not state:
        return {}
    entry = _prune_oauth_states(_load_json(_oauth_state_path(), {})).get(state)
    return dict(entry) if isinstance(entry, dict) else {}


def has_oauth_state(state: str) -> bool:
    return bool(peek_oauth_state(state))


def state_return_origin(state: str) -> str | None:
    from app.services.exhaust_ingest import state_return_origin as _sro
    return _sro(state)


def start_oauth_redirect(public_base: str, *, redirect_base: str | None = None,
                         return_path: str | None = None) -> dict[str, Any]:
    """Hosted / HTTPS OAuth: mint state, return the Microsoft auth_url."""
    from app.services.connectors.base import safe_return_path
    from app.services.exhaust_ingest import _encode_state, _prune_oauth_states
    if not oauth_configured():
        return {"ok": False, "error": "MS_OAUTH_CLIENT_ID not set", "skip": True}
    base = (public_base or "").strip().rstrip("/")
    if not base.lower().startswith("https://"):
        return {"ok": False, "error": "public_base must be https://"}
    anchor = (redirect_base or "").strip().rstrip("/")
    if not anchor.lower().startswith("https://"):
        anchor = base
    state = _encode_state(base)
    redirect = f"{anchor}/oauth/outlook/callback"
    states = _prune_oauth_states(_load_json(_oauth_state_path(), {}))
    states[state] = {"created_at": time.time(), "redirect_uri": redirect,
                     "return_origin": base,
                     "return_path": safe_return_path(return_path)}
    _save_json(_oauth_state_path(), states)
    return {
        "ok": True, "mode": "redirect",
        "auth_url": _auth_url(redirect, state),
        "redirect_uri": redirect, "return_origin": base,
        "return_path": states[state]["return_path"], "state": state,
    }


def complete_oauth_redirect(code: str, state: str, *,
                            redirect_uri: str | None = None) -> dict[str, Any]:
    from app.services.exhaust_ingest import _prune_oauth_states
    if not oauth_configured():
        return {"ok": False, "error": "MS_OAUTH_CLIENT_ID not set"}
    states = _prune_oauth_states(_load_json(_oauth_state_path(), {}))
    entry = states.pop(state or "", None)
    _save_json(_oauth_state_path(), states)
    if not entry:
        return {"ok": False, "error": "oauth state mismatch or expired"}
    redirect = redirect_uri or entry.get("redirect_uri") or ""
    if not redirect:
        return {"ok": False, "error": "missing redirect_uri"}
    if not code:
        return {"ok": False, "error": "no oauth code"}
    try:
        tokens = _exchange_code(code, redirect)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"token exchange failed: {exc}"}
    _save_tokens(tokens)
    return {"ok": True, "scopes": tokens.get("scope"), "mode": "redirect",
            "return_path": entry.get("return_path")}


def start_oauth_loopback() -> dict[str, Any]:
    """Desktop: system browser + a one-shot listener on 127.0.0.1.

    Register ``http://127.0.0.1`` (any port) as a *Mobile and desktop*
    redirect on the Entra app registration; blocks until the redirect lands.
    """
    if not oauth_configured():
        return {"ok": False, "error": "MS_OAUTH_CLIENT_ID not set", "skip": True}
    import secrets
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    state = secrets.token_urlsafe(16)
    result: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            result["code"] = (qs.get("code") or [None])[0]
            result["state"] = (qs.get("state") or [None])[0]
            result["error"] = (qs.get("error") or [None])[0]
            body = b"<html><body>You can close this tab.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # noqa: A003
            return

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    redirect = f"http://127.0.0.1:{port}/"
    try:
        webbrowser.open(_auth_url(redirect, state))
        httpd.handle_request()
    finally:
        httpd.server_close()
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    if result.get("state") != state:
        return {"ok": False, "error": "oauth state mismatch"}
    if not result.get("code"):
        return {"ok": False, "error": "no oauth code"}
    try:
        tokens = _exchange_code(result["code"], redirect)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"token exchange failed: {exc}"}
    _save_tokens(tokens)
    return {"ok": True, "scopes": tokens.get("scope"), "mode": "loopback"}


# --- Graph reads (metadata only) -------------------------------------------------------
def _access_token() -> str:
    tok = load_tokens()
    if not tok:
        raise RuntimeError("not connected")
    obtained = float(tok.get("obtained_at") or 0)
    expires = float(tok.get("expires_in") or 3600)
    if tok.get("refresh_token") and (time.time() - obtained > expires - 60):
        fresh = _refresh_token(tok["refresh_token"])
        tok.update(fresh)
        tok["obtained_at"] = time.time()
        _save_json(_token_path(), tok)
    return str(tok["access_token"])


def _graph_get(url: str) -> dict:
    req = Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {_access_token()}")
    # Calendar start/end come back in this zone instead of the mailbox's.
    req.add_header("Prefer", 'outlook.timezone="UTC"')
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _iso_utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_iso(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        from datetime import datetime, timezone
        s = str(raw).strip()
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp()
        # Graph emits 7 fractional digits; fromisoformat wants ≤6.
        if "." in s:
            head, _, tail = s.partition(".")
            frac = "".join(ch for ch in tail if ch.isdigit())[:6]
            zone = tail[len("".join(ch for ch in tail if ch.isdigit())):]
            s = f"{head}.{frac}{zone}" if frac else f"{head}{zone}"
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _addr(rec: dict | None) -> str:
    """Graph recipient → RFC 2822 ``Name <addr>`` so header parsing is shared."""
    ea = (rec or {}).get("emailAddress") or {}
    name = (ea.get("name") or "").strip()
    addr = (ea.get("address") or "").strip().lower()
    if not addr:
        return ""
    return f"{name} <{addr}>" if name else addr


def _addrs(recs: list | None) -> str:
    return ", ".join(a for a in (_addr(r) for r in (recs or [])) if a)


def fetch_mail_headers(*, days: float | None = None,
                       now: float | None = None,
                       include_subject: bool = False) -> list[dict]:
    """Last N days of message metadata via ``Mail.ReadBasic``.

    Same shape as ``exhaust_ingest.fetch_gmail_headers`` so both feed the
    People-seeding ingest and the background capture unchanged.
    """
    days = float(days if days is not None else settings.exhaust.days)
    ts = time.time() if now is None else float(now)
    since = _iso_utc(ts - days * 86400)
    select = ("id,conversationId,internetMessageId,receivedDateTime,"
              "sentDateTime,from,toRecipients,ccRecipients,isDraft")
    if include_subject:
        select += ",subject"
    url = (f"{GRAPH}/me/messages?$select={quote(select)}"
           f"&$filter={quote(f'receivedDateTime ge {since}')}"
           f"&$orderby={quote('receivedDateTime desc')}&$top=100")
    out: list[dict] = []
    while url:
        data = _graph_get(url)
        for m in data.get("value") or []:
            if m.get("isDraft"):
                continue
            # Guard: Mail.ReadBasic must never carry a body.
            if m.get("body") or m.get("attachments") or m.get("bodyPreview"):
                raise RuntimeError("graph returned message body data; aborting")
            headers = {
                "from": _addr(m.get("from")),
                "to": _addrs(m.get("toRecipients")),
                "cc": _addrs(m.get("ccRecipients")),
                "date": m.get("receivedDateTime") or m.get("sentDateTime") or "",
                "message-id": m.get("internetMessageId") or "",
            }
            if include_subject:
                headers["subject"] = m.get("subject") or ""
            msg_ts = _parse_iso(m.get("receivedDateTime")
                                or m.get("sentDateTime")) or ts
            out.append({"id": headers["message-id"] or m.get("id"),
                        "thread_id": str(m.get("conversationId") or "") or None,
                        "headers": headers, "ts": msg_ts})
        url = data.get("@odata.nextLink")
        if len(out) >= 5000:
            break
    return out


def fetch_calendar_events(*, days: int | None = None,
                          now: float | None = None) -> list[dict]:
    """``/me/calendarView`` over the window, expanded (no recurrence masters)."""
    days = int(days if days is not None else settings.exhaust.days)
    ts = time.time() if now is None else float(now)
    start = _iso_utc(ts - days * 86400)
    end = _iso_utc(ts + 1 * 86400)
    select = "id,subject,start,end,attendees,organizer,isCancelled,seriesMasterId"
    url = (f"{GRAPH}/me/calendarView?startDateTime={quote(start)}"
           f"&endDateTime={quote(end)}&$select={quote(select)}&$top=250")
    out: list[dict] = []
    while url:
        data = _graph_get(url)
        for it in data.get("value") or []:
            if it.get("isCancelled"):
                continue
            s = _parse_iso((it.get("start") or {}).get("dateTime"))
            e = _parse_iso((it.get("end") or {}).get("dateTime")) or s
            attendees = []
            for a in it.get("attendees") or []:
                ea = a.get("emailAddress") or {}
                email = (ea.get("address") or "").lower()
                if not email:
                    continue
                attendees.append({"email": email, "name": ea.get("name") or ""})
            org = ((it.get("organizer") or {}).get("emailAddress") or {})
            organizer = None
            if org.get("address"):
                organizer = {"email": str(org["address"]).lower(),
                             "name": org.get("name") or ""}
            out.append({
                "id": it.get("id"), "title": it.get("subject") or "",
                "start": s, "end": e, "attendees": attendees,
                "organizer": organizer,
                "recurrence": [it["seriesMasterId"]]
                if it.get("seriesMasterId") else [],
            })
        url = data.get("@odata.nextLink")
    return out


# --- cold-start ingest (People seeding) --------------------------------------------------
def run_ingest(*, store=None, now: float | None = None) -> dict[str, Any]:
    """Fetch headers + events and seed People through exhaust_ingest."""
    from app.services import exhaust_ingest as ex
    _set_progress(running=True, contacts=0, events=0, messages=0, error=None)
    try:
        if not connected():
            return {"ok": False, "error": "not connected", "need_oauth": True}
        messages = fetch_mail_headers(now=now)
        _set_progress(messages=len(messages))
        events = fetch_calendar_events(now=now)
        _set_progress(events=len(events))
        out = ex.run_ingest(messages=messages, events=events, fetch=False,
                            now=now, provider="outlook")
        _set_progress(running=False, contacts=int(out.get("contacts") or 0))
        return out
    except Exception as exc:
        _set_progress(running=False, error=str(exc))
        return {"ok": False, "error": str(exc)}


def status() -> dict[str, Any]:
    return {
        "oauth_configured": oauth_configured(),
        "connected": connected(),
        "days": settings.exhaust.days,
        "scopes": list(SCOPES),
        "tenant": _cfg().tenant or "common",
        "progress": progress(),
    }


# --- the connector ----------------------------------------------------------------------
class OutlookConnector:
    id = "outlook"
    label = "Outlook (Mail + Calendar)"
    tool_names = ("Outlook", "Microsoft Calendar")
    availability = "ready"
    kind = "directory"
    category = "calendar"
    source_prefixes = (SOURCE_MAIL, SOURCE_CAL, "outlook.mail", "outlook.calendar")
    sync_interval_s = 300
    sync_blurb = ("new Outlook message headers (sender, recipients, subject) "
                  "and Microsoft Calendar events")
    description = (
        "Read-only Outlook mail headers and Microsoft Calendar events. "
        "You sign in on Microsoft's screen — Sparrow never sees your password."
    )
    capabilities = (
        "Import contacts from recent mail headers",
        "Import upcoming and recent calendar events",
        "Seed People and your next-meeting view",
    )

    def configured(self) -> bool:
        return oauth_configured()

    def connected(self) -> bool:
        return connected()

    def status(self) -> dict[str, Any]:
        st = status()
        return {
            "id": self.id, "label": self.label,
            "tool_names": list(self.tool_names),
            "availability": self.availability, "kind": self.kind,
            "category": self.category, "description": self.description,
            "capabilities": list(self.capabilities),
            "configured": st["oauth_configured"], "connected": st["connected"],
            "days": st["days"], "scopes": st["scopes"], "tenant": st["tenant"],
            "progress": st["progress"],
            "oauth_mode": "redirect" if public_base_url() else "loopback",
            "public_base": public_base_url(),
            "redirect_base": oauth_redirect_base(),
            "error": None,
        }

    def begin_connect(self, *, public_base: str | None = None,
                      return_path: str | None = None) -> dict[str, Any]:
        base = (public_base or "").strip().rstrip("/") or public_base_url()
        if base and base.lower().startswith("https://"):
            return start_oauth_redirect(base, redirect_base=oauth_redirect_base(),
                                        return_path=return_path)
        result = start_oauth_loopback()
        if result.get("ok"):
            result = {**result, "mode": "loopback"}
        return result

    def complete_connect(self, code: str, state: str,
                         *, redirect_uri: str) -> dict[str, Any]:
        return complete_oauth_redirect(code, state, redirect_uri=redirect_uri)

    # OAuth-callback relay hooks (adoption.py) — same names as exhaust_ingest.
    peek_oauth_state = staticmethod(peek_oauth_state)
    has_oauth_state = staticmethod(has_oauth_state)
    state_return_origin = staticmethod(state_return_origin)

    def sync(self) -> dict[str, Any]:
        if not connected():
            return {"ok": False, "error": "not connected"}
        if progress().get("running"):
            return {"ok": True, "running": True, **progress()}
        t = threading.Thread(target=run_ingest, name="outlook-ingest", daemon=True)
        t.start()
        return {"ok": True, "started": True}

    def disconnect(self) -> dict[str, Any]:
        return clear_tokens()

    # --- background capture (spec F1) ----------------------------------------
    def fetch_items(self, *, cursor: dict | None = None,
                    now: float | None = None) -> tuple[list[dict], dict]:
        now = float(now if now is not None else time.time())
        cursor = dict(cursor or {})
        backfill_days = float(os.environ.get("QUILL_CONNECTOR_BACKFILL_DAYS", "7"))
        since = cursor.get("since")
        try:
            since = float(since) if since is not None else now - backfill_days * 86400
        except (TypeError, ValueError):
            since = now - backfill_days * 86400
        window_days = max(1.0 / 24.0, (now - since + 3600) / 86400)
        items: list[dict] = []
        for m in fetch_mail_headers(days=window_days, now=now,
                                    include_subject=True):
            items.append(self._mail_item(m))
        for e in fetch_calendar_events(days=window_days, now=now):
            items.append(self._calendar_item(e))
        return items, {**cursor, "since": now}

    @staticmethod
    def _addr_names(raw: str) -> list[str]:
        from app.services import exhaust_ingest as ex
        out = []
        for a in ex.parse_rfc2822_addr(raw or ""):
            name = (a.get("name") or "").strip() or (a.get("email") or "")
            if name and name not in out:
                out.append(name)
        return out

    def _mail_item(self, m: dict) -> dict:
        h = m.get("headers") or {}
        subject = (h.get("subject") or "").strip()
        frm, to, cc = h.get("from") or "", h.get("to") or "", h.get("cc") or ""
        people = self._addr_names(frm) + self._addr_names(to) + self._addr_names(cc)
        lines = [f"From: {frm}" if frm else "", f"To: {to}" if to else "",
                 f"Cc: {cc}" if cc else "", f"Subject: {subject}" if subject else ""]
        return {"kind": "mail", "external_id": m.get("id"),
                "thread_id": m.get("thread_id"), "ts": m.get("ts"),
                "title": subject, "text": "\n".join(l for l in lines if l),
                "people": people, "from": frm, "to": to, "cc": cc}

    def _calendar_item(self, e: dict) -> dict:
        attendees = [a.get("name") or a.get("email") for a in (e.get("attendees") or [])
                     if (a.get("name") or a.get("email"))]
        title = (e.get("title") or "").strip()
        return {"kind": "calendar", "external_id": e.get("id"), "ts": e.get("start"),
                "title": title, "text": f"{title} ({', '.join(attendees)})" if attendees
                else title, "people": attendees, "start": e.get("start"),
                "end": e.get("end"), "attendees": e.get("attendees") or [],
                "organizer": e.get("organizer") or {}, "summary": title}

    def lookup(self, need: str, *, timeout_s: float = 10.0) -> list[dict]:
        """Option A search assist: recent mail whose subject matches the need."""
        from app.services.slots import coverage
        deadline = time.time() + float(timeout_s)
        out = []
        try:
            for m in fetch_mail_headers(days=30, include_subject=True):
                if time.time() > deadline:
                    break
                item = self._mail_item(m)
                if coverage(need, item.get("title") or "") >= 0.6:
                    out.append(item)
        except Exception as exc:
            print(f"[connectors] outlook lookup skipped ({exc}).")
        return out[:10]


outlook = OutlookConnector()
