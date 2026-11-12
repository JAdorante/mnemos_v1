"""Slack connector — messages in the channels and DMs the user is in,
read-only, as the user.

OAuth v2 with *user* scopes only (``user_scope``), so the token is the
user's own (``xoxp-…``) and sees exactly what they see; it comes back nested
under ``authed_user`` and is flattened in ``_normalize_token_response``.
Slack redirects only to HTTPS, so there is no desktop loopback: an install
without a public HTTPS origin uses a ``SLACK_USER_TOKEN`` instead.

Reads: ``conversations.list`` for the conversations the user is a member of,
then one ``conversations.history`` per conversation past the cursor (capped),
with ``users.list`` to turn ids into names. Unlike mail, a message *is* its
text, so this lands bodies, the way iMessage does: a direct or group message
is stamped ``sensitive`` and a private channel ``personal``, so neither
leaves the machine on its own. The consent sentence at Connect says so.
Join/leave notices and bot chatter are skipped. Items land through
connectors/scheduler.py as ``slack.message`` and ``slack.dm``.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import urlencode

from app.config import settings
from app.services.connectors.oauth_base import (
    OAuthApiError,
    OAuthConnectorBase,
    clip,
)

API = "https://slack.com/api"
BODY_CAP = 2000
_SKIP_SUBTYPES = {"channel_join", "channel_leave", "group_join", "group_leave",
                  "channel_topic", "channel_purpose", "channel_name",
                  "bot_message", "channel_archive", "channel_unarchive",
                  "pinned_item", "unpinned_item", "tombstone"}
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)\|([^>]*)>")
_LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")


class SlackConnector(OAuthConnectorBase):
    id = "slack"
    label = "Slack"
    tool_names = ("Slack",)
    category = "productivity"
    description = (
        "Read-only messages from the channels and DMs you are in, as you. "
        "You sign in on Slack's screen — Sparrow never sees your password."
    )
    capabilities = (
        "Remember what was said in your channels",
        "Keep DMs as sensitive memory that never leaves this machine",
        "Close tasks when someone answers you in Slack",
    )
    source_prefixes = ("slack.message", "slack.dm")
    sync_interval_s = 300
    sync_blurb = "new messages in the Slack channels and direct messages you are in"
    consent_notice = (
        "Sparrow will read new messages in the Slack channels and direct "
        "messages you are in about every 5 minutes, read-only, and keep what "
        "they say as observed memory on this machine. Direct messages are "
        "stamped sensitive and private channels personal, so they never leave "
        "this machine unless you share them. Nothing is ever posted to Slack."
    )

    authorize_url = "https://slack.com/oauth/v2/authorize"
    token_url = "https://slack.com/api/oauth.v2.access"
    scopes = ("channels:history", "channels:read", "groups:history", "groups:read",
              "im:history", "im:read", "mpim:history", "mpim:read", "users:read")
    scope_separator = ","
    loopback_supported = False
    client_id_env = "SLACK_OAUTH_CLIENT_ID"
    client_secret_env = "SLACK_OAUTH_CLIENT_SECRET"
    env_token_var = "SLACK_USER_TOKEN"
    env_token_label = "user token (xoxp-…)"

    def _cfg(self):
        return settings.slack

    def _auth_url(self, redirect: str, state: str) -> str:
        # user_scope, not scope: we want the user's token, no bot installed.
        q = {"client_id": self._client_id(), "redirect_uri": redirect,
             "user_scope": self._scope_string(), "state": state}
        return f"{self.authorize_url}?{urlencode(q)}"

    def _normalize_token_response(self, data: dict) -> dict:
        if data.get("ok") is False:
            raise RuntimeError(str(data.get("error") or "slack refused the exchange"))
        user = data.get("authed_user") or {}
        if not user.get("access_token"):
            raise RuntimeError("slack returned no user token (authed_user.access_token)")
        flat = {
            "access_token": user["access_token"],
            "scope": user.get("scope"),
            "token_type": user.get("token_type") or "user",
            "user_id": user.get("id"),
            "team_id": (data.get("team") or {}).get("id"),
            "team_name": (data.get("team") or {}).get("name"),
        }
        if user.get("refresh_token"):
            flat["refresh_token"] = user["refresh_token"]
            flat["expires_in"] = user.get("expires_in")
        return flat

    # --- Web API ----------------------------------------------------------------------
    def _slack(self, method: str, **params) -> dict:
        q = urlencode({k: v for k, v in params.items() if v not in (None, "")})
        data, _ = self._request(f"{API}/{method}" + (f"?{q}" if q else ""))
        if not isinstance(data, dict):
            raise RuntimeError(f"{method} returned a non-object")
        if not data.get("ok"):
            raise OAuthApiError(200, f"{method}: {data.get('error') or 'not ok'}")
        return data

    def _whoami(self) -> dict[str, Any]:
        me = self._slack("auth.test")
        return {"account": me.get("user") or "", "name": me.get("team") or "",
                "user_id": me.get("user_id"), "team_url": me.get("url")}

    def _max_conversations(self) -> int:
        try:
            return max(1, int(os.environ.get("QUILL_SLACK_MAX_CONVERSATIONS", "60")))
        except ValueError:
            return 60

    def fetch_conversations(self) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(10):
            data = self._slack("conversations.list",
                               types="public_channel,private_channel,mpim,im",
                               exclude_archived="true", limit=200, cursor=cursor)
            for c in data.get("channels") or []:
                if not isinstance(c, dict) or not c.get("id"):
                    continue
                if c.get("is_im") or c.get("is_mpim") or c.get("is_member"):
                    out.append(c)
            cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        return out

    def fetch_history(self, channel: str, *, since: float) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(2):
            data = self._slack("conversations.history", channel=channel,
                               oldest=f"{since:.6f}", limit=200, cursor=cursor)
            out.extend(m for m in (data.get("messages") or []) if isinstance(m, dict))
            cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not data.get("has_more") or not cursor:
                break
        return out

    def fetch_users(self) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            cursor = None
            for _ in range(5):
                data = self._slack("users.list", limit=200, cursor=cursor)
                for u in data.get("members") or []:
                    if isinstance(u, dict) and u.get("id"):
                        prof = u.get("profile") or {}
                        out[str(u["id"])] = str(u.get("real_name") or prof.get("real_name")
                                                or prof.get("display_name")
                                                or u.get("name") or u["id"])
                cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "")
                if not cursor:
                    break
        except Exception as exc:
            print(f"[connectors] slack users.list skipped ({exc}).")
        return out

    # --- mapping ----------------------------------------------------------------------
    @staticmethod
    def render_text(raw: str, users: dict[str, str]) -> str:
        s = _MENTION_RE.sub(lambda m: "@" + users.get(m.group(1), m.group(1)), raw or "")
        s = _CHANNEL_RE.sub(lambda m: "#" + (m.group(2) or m.group(1)), s)
        s = _LINK_RE.sub(lambda m: m.group(2) or m.group(1), s)
        s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        return clip(s, BODY_CAP)

    @staticmethod
    def _permalink(team_url: str, channel: str, ts: str) -> str:
        if not team_url or not channel or not ts:
            return ""
        return f"{team_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"

    def _conv_name(self, c: dict, users: dict) -> str:
        if c.get("is_im"):
            return "DM with " + users.get(str(c.get("user") or ""), str(c.get("user") or "?"))
        if c.get("is_mpim"):
            return "group DM " + str(c.get("name") or "")
        return "#" + str(c.get("name") or c.get("id") or "")

    def _message_item(self, m: dict, *, conv: dict, users: dict,
                      me: str, team_url: str) -> dict | None:
        if m.get("subtype") in _SKIP_SUBTYPES or m.get("bot_id"):
            return None
        text = self.render_text(str(m.get("text") or ""), users)
        if not text and not m.get("files"):
            return None
        if not text:
            names = [str(f.get("name") or "file") for f in (m.get("files") or [])
                     if isinstance(f, dict)]
            text = "(shared " + ", ".join(names) + ")"
        ts_raw = str(m.get("ts") or "")
        try:
            ts = float(ts_raw)
        except ValueError:
            ts = time.time()
        author_id = str(m.get("user") or "")
        author = users.get(author_id, author_id or "someone")
        is_dm = bool(conv.get("is_im") or conv.get("is_mpim"))
        is_private = bool(conv.get("is_private")) and not is_dm
        where = self._conv_name(conv, users)
        cid = str(conv.get("id") or "")
        people = [author]
        if conv.get("is_im") and conv.get("user"):
            other = users.get(str(conv["user"]), str(conv["user"]))
            if other not in people:
                people.append(other)
        url = self._permalink(team_url, cid, ts_raw)
        item: dict[str, Any] = {
            "kind": "dm" if is_dm else "message",
            "external_id": f"{cid}:{ts_raw}",
            "thread_id": str(m.get("thread_ts") or cid),
            "ts": ts, "title": f"{where}: {clip(text, 80)}",
            "text": f"{where} · {author}: {text}",
            "people": people, "url": url or None,
            "from": author, "from_self": bool(me and author_id == me),
            "direction": "outbound" if (me and author_id == me) else "inbound",
            "meta": {"channel": cid, "channel_name": conv.get("name"),
                     "is_private": is_private, "is_dm": is_dm,
                     "thread_ts": m.get("thread_ts"), "author_id": author_id,
                     "reply_count": m.get("reply_count")},
        }
        if is_dm:
            item["meta"]["privacy_class"] = "sensitive"
        elif is_private:
            item["meta"]["privacy_class"] = "personal"
        return item

    # --- background capture ---------------------------------------------------------
    def fetch_items(self, *, cursor: dict | None = None,
                    now: float | None = None) -> tuple[list[dict], dict]:
        from app.services.connectors.oauth_base import FETCH_OVERLAP_S
        now = float(now if now is not None else time.time())
        cursor = dict(cursor or {})
        since = self._since(cursor, now)
        me = str(cursor.get("user_id") or "")
        team_url = str(cursor.get("team_url") or "")
        if not me or not team_url:
            try:
                who = self._whoami()
                me = me or str(who.get("user_id") or "")
                team_url = team_url or str(who.get("team_url") or "")
            except Exception as exc:
                print(f"[connectors] slack auth.test skipped ({exc}).")
        convs = self.fetch_conversations()[: self._max_conversations()]
        users = self.fetch_users() if convs else {}
        items: list[dict] = []
        for c in convs:
            try:
                history = self.fetch_history(str(c["id"]), since=since)
            except OAuthApiError as exc:
                if exc.status == 429:
                    raise
                print(f"[connectors] slack history skipped for {c.get('id')} ({exc}).")
                continue
            for m in history:
                it = self._message_item(m, conv=c, users=users, me=me, team_url=team_url)
                if it:
                    items.append(it)
        items.sort(key=lambda it: it["ts"])
        self._note_fetch(items, now)
        nxt = {**cursor, "since": now - FETCH_OVERLAP_S}
        if me:
            nxt["user_id"] = me
        if team_url:
            nxt["team_url"] = team_url
        return items, nxt


slack = SlackConnector()
