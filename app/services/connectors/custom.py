"""Custom MCP connectors — user-added servers Sparrow reaches from this machine.

Unlike Claude custom connectors (reached from Anthropic's cloud, so the
server must be public), Sparrow is local-first: the URL may be localhost,
Tailscale, or any host this install can reach.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from app.services.connectors import prefs


def _valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if not p.netloc:
        return False
    return True


def normalize_id(raw: str, *, label: str = "") -> str:
    s = (raw or "").strip().lower()
    if not s and label:
        s = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    if s and not s.startswith("mcp-"):
        # Keep namespace clear vs directory ids (google, slack, …).
        s = "mcp-" + s if not s.startswith("custom-") else s
    return s


class CustomConnector:
    """Thin status/connect surface for a user-registered MCP server URL."""

    availability = "ready"
    kind = "custom"
    category = "custom"
    auth = "none"

    def __init__(self, entry: dict[str, Any]):
        self.id = (entry.get("id") or "").strip().lower()
        self.label = (entry.get("label") or self.id).strip() or self.id
        self.url = (entry.get("url") or "").strip().rstrip("/")
        self.oauth_client_id = (entry.get("oauth_client_id") or "").strip()
        self.oauth_client_secret = (entry.get("oauth_client_secret") or "").strip()
        self.description = (
            entry.get("description")
            or f"Custom MCP server at {self.url}"
        )
        self.capabilities = list(entry.get("capabilities") or (
            "Tools advertised by the remote MCP server",
        ))
        self.tool_names = tuple(entry.get("tool_names") or (self.label,))
        self._connected = bool(entry.get("connected"))
        self._last_error = entry.get("last_error")

    def configured(self) -> bool:
        return bool(self.url) and _valid_url(self.url)

    def connected(self) -> bool:
        return self._connected and self.configured()

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "tool_names": list(self.tool_names),
            "availability": self.availability,
            "kind": self.kind,
            "category": self.category,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "auth": "oauth" if self.oauth_client_id else "none",
            "url": self.url,
            "configured": self.configured(),
            "connected": self.connected(),
            "error": self._last_error,
        }

    def begin_connect(self, *, public_base: str | None = None,
                      return_path: str | None = None) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "error": "invalid MCP URL"}
        # Reachability probe — Sparrow talks to the server from this machine.
        probe = self._probe()
        if not probe.get("ok"):
            self._persist_connected(False, error=probe.get("error"))
            return probe
        self._persist_connected(True, error=None)
        return {"ok": True, "mode": "local", "reachable": True,
                "tools": probe.get("tools") or []}

    def complete_connect(self, code: str, state: str,
                         *, redirect_uri: str) -> dict[str, Any]:
        return {"ok": False, "error": "custom MCP uses local connect, not OAuth callback"}

    def sync(self) -> dict[str, Any]:
        return self.begin_connect()

    def disconnect(self) -> dict[str, Any]:
        self._persist_connected(False, error=None)
        return {"ok": True}

    def _probe(self) -> dict[str, Any]:
        """Best-effort GET/POST against common MCP HTTP shapes."""
        url = self.url
        # Try a few well-known discovery paths; any 2xx counts as reachable.
        candidates = [
            url,
            f"{url}/",
            f"{url}/sse",
            f"{url}/mcp",
            f"{url}/health",
        ]
        last_err = "unreachable"
        for candidate in candidates:
            try:
                req = urllib.request.Request(
                    candidate,
                    headers={"Accept": "application/json, text/event-stream, */*",
                             "User-Agent": "Sparrow-connectors/1.0"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=4) as resp:
                    code = getattr(resp, "status", 200) or 200
                    if 200 <= int(code) < 500:
                        body = b""
                        try:
                            body = resp.read(4096)
                        except Exception:
                            pass
                        tools = []
                        try:
                            data = json.loads(body.decode("utf-8", "replace"))
                            if isinstance(data, dict) and isinstance(data.get("tools"), list):
                                tools = data["tools"]
                        except Exception:
                            pass
                        return {"ok": True, "tools": tools, "probed": candidate}
            except urllib.error.HTTPError as exc:
                # 401/403 still proves the host is up — treat as reachable.
                if exc.code in (401, 403, 405, 406):
                    return {"ok": True, "tools": [], "probed": candidate,
                            "auth_required": True}
                last_err = f"HTTP {exc.code}"
            except Exception as exc:
                last_err = str(exc) or "unreachable"
        return {"ok": False, "error": last_err}

    def _persist_connected(self, connected: bool, *, error: str | None) -> None:
        self._connected = connected
        self._last_error = error
        entry = {
            "id": self.id,
            "label": self.label,
            "url": self.url,
            "oauth_client_id": self.oauth_client_id,
            "oauth_client_secret": self.oauth_client_secret,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "tool_names": list(self.tool_names),
            "connected": connected,
            "last_error": error,
        }
        prefs.upsert_custom(entry)


def load_all() -> list[CustomConnector]:
    return [CustomConnector(e) for e in prefs.list_custom()
            if (e.get("id") or "").strip()]


def add(*, label: str, url: str, connector_id: str | None = None,
        oauth_client_id: str | None = None,
        oauth_client_secret: str | None = None,
        description: str | None = None) -> dict[str, Any]:
    label = (label or "").strip()
    url = (url or "").strip().rstrip("/")
    if not label:
        return {"ok": False, "error": "name required"}
    if not _valid_url(url):
        return {"ok": False, "error": "url must be http(s) with a host"}
    cid = normalize_id(connector_id or "", label=label)
    if not re.match(r"^[a-z][a-z0-9_-]{1,47}$", cid):
        return {"ok": False, "error": "id must be lowercase letters, digits, _-"}
    # Reserved directory ids.
    from app.services.connectors import catalog
    if cid in catalog.DIRECTORY:
        return {"ok": False, "error": f"id '{cid}' is reserved"}
    entry = {
        "id": cid,
        "label": label,
        "url": url,
        "oauth_client_id": (oauth_client_id or "").strip(),
        "oauth_client_secret": (oauth_client_secret or "").strip(),
        "description": (description or "").strip()
            or f"Custom MCP server at {url}",
        "capabilities": ["Tools advertised by the remote MCP server"],
        "tool_names": [label],
        "connected": False,
        "last_error": None,
    }
    prefs.upsert_custom(entry)
    return {"ok": True, "connector": CustomConnector(entry).status()}


def remove(connector_id: str) -> dict[str, Any]:
    return prefs.remove_custom(connector_id)
