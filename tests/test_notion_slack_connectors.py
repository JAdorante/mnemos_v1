"""Notion and Slack connectors — registry (planned list now empty), Notion's
Basic-auth JSON exchange and search/body mapping, Slack's nested user token,
HTTPS-only connect, conversation/history mapping with privacy stamps, and
landing through the scheduler."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import connectors
from app.services.connectors import notion as nt
from app.services.connectors import oauth_base as ob
from app.services.connectors import slack as sl

NOW = 1_762_800_000.0
ISO_RECENT = "2025-11-10T12:00:00.000Z"


class RegistryTests(unittest.TestCase):
    def test_both_ready_and_nothing_planned(self) -> None:
        from app.services.connectors.planned import PLANNED
        self.assertEqual(PLANNED, ())
        self.assertIsInstance(connectors.get("notion"), nt.NotionConnector)
        self.assertIsInstance(connectors.get("slack"), sl.SlackConnector)
        self.assertEqual(connectors.for_tool("Notion").id, "notion")
        self.assertEqual(connectors.for_tool("Slack").id, "slack")
        self.assertTrue(all(c.availability == "ready" for c in connectors.all()
                            if c.kind == "directory"))
        rows = {c["id"]: c for c in connectors.directory()["connectors"]}
        for cid in ("notion", "slack"):
            self.assertEqual(rows[cid]["category"], "productivity")
            self.assertNotIn("planned", rows[cid]["description"].lower())
        from app.services import api_auth
        self.assertTrue(api_auth.path_is_exempt("/oauth/notion/callback", "GET"))
        self.assertTrue(api_auth.path_is_exempt("/oauth/slack/callback", "GET"))

    def test_slack_consent_sentence_names_the_stamps(self) -> None:
        from app.services.connectors import scheduler
        s = scheduler.consent_sentence(sl.slack)
        self.assertIn("sensitive", s); self.assertIn("Nothing is ever posted", s)


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data = Path(self._td.name)
        self._env = patch.dict(os.environ, {
            "NOTION_TOKEN": "", "SLACK_USER_TOKEN": "",
            "QUILL_PUBLIC_BASE_URL": "", "QUILL_PEER_BASE_URL": "",
            "QUILL_OAUTH_REDIRECT_BASE": ""}, clear=False)
        self._env.start(); self.addCleanup(self._env.stop)
        for cls, scopes in ((nt.NotionConnector, ""),
                            (sl.SlackConnector, " ".join(sl.SlackConnector.scopes))):
            cid = cls.id
            cfg = SimpleNamespace(
                client_id=f"{cid}-client", client_secret=f"{cid}-secret", scopes=scopes,
                token_path=str(self.data / "connectors" / cid / "token.json"),
                oauth_state_path=str(self.data / "connectors" / cid / "oauth_state.json"))
            p = patch.object(cls, "_cfg", lambda self, _c=cfg: _c)
            p.start(); self.addCleanup(p.stop)
        nt.notion._last_counts = {}; sl.slack._last_counts = {}


class _Resp:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.body


class NotionOAuthTests(_Sandbox):
    def test_auth_url_has_owner_and_no_scope(self) -> None:
        r = nt.notion.begin_connect(public_base="https://seat.example.com")
        self.assertTrue(r["auth_url"].startswith("https://api.notion.com/v1/oauth/authorize?"))
        self.assertIn("owner=user", r["auth_url"])
        self.assertNotIn("scope=", r["auth_url"])
        self.assertIn("%2Foauth%2Fnotion%2Fcallback", r["auth_url"])

    def test_exchange_is_json_under_basic_auth(self) -> None:
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.get_header("Authorization")
            captured["ctype"] = req.get_header("Content-type")
            return _Resp(b'{"access_token":"ntn_x","workspace_name":"Acme","bot_id":"b1"}')

        with patch.object(ob, "urlopen", fake_urlopen):
            tok = nt.notion._exchange_code("c", "https://x/oauth/notion/callback")
        self.assertEqual(tok["workspace_name"], "Acme")
        self.assertEqual(captured["body"], {"code": "c", "grant_type": "authorization_code",
                                            "redirect_uri": "https://x/oauth/notion/callback"})
        self.assertEqual(captured["ctype"], "application/json")
        expect = base64.b64encode(b"notion-client:notion-secret").decode()
        self.assertEqual(captured["auth"], f"Basic {expect}")

    def test_exchange_error_surfaces(self) -> None:
        with patch.object(ob, "urlopen", lambda req, timeout=30: _Resp(
                b'{"error":"invalid_grant","error_description":"bad code"}')):
            with self.assertRaises(RuntimeError):
                nt.notion._exchange_code("c", "https://x/oauth/notion/callback")

    def test_env_token_and_headers(self) -> None:
        with patch.dict(os.environ, {"NOTION_TOKEN": "ntn_env"}, clear=False):
            h = nt.notion._auth_headers()
            self.assertEqual(h["Authorization"], "Bearer ntn_env")
            self.assertEqual(h["Notion-Version"], nt.NOTION_VERSION)


NT_PAGE_OLD = "2024-01-01T00:00:00.000Z"
NT_SEARCH_PAGES_1 = {"results": [
    {"object": "page", "id": "p-1", "url": "https://www.notion.so/Roadmap-p1",
     "last_edited_time": ISO_RECENT, "created_time": "2025-10-01T00:00:00.000Z",
     "last_edited_by": {"id": "u-1"}, "archived": False,
     "parent": {"type": "database_id", "database_id": "db-1"},
     "properties": {"Name": {"type": "title", "title": [{"plain_text": "Q4 "},
                                                        {"plain_text": "Roadmap"}]},
                    "Status": {"type": "select"}}},
    {"object": "page", "id": "p-2", "url": "https://www.notion.so/Untitled-p2",
     "last_edited_time": ISO_RECENT, "last_edited_by": {"id": "u-9"}, "archived": True,
     "parent": {"type": "workspace", "workspace": True}, "properties": {}},
], "has_more": True, "next_cursor": "cur-2"}
NT_SEARCH_PAGES_2 = {"results": [
    {"object": "page", "id": "p-old", "last_edited_time": NT_PAGE_OLD,
     "properties": {"title": {"type": "title", "title": [{"plain_text": "Old"}]}}},
], "has_more": True, "next_cursor": "cur-3"}
NT_SEARCH_DBS = {"results": [
    {"object": "database", "id": "db-1", "url": "https://www.notion.so/db1",
     "last_edited_time": ISO_RECENT, "last_edited_by": {"id": "u-1"},
     "title": [{"plain_text": "Projects"}], "description": [{"plain_text": "All projects"}],
     "properties": {"Name": {}, "Status": {}, "Owner": {}}},
], "has_more": False}
NT_BLOCKS = {"results": [
    {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Goals"}]}},
    {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Ship "},
                                                      {"plain_text": "search."}]}},
    {"type": "to_do", "to_do": {"checked": True, "rich_text": [{"plain_text": "Spec"}]}},
    {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "Bench"}]}},
    {"type": "image", "image": {}},
    {"type": "paragraph", "paragraph": {"rich_text": []}},
]}


def _fake_notion(calls: list):
    def req(self, url, *, method="GET", body=None, headers=None):
        calls.append((url, body))
        if url.endswith("/search"):
            kind = body["filter"]["value"]
            if kind == "database":
                return NT_SEARCH_DBS, {}
            return (NT_SEARCH_PAGES_2 if body.get("start_cursor") else NT_SEARCH_PAGES_1), {}
        if "/blocks/p-1/children" in url:
            return NT_BLOCKS, {}
        if "/users?" in url:
            return {"results": [{"id": "u-1", "name": "Priya Natarajan"}]}, {}
        if url.endswith("/users/me"):
            return {"name": "Sparrow bot", "bot": {"owner": {"user": {"name": "Sam"}},
                                                   "workspace_name": "Acme"}}, {}
        raise AssertionError(url)
    return req


class NotionMappingTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        nt.notion._save_tokens({"access_token": "ntn_x", "workspace_name": "Acme"})

    def test_pages_and_databases_shape_stop_at_cursor_and_bodies(self) -> None:
        calls: list = []
        with patch.object(nt.NotionConnector, "_request", _fake_notion(calls)):
            items, cursor = nt.notion.fetch_items(cursor={"since": NOW - 86400}, now=NOW)
        self.assertEqual([(i["kind"], i["external_id"]) for i in items],
                         [("page", "page:p-1"), ("page", "page:p-2"),
                          ("database", "database:db-1")])
        p = items[0]
        self.assertEqual(p["title"], "Q4 Roadmap")
        self.assertEqual(p["thread_id"], "db-1")
        self.assertEqual(p["people"], ["Priya Natarajan"])
        self.assertIn("Notion page Q4 Roadmap\nEdited by Priya Natarajan · in a database\n"
                      "https://www.notion.so/Roadmap-p1\n\nGoals\nShip search.\n[x] Spec\n- Bench",
                      p["text"])
        self.assertEqual(items[1]["title"], "Untitled")
        self.assertTrue(items[1]["meta"]["archived"])
        d = items[2]
        self.assertIn("Notion database Projects: All projects", d["text"])
        self.assertEqual(d["meta"]["columns"], ["Name", "Status", "Owner"])
        # Paged once past the cursor and stopped on the old page; the archived
        # page got no body call.
        searches = [b for u, b in calls if u.endswith("/search")]
        self.assertEqual([b.get("start_cursor") for b in searches if b["filter"]["value"] == "page"],
                         [None, "cur-2"])
        self.assertEqual([u for u, b in calls if "/blocks/" in u],
                         ["https://api.notion.com/v1/blocks/p-1/children?page_size=100"])
        self.assertLess(cursor["since"], NOW)
        self.assertIn("1 database, 2 pages", nt.notion.status()["status_detail"])

    def test_body_budget_zero_skips_block_reads(self) -> None:
        calls: list = []
        with patch.dict(os.environ, {"QUILL_NOTION_MAX_BODIES": "0"}), \
             patch.object(nt.NotionConnector, "_request", _fake_notion(calls)):
            items, _ = nt.notion.fetch_items(cursor={"since": NOW - 86400}, now=NOW)
        self.assertFalse([u for u, b in calls if "/blocks/" in u])
        self.assertNotIn("Goals", items[0]["text"])

    def test_whoami(self) -> None:
        with patch.object(nt.NotionConnector, "_request", _fake_notion([])):
            self.assertEqual(nt.notion._whoami(), {"account": "Sam", "name": "Acme"})


class SlackOAuthTests(_Sandbox):
    def test_auth_url_uses_user_scope(self) -> None:
        r = sl.slack.begin_connect(public_base="https://seat.example.com")
        self.assertTrue(r["auth_url"].startswith("https://slack.com/oauth/v2/authorize?"))
        self.assertIn("user_scope=channels%3Ahistory%2Cchannels%3Aread", r["auth_url"])
        self.assertNotIn("&scope=", r["auth_url"])

    def test_no_loopback_without_https(self) -> None:
        r = sl.slack.begin_connect(public_base="http://127.0.0.1:8000")
        self.assertFalse(r["ok"]); self.assertIn("HTTPS", r["error"])
        self.assertIn("SLACK_USER_TOKEN", r["error"])
        self.assertFalse(sl.slack.start_oauth_loopback()["ok"])

    def test_nested_user_token_is_flattened_and_bot_token_ignored(self) -> None:
        payload = {"ok": True, "access_token": "", "token_type": "bot",
                   "team": {"id": "T1", "name": "Acme"},
                   "authed_user": {"id": "U1", "access_token": "xoxp-u",
                                   "scope": "channels:history,channels:read,users:read",
                                   "token_type": "user"}}
        with patch.object(ob, "urlopen", lambda req, timeout=30: _Resp(json.dumps(payload).encode())):
            tok = sl.slack._exchange_code("c", "https://x/oauth/slack/callback")
        self.assertEqual(tok["access_token"], "xoxp-u")
        self.assertEqual(tok["user_id"], "U1"); self.assertEqual(tok["team_name"], "Acme")
        self.assertNotIn("refresh_token", tok)
        bad = {"ok": False, "error": "invalid_code"}
        with patch.object(ob, "urlopen", lambda req, timeout=30: _Resp(json.dumps(bad).encode())):
            with self.assertRaises(RuntimeError):
                sl.slack._exchange_code("c", "https://x/oauth/slack/callback")
        wide = dict(payload); wide["authed_user"] = {**payload["authed_user"],
                                                     "scope": "channels:history,chat:write"}
        with patch.object(ob, "urlopen", lambda req, timeout=30: _Resp(json.dumps(wide).encode())):
            with self.assertRaises(PermissionError):
                sl.slack._exchange_code("c", "https://x/oauth/slack/callback")

    def test_rotated_token_keeps_refresh_fields(self) -> None:
        payload = {"ok": True, "team": {"id": "T1"},
                   "authed_user": {"id": "U1", "access_token": "xoxe-u",
                                   "refresh_token": "xoxe-r", "expires_in": 43200,
                                   "scope": "users:read"}}
        with patch.object(ob, "urlopen", lambda req, timeout=30: _Resp(json.dumps(payload).encode())):
            tok = sl.slack._refresh_token("xoxe-old")
        self.assertEqual((tok["refresh_token"], tok["expires_in"]), ("xoxe-r", 43200))


SL_CONVS = {"ok": True, "channels": [
    {"id": "C1", "name": "eng", "is_channel": True, "is_member": True, "is_private": False},
    {"id": "C2", "name": "random", "is_channel": True, "is_member": False},
    {"id": "G1", "name": "leads", "is_group": True, "is_member": True, "is_private": True},
    {"id": "D1", "is_im": True, "user": "U2"},
], "response_metadata": {"next_cursor": ""}}
SL_HISTORY = {
    "C1": {"ok": True, "messages": [
        {"type": "message", "user": "U2", "ts": "1762776000.000100",
         "text": "<@U1> can you look at <https://github.com/acme/web/pull/7|PR 7> &amp; reply in <#C1|eng>?",
         "reply_count": 2},
        {"type": "message", "subtype": "channel_join", "user": "U3", "ts": "1762776001.000000",
         "text": "<@U3> has joined"},
        {"type": "message", "bot_id": "B1", "ts": "1762776002.000000", "text": "deploy done"},
        {"type": "message", "user": "U1", "ts": "1762776003.000000", "text": "",
         "files": [{"name": "spec.pdf"}]},
    ], "has_more": False},
    "G1": {"ok": True, "messages": [
        {"type": "message", "user": "U1", "ts": "1762776010.000000", "text": "pricing draft v2",
         "thread_ts": "1762775000.000000"}], "has_more": False},
    "D1": {"ok": True, "messages": [
        {"type": "message", "user": "U2", "ts": "1762776020.000000", "text": "lunch?"}],
           "has_more": False},
}


def _fake_slack(calls: list, *, history_fail: set | None = None):
    def req(self, url, *, method="GET", body=None, headers=None):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(url); qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        m = u.path.rsplit("/", 1)[1]
        calls.append((m, qs))
        if m == "auth.test":
            return {"ok": True, "user": "sam", "user_id": "U1", "team": "Acme",
                    "url": "https://acme.slack.com/"}, {}
        if m == "conversations.list":
            return SL_CONVS, {}
        if m == "users.list":
            return {"ok": True, "members": [
                {"id": "U1", "real_name": "Sam Ortiz"},
                {"id": "U2", "profile": {"real_name": "Priya Natarajan"}},
            ], "response_metadata": {"next_cursor": ""}}, {}
        if m == "conversations.history":
            ch = qs["channel"]
            if history_fail and ch in history_fail:
                return {"ok": False, "error": "channel_not_found"}, {}
            return SL_HISTORY[ch], {}
        raise AssertionError(url)
    return req


class SlackMappingTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        sl.slack._save_tokens({"access_token": "xoxp-u", "user_id": "U1"})

    def test_messages_shape_stamps_and_skips(self) -> None:
        calls: list = []
        with patch.object(sl.SlackConnector, "_request", _fake_slack(calls)):
            items, cursor = sl.slack.fetch_items(cursor={"since": NOW - 86400}, now=NOW)
        self.assertEqual([(i["kind"], i["external_id"]) for i in items],
                         [("message", "C1:1762776000.000100"), ("message", "C1:1762776003.000000"),
                          ("message", "G1:1762776010.000000"), ("dm", "D1:1762776020.000000")])
        m = items[0]
        self.assertEqual(m["text"], "#eng · Priya Natarajan: @Sam Ortiz can you look at PR 7 & "
                                    "reply in #eng?")
        self.assertEqual(m["people"], ["Priya Natarajan"])
        self.assertEqual(m["url"], "https://acme.slack.com/archives/C1/p1762776000000100")
        self.assertEqual(m["direction"], "inbound"); self.assertFalse(m["from_self"])
        self.assertNotIn("privacy_class", m["meta"])
        self.assertEqual(items[1]["text"], "#eng · Sam Ortiz: (shared spec.pdf)")
        self.assertTrue(items[1]["from_self"])
        g = items[2]
        self.assertEqual(g["meta"]["privacy_class"], "personal")
        self.assertEqual(g["thread_id"], "1762775000.000000")
        d = items[3]
        self.assertEqual(d["meta"]["privacy_class"], "sensitive")
        self.assertEqual(d["title"], "DM with Priya Natarajan: lunch?")
        self.assertEqual(d["people"], ["Priya Natarajan"])
        # Only member conversations were read, past the cursor, and identity
        # is cached in the cursor for next time.
        hist = [qs for m, qs in calls if m == "conversations.history"]
        self.assertEqual([h["channel"] for h in hist], ["C1", "G1", "D1"])
        self.assertEqual(hist[0]["oldest"], f"{NOW - 86400:.6f}")
        self.assertEqual((cursor["user_id"], cursor["team_url"]),
                         ("U1", "https://acme.slack.com/"))
        self.assertIn("1 dm, 3 messages", sl.slack.status()["status_detail"])

    def test_one_bad_conversation_does_not_sink_the_pass(self) -> None:
        with patch.object(sl.SlackConnector, "_request",
                          _fake_slack([], history_fail={"G1"})):
            items, _ = sl.slack.fetch_items(cursor={"since": NOW - 86400, "user_id": "U1",
                                                    "team_url": "https://acme.slack.com"}, now=NOW)
        self.assertEqual([i["external_id"] for i in items],
                         ["C1:1762776000.000100", "C1:1762776003.000000", "D1:1762776020.000000"])

    def test_render_text(self) -> None:
        users = {"U1": "Sam"}
        self.assertEqual(sl.SlackConnector.render_text(
            "hi <@U1|sam> see <#C9|ops> and <https://x.io> &lt;b&gt;", users),
            "hi @Sam see #ops and https://x.io <b>")


class LandingTests(_Sandbox):
    def test_slack_dm_lands_sensitive_and_notion_page_lands_plain(self) -> None:
        from app.services.connectors import scheduler
        from app.storage import Store
        store = Store(self.data / "t.db")
        sl.slack._save_tokens({"access_token": "xoxp-u"})
        nt.notion._save_tokens({"access_token": "ntn_x"})
        with patch.object(sl.SlackConnector, "_request", _fake_slack([])), \
             patch.object(nt.NotionConnector, "_request", _fake_notion([])):
            r1 = scheduler.sync_connector(sl.slack, store=store, now=NOW, force=True)
            r2 = scheduler.sync_connector(nt.notion, store=store, now=NOW, force=True)
            r3 = scheduler.sync_connector(sl.slack, store=store, now=NOW + 600, force=True)
        self.assertEqual((r1["landed"], r2["landed"], r3["landed"]), (4, 3, 0))
        dm = next(r for r in store.recent_events(source_substr="slack.dm", limit=5))
        self.assertEqual(dm["meta"]["privacy_class"], "sensitive")
        self.assertEqual(dm["meta"]["connector_id"], "slack")
        self.assertTrue(dm["meta"]["never_authorizes"])
        page = next(r for r in store.recent_events(source_substr="notion.page", limit=5)
                    if r["meta"]["external_id"] == "page:p-1")
        self.assertEqual(page["meta"]["url"], "https://www.notion.so/Roadmap-p1")
        self.assertIn("Ship search.", page["raw"])


if __name__ == "__main__":
    unittest.main()
