"""Shared OAuth2 (authorization code) plumbing for the directory connectors
that are not mail: GitHub, Linear, HubSpot.

Google and Outlook each carry their own copy of the redirect / loopback /
state / refresh dance. This module is that dance once, parameterised by the
provider's two URLs, its scopes and its config, so a new connector is the
provider's read calls plus item mapping (see github.py for the smallest one).

Two connect modes, the same as the mail connectors:

  hosted HTTPS   ``begin_connect`` returns ``{mode: "redirect", auth_url}``;
                 the browser comes back to ``/oauth/<id>/callback`` (relayable
                 through ``QUILL_OAUTH_REDIRECT_BASE``) and ``complete_connect``
                 exchanges the code with the redirect_uri saved under ``state``.
  desktop        a one-shot listener on the loopback address. GitHub accepts
                 any port on 127.0.0.1; Linear and HubSpot need the exact
                 ``http://localhost:<port>/`` registered, so those have a
                 fixed default port (``QUILL_OAUTH_LOOPBACK_PORT_<ID>``).

A third way in needs no OAuth app at all: an env token (``GITHUB_TOKEN``,
``LINEAR_API_KEY``, ``HUBSPOT_ACCESS_TOKEN``). When set, Connect verifies it
against the provider and the connector reads as connected; Disconnect records
an opt-out so the token stays unused until Connect is pressed again.

Tokens live at ``data/connectors/<id>/token.json``. Nothing here, or in any
subclass, issues a write to a provider: every request is a read, and the
scope guard refuses a grant carrying a scope that was not asked for.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.services.connectors.base import (
    oauth_redirect_base,
    public_base_url,
    safe_return_path,
)

USER_AGENT = "Sparrow-connectors/1.0"
# Fresh overlap on every incremental fetch: the scheduler's ledger drops
# re-seen items, so a few minutes of overlap costs nothing and never misses
# an item updated while the previous fetch ran.
FETCH_OVERLAP_S = 300.0


def iso_utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_iso(raw: Any) -> float:
    """ISO-8601 (``Z`` or offset, any fractional digits) or ms epoch → epoch
    seconds; 0.0 when unparseable."""
    if raw in (None, ""):
        return 0.0
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v / 1000.0 if v > 1e11 else v
    s = str(raw).strip()
    if s.isdigit():
        return parse_iso(int(s))
    try:
        from datetime import datetime, timezone
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp()
        if "." in s:
            head, _, tail = s.partition(".")
            digits = "".join(ch for ch in tail if ch.isdigit())
            frac = digits[:6]
            zone = tail[len(digits):]
            s = f"{head}.{frac}{zone}" if frac else f"{head}{zone}"
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def clip(text: Any, cap: int) -> str:
    s = str(text or "").replace("\r", "").strip()
    return s if len(s) <= cap else s[: cap - 1].rstrip() + "…"


class OAuthApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class OAuthConnectorBase:
    """Subclasses set the class attributes and implement ``_cfg``,
    ``_whoami`` and ``fetch_items``."""

    id = ""
    label = ""
    tool_names: tuple[str, ...] = ()
    availability = "ready"
    kind = "directory"
    category = "productivity"
    description = ""
    capabilities: tuple[str, ...] = ()
    source_prefixes: tuple[str, ...] = ()
    sync_interval_s = 300
    sync_blurb = ""

    # Provider endpoints.
    authorize_url = ""
    token_url = ""
    scopes: tuple[str, ...] = ()
    scope_separator = " "
    extra_authorize_params: dict[str, str] = {}
    # Loopback host the provider accepts for a desktop redirect, and the
    # default port (0 = any; providers that need an exact match get a fixed one).
    loopback_host = "127.0.0.1"
    loopback_port = 0
    # Slack accepts only HTTPS redirects: no desktop loopback at all.
    loopback_supported = True
    # Env var names, for messages and the env-token path.
    client_id_env = ""
    client_secret_env = ""
    env_token_var = ""
    env_token_label = "access token"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._progress: dict[str, Any] = {"running": False, "error": None}
        self._last_counts: dict[str, int] = {}
        self._last_fetch_at: float | None = None

    # --- subclass hooks ---------------------------------------------------------
    def _cfg(self):
        raise NotImplementedError

    def _whoami(self) -> dict[str, Any]:
        """One cheap authenticated read; ``{"account": "<name>"}`` on success."""
        return {}

    def fetch_items(self, *, cursor: dict | None = None,
                    now: float | None = None) -> tuple[list[dict], dict]:
        raise NotImplementedError

    # --- paths / config -----------------------------------------------------------
    def _token_path(self) -> Path:
        return Path(self._cfg().token_path)

    def _oauth_state_path(self) -> Path:
        return Path(self._cfg().oauth_state_path)

    def _client_id(self) -> str:
        return str(getattr(self._cfg(), "client_id", "") or "").strip()

    def _client_secret(self) -> str:
        return str(getattr(self._cfg(), "client_secret", "") or "").strip()

    def requested_scopes(self) -> tuple[str, ...]:
        raw = getattr(self._cfg(), "scopes", None)
        if raw:
            return tuple(s for s in str(raw).replace(",", " ").split() if s)
        return tuple(self.scopes)

    def env_token(self) -> str:
        if not self.env_token_var:
            return ""
        return (os.environ.get(self.env_token_var) or "").strip()

    def oauth_configured(self) -> bool:
        """Every provider here needs a confidential client for the exchange."""
        return bool(self._client_id() and self._client_secret())

    def setup_hint(self) -> str:
        parts = []
        if self.client_id_env:
            parts.append(f"{self.client_id_env} + {self.client_secret_env}")
        if self.env_token_var:
            parts.append(f"a {self.env_token_var} {self.env_token_label}")
        return "Set " + " or ".join(parts) + " on this install."

    def _load_json(self, path: Path, default):
        from app.services.exhaust_ingest import _load_json as _lj
        return _lj(path, default)

    def _save_json(self, path: Path, data) -> None:
        from app.services.exhaust_ingest import _save_json as _sj
        _sj(path, data)

    def progress(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._progress)

    def _set_progress(self, **kw) -> None:
        with self._lock:
            self._progress.update(kw)

    # --- tokens -------------------------------------------------------------------
    def load_tokens(self) -> dict:
        return self._load_json(self._token_path(), {})

    def _save_tokens(self, tokens: dict) -> None:
        tokens = dict(tokens)
        tokens["obtained_at"] = time.time()
        tokens.pop("env_opt_out", None)
        self._save_json(self._token_path(), tokens)

    def clear_tokens(self) -> dict[str, Any]:
        removed = []
        path = self._token_path()
        try:
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        except OSError:
            pass
        return {"ok": True, "removed": removed}

    def _env_opted_out(self) -> bool:
        return bool(self.load_tokens().get("env_opt_out"))

    def env_token_active(self) -> bool:
        return bool(self.env_token()) and not self._env_opted_out()

    def _oauth_connected(self) -> bool:
        tok = self.load_tokens()
        return bool(tok.get("access_token") or tok.get("refresh_token"))

    def connected(self) -> bool:
        return self.env_token_active() or self._oauth_connected()

    def configured(self) -> bool:
        return bool(self.env_token()) or self.oauth_configured()

    def auth_mode(self) -> str | None:
        if self.env_token_active():
            return "token"
        if self._oauth_connected():
            return "oauth"
        return None

    def _access_token(self) -> str:
        if self.env_token_active():
            return self.env_token()
        tok = self.load_tokens()
        if not tok.get("access_token") and not tok.get("refresh_token"):
            raise RuntimeError("not connected")
        obtained = float(tok.get("obtained_at") or 0)
        expires = tok.get("expires_in")
        if tok.get("refresh_token") and expires:
            try:
                stale = time.time() - obtained > float(expires) - 60
            except (TypeError, ValueError):
                stale = False
            if stale or not tok.get("access_token"):
                fresh = self._refresh_token(str(tok["refresh_token"]))
                tok.update({k: v for k, v in fresh.items() if v is not None})
                tok["obtained_at"] = time.time()
                self._save_json(self._token_path(), tok)
        return str(tok["access_token"])

    # --- OAuth ----------------------------------------------------------------------
    def _scope_string(self) -> str:
        return self.scope_separator.join(self.requested_scopes())

    def _auth_url(self, redirect: str, state: str) -> str:
        q = {
            "client_id": self._client_id(),
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": self._scope_string(),
            "state": state,
        }
        q.update(self.extra_authorize_params)
        return f"{self.authorize_url}?{urlencode(q)}"

    def _post_token_endpoint(self, body: bytes, headers: dict[str, str]) -> dict:
        """Raw POST to the token endpoint; JSON object back or RuntimeError."""
        req = Request(self.token_url, data=body, method="POST")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"token endpoint HTTP {exc.code}: {detail}") from None
        if not isinstance(data, dict):
            raise RuntimeError("token endpoint returned a non-object")
        return data

    def _token_request(self, fields: dict[str, str]) -> dict:
        """Form-encoded exchange with the client credentials in the body
        (GitHub, Linear, HubSpot, Slack). Notion overrides this."""
        body = dict(fields)
        body["client_id"] = self._client_id()
        body["client_secret"] = self._client_secret()
        data = self._post_token_endpoint(
            urlencode(body).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"})
        data = self._normalize_token_response(data)
        if data.get("error"):
            raise RuntimeError(
                f"{data.get('error')}: {data.get('error_description') or ''}".strip())
        if not data.get("access_token"):
            raise RuntimeError("token endpoint returned no access_token")
        return data

    def _normalize_token_response(self, data: dict) -> dict:
        """Hook: a provider whose token payload is not flat (Slack nests the
        user token under ``authed_user``) reshapes it here."""
        return data

    def assert_scopes(self, granted: Any) -> None:
        """Refuse a grant with a scope that was never asked for. A provider
        that does not echo scopes (HubSpot) passes; one that echoes a
        subset (user narrowed on the consent screen) passes too."""
        if granted in (None, ""):
            return
        if isinstance(granted, (list, tuple)):
            got = {str(s).strip() for s in granted if str(s).strip()}
        else:
            got = {s for s in str(granted).replace(",", " ").split() if s}
        extra = got - set(self.requested_scopes()) - set(self.scopes)
        if extra:
            raise PermissionError(
                f"OAuth grant includes disallowed scopes: {sorted(extra)}")

    def _exchange_code(self, code: str, redirect: str) -> dict:
        data = self._token_request({
            "code": code, "redirect_uri": redirect,
            "grant_type": "authorization_code",
        })
        self.assert_scopes(data.get("scope"))
        return data

    def _refresh_token(self, refresh: str) -> dict:
        return self._token_request({
            "refresh_token": refresh, "grant_type": "refresh_token",
        })

    def peek_oauth_state(self, state: str) -> dict:
        from app.services.exhaust_ingest import _prune_oauth_states
        if not state:
            return {}
        entry = _prune_oauth_states(
            self._load_json(self._oauth_state_path(), {})).get(state)
        return dict(entry) if isinstance(entry, dict) else {}

    def has_oauth_state(self, state: str) -> bool:
        return bool(self.peek_oauth_state(state))

    @staticmethod
    def state_return_origin(state: str) -> str | None:
        from app.services.exhaust_ingest import state_return_origin as _sro
        return _sro(state)

    def start_oauth_redirect(self, public_base: str, *,
                             redirect_base: str | None = None,
                             return_path: str | None = None) -> dict[str, Any]:
        """Hosted / HTTPS OAuth: mint state, return the provider's auth_url."""
        from app.services.exhaust_ingest import _encode_state, _prune_oauth_states
        if not self.oauth_configured():
            return {"ok": False, "skip": True,
                    "error": f"{self.client_id_env} / {self.client_secret_env} not set"}
        base = (public_base or "").strip().rstrip("/")
        if not base.lower().startswith("https://"):
            return {"ok": False, "error": "public_base must be https://"}
        anchor = (redirect_base or "").strip().rstrip("/")
        if not anchor.lower().startswith("https://"):
            anchor = base
        state = _encode_state(base)
        redirect = f"{anchor}/oauth/{self.id}/callback"
        states = _prune_oauth_states(self._load_json(self._oauth_state_path(), {}))
        states[state] = {"created_at": time.time(), "redirect_uri": redirect,
                         "return_origin": base,
                         "return_path": safe_return_path(return_path)}
        self._save_json(self._oauth_state_path(), states)
        return {
            "ok": True, "mode": "redirect",
            "auth_url": self._auth_url(redirect, state),
            "redirect_uri": redirect, "return_origin": base,
            "return_path": states[state]["return_path"], "state": state,
        }

    def complete_oauth_redirect(self, code: str, state: str, *,
                                redirect_uri: str | None = None) -> dict[str, Any]:
        from app.services.exhaust_ingest import _prune_oauth_states
        if not self.oauth_configured():
            return {"ok": False,
                    "error": f"{self.client_id_env} / {self.client_secret_env} not set"}
        states = _prune_oauth_states(self._load_json(self._oauth_state_path(), {}))
        entry = states.pop(state or "", None)
        self._save_json(self._oauth_state_path(), states)
        if not entry:
            return {"ok": False, "error": "oauth state mismatch or expired"}
        redirect = redirect_uri or entry.get("redirect_uri") or ""
        if not redirect:
            return {"ok": False, "error": "missing redirect_uri"}
        if not code:
            return {"ok": False, "error": "no oauth code"}
        try:
            tokens = self._exchange_code(code, redirect)
        except PermissionError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"token exchange failed: {exc}"}
        self._save_tokens(tokens)
        return {"ok": True, "scopes": tokens.get("scope"), "mode": "redirect",
                "return_path": entry.get("return_path")}

    def _loopback_port(self) -> int:
        raw = os.environ.get(f"QUILL_OAUTH_LOOPBACK_PORT_{self.id.upper()}")
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        return int(self.loopback_port or 0)

    def start_oauth_loopback(self) -> dict[str, Any]:
        """Desktop: system browser + a one-shot listener on the loopback host."""
        if not self.loopback_supported:
            return {"ok": False, "error": f"{self.label} does not allow loopback redirects"}
        if not self.oauth_configured():
            return {"ok": False, "skip": True,
                    "error": f"{self.client_id_env} / {self.client_secret_env} not set"}
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

        try:
            httpd = HTTPServer(("127.0.0.1", self._loopback_port()), Handler)
        except OSError as exc:
            return {"ok": False, "error": f"loopback port unavailable: {exc}"}
        port = httpd.server_address[1]
        redirect = f"http://{self.loopback_host}:{port}/"
        try:
            webbrowser.open(self._auth_url(redirect, state))
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
            tokens = self._exchange_code(result["code"], redirect)
        except PermissionError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"token exchange failed: {exc}"}
        self._save_tokens(tokens)
        return {"ok": True, "scopes": tokens.get("scope"), "mode": "loopback"}

    # --- authenticated reads ----------------------------------------------------------
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _request(self, url: str, *, method: str = "GET", body: Any = None,
                 headers: dict[str, str] | None = None) -> tuple[Any, dict]:
        """JSON in, JSON out. Returns (data, response headers)."""
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in self._auth_headers().items():
            req.add_header(k, v)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise OAuthApiError(exc.code, detail or exc.reason) from None
        return (json.loads(raw) if raw.strip() else None), hdrs

    # --- the Connector contract ------------------------------------------------------
    def status(self) -> dict[str, Any]:
        connected = self.connected()
        out: dict[str, Any] = {
            "id": self.id, "label": self.label,
            "tool_names": list(self.tool_names),
            "availability": self.availability, "kind": self.kind,
            "category": self.category, "description": self.description,
            "capabilities": list(self.capabilities),
            "configured": self.configured(), "connected": connected,
            "auth_mode": self.auth_mode(),
            "scopes": list(self.requested_scopes()),
            "progress": self.progress(),
            "oauth_mode": "redirect" if public_base_url() else "loopback",
            "public_base": public_base_url(),
            "redirect_base": oauth_redirect_base(),
            "error": None,
        }
        if not out["configured"]:
            out["setup_hint"] = self.setup_hint()
        detail = self.status_detail()
        if detail:
            out["status_detail"] = detail
        return out

    def status_detail(self) -> str | None:
        with self._lock:
            counts = dict(self._last_counts)
        if not counts:
            return None
        parts = [f"{n} {k.replace('_', ' ')}{'' if n == 1 else 's'}"
                 for k, n in sorted(counts.items()) if n]
        if not parts:
            return "Last check found nothing new."
        return "Last check found " + ", ".join(parts) + "."

    def _note_fetch(self, items: list[dict], now: float) -> None:
        counts: dict[str, int] = {}
        for it in items:
            k = str(it.get("kind") or "item")
            counts[k] = counts.get(k, 0) + 1
        with self._lock:
            self._last_counts = counts
            self._last_fetch_at = now

    def begin_connect(self, *, public_base: str | None = None,
                      return_path: str | None = None) -> dict[str, Any]:
        if self.env_token():
            # Operator-provided token: clear any opt-out, prove it works.
            self._save_json(self._token_path(), {})
            try:
                who = self._whoami()
            except Exception as exc:
                self._save_json(self._token_path(), {"env_opt_out": True})
                return {"ok": False,
                        "error": f"{self.env_token_var} was refused: {exc}"}
            return {"ok": True, "mode": "token", **(who or {})}
        if not self.oauth_configured():
            return {"ok": False, "skip": True, "error": self.setup_hint()}
        base = (public_base or "").strip().rstrip("/") or public_base_url()
        if base and base.lower().startswith("https://"):
            return self.start_oauth_redirect(
                base, redirect_base=oauth_redirect_base(), return_path=return_path)
        if not self.loopback_supported:
            return {"ok": False, "error": (
                f"{self.label} only redirects to an HTTPS address, so this "
                "install needs a public HTTPS origin (QUILL_PUBLIC_BASE_URL) "
                f"or a {self.env_token_var} token.")}
        return self.start_oauth_loopback()

    def complete_connect(self, code: str, state: str,
                         *, redirect_uri: str) -> dict[str, Any]:
        return self.complete_oauth_redirect(code, state, redirect_uri=redirect_uri)

    def sync(self) -> dict[str, Any]:
        if not self.connected():
            return {"ok": False, "error": "not connected"}
        if self.progress().get("running"):
            return {"ok": True, "running": True, **self.progress()}

        def _run() -> None:
            from app.services.connectors import scheduler
            self._set_progress(running=True, error=None)
            try:
                res = scheduler.sync_connector(self, force=True)
                self._set_progress(running=False,
                                   error=None if res.get("ok") else res.get("error"))
            except Exception as exc:
                self._set_progress(running=False, error=str(exc))

        threading.Thread(target=_run, name=f"{self.id}-sync", daemon=True).start()
        return {"ok": True, "started": True}

    def disconnect(self) -> dict[str, Any]:
        out = self.clear_tokens()
        if self.env_token():
            # The env token is the operator's; remember the user said no.
            self._save_json(self._token_path(), {"env_opt_out": True})
            out["env_token_opted_out"] = True
        return out

    # --- shared fetch helpers ----------------------------------------------------------
    def _since(self, cursor: dict | None, now: float) -> float:
        cursor = dict(cursor or {})
        backfill_days = float(os.environ.get("QUILL_CONNECTOR_BACKFILL_DAYS", "7"))
        since = cursor.get("since")
        try:
            return float(since) if since is not None else now - backfill_days * 86400
        except (TypeError, ValueError):
            return now - backfill_days * 86400

    def lookup(self, need: str, *, timeout_s: float = 10.0) -> list[dict]:
        """Search assist: recent items whose title covers the need."""
        from app.services.slots import coverage
        deadline = time.time() + float(timeout_s)
        out: list[dict] = []
        try:
            now = time.time()
            items, _ = self.fetch_items(cursor={"since": now - 30 * 86400}, now=now)
            for it in items:
                if time.time() > deadline:
                    break
                if coverage(need, it.get("title") or "") >= 0.6:
                    out.append(it)
        except Exception as exc:
            print(f"[connectors] {self.id} lookup skipped ({exc}).")
        return out[:10]
