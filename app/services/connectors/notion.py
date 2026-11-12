"""Notion connector — pages and databases the integration can see, read-only.

Public-integration OAuth (``api.notion.com/v1/oauth``): the exchange is a
JSON body under HTTP Basic ``client_id:client_secret``, not the form post
the other providers take, so ``_token_request`` is overridden. There are no
scopes; what the integration may read is set on the integration itself, and
the user picks which pages to share on Notion's consent screen. An internal
integration secret (``NOTION_TOKEN``) works for an install without a public
integration. Notion tokens do not expire unless the integration opted into
refresh tokens, which the base handles when ``refresh_token`` is present.

Reads: ``POST /v1/search`` sorted by last edit, newest first, stopping at
the cursor; page text comes from one ``GET /v1/blocks/{id}/children`` per
changed page (top-level blocks only, capped), and ``GET /v1/users`` maps the
last editor to a name. Items land through connectors/scheduler.py as
``notion.page`` and ``notion.database``.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any
from urllib.parse import urlencode

from app.config import settings
from app.services.connectors.oauth_base import (
    OAuthConnectorBase,
    clip,
    parse_iso,
)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
BODY_CAP = 1500
PAGE = 100
_TEXT_BLOCKS = ("paragraph", "heading_1", "heading_2", "heading_3",
                "bulleted_list_item", "numbered_list_item", "to_do", "quote",
                "callout", "toggle", "code")


class NotionConnector(OAuthConnectorBase):
    id = "notion"
    label = "Notion"
    tool_names = ("Notion",)
    category = "productivity"
    description = (
        "Read-only pages and databases you share with Sparrow. "
        "You pick the pages on Notion's own screen — Sparrow never sees your password."
    )
    capabilities = (
        "Remember pages as they change",
        "Track databases and their rows",
        "Find a page by what it says, not just its title",
    )
    source_prefixes = ("notion.page", "notion.database")
    sync_interval_s = 600
    sync_blurb = "Notion pages and databases shared with it that changed"

    authorize_url = "https://api.notion.com/v1/oauth/authorize"
    token_url = "https://api.notion.com/v1/oauth/token"
    scopes = ()
    extra_authorize_params = {"owner": "user"}
    loopback_host = "localhost"
    loopback_port = 48623
    client_id_env = "NOTION_OAUTH_CLIENT_ID"
    client_secret_env = "NOTION_OAUTH_CLIENT_SECRET"
    env_token_var = "NOTION_TOKEN"
    env_token_label = "internal integration secret"

    def _cfg(self):
        return settings.notion

    def _auth_url(self, redirect: str, state: str) -> str:
        # No scope parameter at all: Notion rejects an empty one.
        q = {"client_id": self._client_id(), "redirect_uri": redirect,
             "response_type": "code", "state": state}
        q.update(self.extra_authorize_params)
        return f"{self.authorize_url}?{urlencode(q)}"

    def _token_request(self, fields: dict[str, str]) -> dict:
        basic = base64.b64encode(
            f"{self._client_id()}:{self._client_secret()}".encode()).decode("ascii")
        data = self._post_token_endpoint(
            json.dumps(fields).encode("utf-8"),
            {"Content-Type": "application/json", "Authorization": f"Basic {basic}"})
        if data.get("error"):
            raise RuntimeError(
                f"{data.get('error')}: {data.get('error_description') or ''}".strip())
        if not data.get("access_token"):
            raise RuntimeError("token endpoint returned no access_token")
        return data

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}",
                "Notion-Version": NOTION_VERSION}

    def _whoami(self) -> dict[str, Any]:
        me, _ = self._request(f"{API}/users/me")
        me = me or {}
        bot = me.get("bot") or {}
        owner = (bot.get("owner") or {}).get("user") or {}
        tok = self.load_tokens()
        return {"account": owner.get("name") or me.get("name") or "",
                "name": tok.get("workspace_name") or bot.get("workspace_name") or ""}

    # --- reads ------------------------------------------------------------------------
    def _max_pages(self) -> int:
        try:
            return max(1, int(os.environ.get("QUILL_NOTION_MAX_PAGES", "5")))
        except ValueError:
            return 5

    def _max_bodies(self) -> int:
        try:
            return max(0, int(os.environ.get("QUILL_NOTION_MAX_BODIES", "50")))
        except ValueError:
            return 50

    def search(self, kind: str, *, since: float) -> list[dict]:
        """Pages or databases edited since the cursor, newest first."""
        out: list[dict] = []
        cursor = None
        for _ in range(self._max_pages()):
            body: dict[str, Any] = {
                "filter": {"property": "object", "value": kind},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
                "page_size": PAGE,
            }
            if cursor:
                body["start_cursor"] = cursor
            data, _ = self._request(f"{API}/search", method="POST", body=body)
            data = data or {}
            stop = False
            for r in data.get("results") or []:
                if not isinstance(r, dict):
                    continue
                if parse_iso(r.get("last_edited_time")) < since:
                    stop = True
                    break
                out.append(r)
            if stop or not data.get("has_more") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]
        return out

    def page_text(self, page_id: str) -> str:
        data, _ = self._request(f"{API}/blocks/{page_id}/children?page_size={PAGE}")
        lines: list[str] = []
        total = 0
        for b in ((data or {}).get("results") or []):
            if not isinstance(b, dict):
                continue
            t = str(b.get("type") or "")
            if t not in _TEXT_BLOCKS:
                continue
            rich = (b.get(t) or {}).get("rich_text") or []
            text = "".join(str(x.get("plain_text") or "") for x in rich
                           if isinstance(x, dict)).strip()
            if not text:
                continue
            if t == "to_do":
                text = ("[x] " if (b.get(t) or {}).get("checked") else "[ ] ") + text
            elif t.startswith("bulleted") or t.startswith("numbered"):
                text = "- " + text
            lines.append(text)
            total += len(text)
            if total >= BODY_CAP:
                break
        return clip("\n".join(lines), BODY_CAP)

    def fetch_users(self) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            data, _ = self._request(f"{API}/users?page_size={PAGE}")
        except Exception:
            return out
        for u in ((data or {}).get("results") or []):
            if isinstance(u, dict) and u.get("id") and u.get("name"):
                out[str(u["id"])] = str(u["name"])
        return out

    # --- mapping ----------------------------------------------------------------------
    @staticmethod
    def _rich(parts: Any) -> str:
        return "".join(str(x.get("plain_text") or "") for x in (parts or [])
                       if isinstance(x, dict)).strip()

    def _page_title(self, r: dict) -> str:
        for prop in (r.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                return self._rich(prop.get("title"))
        return ""

    @staticmethod
    def _parent(r: dict) -> tuple[str, str]:
        p = r.get("parent") or {}
        t = str(p.get("type") or "")
        return t, str(p.get(t) or "") if t and t != "workspace" else ""

    def _page_item(self, r: dict, *, users: dict, text: str) -> dict:
        title = clip(self._page_title(r), 200) or "Untitled"
        editor = users.get(str((r.get("last_edited_by") or {}).get("id") or ""), "")
        ptype, pid = self._parent(r)
        bits = [f"Edited by {editor}" if editor else "",
                "in a database" if ptype == "database_id" else "",
                "archived" if r.get("archived") else ""]
        lines = [l for l in (f"Notion page {title}", " · ".join(b for b in bits if b),
                             str(r.get("url") or "")) if l]
        if text:
            lines += ["", text]
        return {
            "kind": "page", "external_id": f"page:{r.get('id')}",
            "thread_id": pid or None,
            "ts": parse_iso(r.get("last_edited_time")) or parse_iso(r.get("created_time")),
            "title": title, "text": "\n".join(lines),
            "people": [editor] if editor else [], "url": r.get("url"),
            "meta": {"parent_type": ptype, "parent_id": pid, "editor": editor,
                     "archived": bool(r.get("archived")),
                     "created_time": r.get("created_time")},
        }

    def _database_item(self, r: dict, *, users: dict) -> dict:
        title = clip(self._rich(r.get("title")), 200) or "Untitled database"
        desc = clip(self._rich(r.get("description")), 300)
        editor = users.get(str((r.get("last_edited_by") or {}).get("id") or ""), "")
        props = [str(k) for k in (r.get("properties") or {}).keys()][:12]
        bits = [f"Edited by {editor}" if editor else "",
                f"Columns: {', '.join(props)}" if props else ""]
        lines = [f"Notion database {title}" + (f": {desc}" if desc else ""),
                 " · ".join(b for b in bits if b), str(r.get("url") or "")]
        return {
            "kind": "database", "external_id": f"database:{r.get('id')}",
            "ts": parse_iso(r.get("last_edited_time")),
            "title": title, "text": "\n".join(l for l in lines if l),
            "people": [editor] if editor else [], "url": r.get("url"),
            "meta": {"editor": editor, "columns": props},
        }

    # --- background capture ---------------------------------------------------------
    def fetch_items(self, *, cursor: dict | None = None,
                    now: float | None = None) -> tuple[list[dict], dict]:
        from app.services.connectors.oauth_base import FETCH_OVERLAP_S
        now = float(now if now is not None else time.time())
        cursor = dict(cursor or {})
        since = self._since(cursor, now)
        pages = self.search("page", since=since)
        dbs = self.search("database", since=since)
        users = self.fetch_users() if (pages or dbs) else {}
        items: list[dict] = []
        budget = self._max_bodies()
        for r in pages:
            text = ""
            if budget > 0 and r.get("id") and not r.get("archived"):
                budget -= 1
                try:
                    text = self.page_text(str(r["id"]))
                except Exception as exc:
                    print(f"[connectors] notion page text skipped ({exc}).")
            items.append(self._page_item(r, users=users, text=text))
        items += [self._database_item(r, users=users) for r in dbs]
        self._note_fetch(items, now)
        return items, {**cursor, "since": now - FETCH_OVERLAP_S}


notion = NotionConnector()
