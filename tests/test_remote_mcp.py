"""Remote MCP for ChatGPT / Claude / Grok: OAuth, scopes, inbox-first capture."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from app.services import account, api_auth, assistant_inbox, remote_mcp

TOKEN = "test-api-token-123456"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class RemoteMcpBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_rmcp_"))
        self._patches = [
            mock.patch.dict(os.environ, {
                "QUILL_DATA_DIR": str(self.tmp), "QUILL_MCP_REMOTE": "1",
                "QUILL_API_TOKEN": TOKEN}),
            mock.patch.object(account, "account_path",
                              lambda: self.tmp / ".account.json"),
            mock.patch.object(account, "sessions_path",
                              lambda: self.tmp / ".web_sessions.json"),
        ]
        for p in self._patches:
            p.start()
        os.environ.pop("QUILL_MCP_PUBLIC_URL", None)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    # -- helpers -------------------------------------------------------------
    def register(self, uri=CLAUDE_CB, **extra):
        r = self.client.post("/oauth/mcp/register", json={
            "client_name": "Claude", "redirect_uris": [uri], **extra})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def authorize(self, client_id, challenge, *, scopes=("capture",),
                  credential=TOKEN, uri=CLAUDE_CB, origin=True):
        form = {"client_id": client_id, "redirect_uri": uri, "state": "st8",
                "response_type": "code", "code_challenge": challenge,
                "code_challenge_method": "S256", "decision": "allow",
                "credential": credential}
        for s in scopes:
            form[f"scope_{s}"] = "1"
        headers = {"Origin": "http://testserver"} if origin else {}
        return self.client.post("/oauth/mcp/authorize", data=form,
                                headers=headers, follow_redirects=False)

    def connect(self, scopes=("capture",)):
        reg = self.register()
        verifier, challenge = _pkce()
        r = self.authorize(reg["client_id"], challenge, scopes=scopes)
        self.assertEqual(r.status_code, 302, r.text)
        q = parse_qs(urlparse(r.headers["location"]).query)
        tok = self.client.post("/oauth/mcp/token", data={
            "grant_type": "authorization_code", "code": q["code"][0],
            "redirect_uri": CLAUDE_CB, "client_id": reg["client_id"],
            "code_verifier": verifier})
        self.assertEqual(tok.status_code, 200, tok.text)
        return reg, tok.json()

    def ui_post(self, path, json=None):
        """/assistants/* is the browser page's API: same-origin POSTs."""
        return self.client.post(path, json=json,
                                headers={"Origin": "http://testserver"})

    def rpc(self, access, method, params=None, msg_id=1):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if msg_id is not None:
            body["id"] = msg_id
        return self.client.post("/mcp", json=body,
                                headers={"Authorization": f"Bearer {access}"})


class GatingTests(RemoteMcpBase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {"QUILL_MCP_REMOTE": "0"}):
            self.assertEqual(self.client.post("/mcp", json={}).status_code, 404)
            self.assertEqual(self.client.get(
                "/.well-known/oauth-authorization-server").status_code, 404)

    def test_refuses_without_an_api_token(self):
        with mock.patch.dict(os.environ, {"QUILL_API_TOKEN": ""}), \
                mock.patch.object(api_auth, "token_path",
                                  lambda: self.tmp / ".api_token"):
            self.assertIn("QUILL_API_TOKEN", remote_mcp.unavailable_reason())
            self.assertEqual(self.client.post(
                "/oauth/mcp/register", json={}).status_code, 404)

    def test_stdio_routes_not_opened_by_exemptions(self):
        self.assertTrue(api_auth.path_is_exempt("/mcp", "POST"))
        self.assertFalse(api_auth.path_is_exempt("/mcp/tool", "POST"))
        self.assertFalse(api_auth.path_is_exempt("/mcp/tools", "GET"))
        self.assertFalse(api_auth.path_is_exempt("/assistants/inbox", "GET"))
        self.assertFalse(api_auth.csrf_path_exempt("/oauth/mcp/authorize"))


class DiscoveryTests(RemoteMcpBase):
    def test_metadata_points_at_this_server(self):
        with mock.patch.dict(os.environ,
                             {"QUILL_MCP_PUBLIC_URL": "https://s.example.com/"}):
            pr = self.client.get("/.well-known/oauth-protected-resource").json()
            asm = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(pr["resource"], "https://s.example.com/mcp")
        self.assertEqual(pr["authorization_servers"], ["https://s.example.com"])
        self.assertEqual(asm["issuer"], "https://s.example.com")
        self.assertEqual(asm["code_challenge_methods_supported"], ["S256"])
        self.assertTrue(asm["registration_endpoint"].endswith("/oauth/mcp/register"))

    def test_unauthenticated_mcp_advertises_resource_metadata(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                           "method": "initialize"})
        self.assertEqual(r.status_code, 401)
        self.assertIn('resource_metadata="', r.headers["www-authenticate"])
        self.assertIn("/.well-known/oauth-protected-resource",
                      r.headers["www-authenticate"])


class OAuthFlowTests(RemoteMcpBase):
    def test_registration_infers_the_assistant(self):
        for uri, want in ((CLAUDE_CB, "claude"),
                          ("https://chatgpt.com/connector_platform_oauth_redirect", "chatgpt"),
                          ("https://grok.com/connectors/oauth/callback", "grok"),
                          ("http://localhost:33418/callback", "local"),
                          ("https://example.org/cb", "other")):
            reg = self.register(uri)
            self.assertEqual(remote_mcp.get_client(reg["client_id"])["assistant"],
                             want, uri)

    def test_registration_rejects_plain_http_off_loopback(self):
        r = self.client.post("/oauth/mcp/register", json={
            "redirect_uris": ["http://evil.example/cb"]})
        self.assertEqual(r.status_code, 400)

    def test_consent_page_names_the_client(self):
        reg = self.register()
        _, challenge = _pkce()
        r = self.client.get("/oauth/mcp/authorize", params={
            "client_id": reg["client_id"], "redirect_uri": CLAUDE_CB,
            "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Connect Claude to Sparrow?", r.text)
        self.assertEqual(r.headers["x-frame-options"], "DENY")

    def test_unregistered_redirect_is_an_error_page_not_a_redirect(self):
        reg = self.register()
        _, challenge = _pkce()
        r = self.authorize(reg["client_id"], challenge,
                           uri="https://attacker.example/cb")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("location", r.headers)

    def test_consent_needs_same_origin(self):
        reg = self.register()
        _, challenge = _pkce()
        r = self.authorize(reg["client_id"], challenge, origin=False)
        self.assertEqual(r.status_code, 403)

    def test_consent_needs_a_valid_credential(self):
        reg = self.register()
        _, challenge = _pkce()
        r = self.authorize(reg["client_id"], challenge, credential="nope")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sign-in failed", r.text)

    def test_deny_returns_access_denied(self):
        reg = self.register()
        _, challenge = _pkce()
        r = self.client.post("/oauth/mcp/authorize", data={
            "client_id": reg["client_id"], "redirect_uri": CLAUDE_CB,
            "state": "s", "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "decision": "deny"},
            headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("error=access_denied", r.headers["location"])

    def test_pkce_mismatch_and_code_replay(self):
        reg = self.register()
        verifier, challenge = _pkce()
        loc = self.authorize(reg["client_id"], challenge).headers["location"]
        code = parse_qs(urlparse(loc).query)["code"][0]
        self.assertEqual(parse_qs(urlparse(loc).query)["state"], ["st8"])
        form = {"grant_type": "authorization_code", "code": code,
                "redirect_uri": CLAUDE_CB, "client_id": reg["client_id"]}
        bad = self.client.post("/oauth/mcp/token",
                               data={**form, "code_verifier": "wrong"})
        self.assertEqual(bad.json()["error"], "invalid_grant")
        good = self.client.post("/oauth/mcp/token",
                                data={**form, "code_verifier": verifier}).json()
        self.assertEqual(self.rpc(good["access_token"], "ping").status_code, 200)
        replay = self.client.post("/oauth/mcp/token",
                                  data={**form, "code_verifier": verifier})
        self.assertEqual(replay.json()["error"], "invalid_grant")
        # A replayed code revokes what it minted.
        self.assertEqual(self.rpc(good["access_token"], "ping").status_code, 401)

    def test_refresh_rotates(self):
        reg, tok = self.connect()
        form = {"grant_type": "refresh_token", "client_id": reg["client_id"],
                "refresh_token": tok["refresh_token"]}
        new = self.client.post("/oauth/mcp/token", data=form)
        self.assertEqual(new.status_code, 200)
        self.assertNotEqual(new.json()["access_token"], tok["access_token"])
        again = self.client.post("/oauth/mcp/token", data=form)
        self.assertEqual(again.json()["error"], "invalid_grant")

    def test_confidential_client_needs_its_secret(self):
        reg = self.register(token_endpoint_auth_method="client_secret_basic")
        verifier, challenge = _pkce()
        loc = self.authorize(reg["client_id"], challenge).headers["location"]
        form = {"grant_type": "authorization_code",
                "code": parse_qs(urlparse(loc).query)["code"][0],
                "redirect_uri": CLAUDE_CB, "code_verifier": verifier}
        r = self.client.post("/oauth/mcp/token",
                             data={**form, "client_id": reg["client_id"]})
        self.assertEqual(r.status_code, 401)
        basic = base64.b64encode(
            f"{reg['client_id']}:{reg['client_secret']}".encode()).decode()
        r = self.client.post("/oauth/mcp/token", data=form,
                             headers={"Authorization": f"Basic {basic}"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_tokens_are_hashed_at_rest(self):
        _, tok = self.connect()
        raw = remote_mcp.db_path().read_bytes()
        self.assertNotIn(tok["access_token"].encode(), raw)
        self.assertNotIn(tok["refresh_token"].encode(), raw)


class ProtocolTests(RemoteMcpBase):
    def test_initialize_negotiates_version(self):
        _, tok = self.connect()
        r = self.rpc(tok["access_token"], "initialize",
                     {"protocolVersion": "2025-03-26", "capabilities": {},
                      "clientInfo": {"name": "t", "version": "1"}}).json()
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        r = self.rpc(tok["access_token"], "initialize",
                     {"protocolVersion": "1999-01-01"}).json()
        self.assertEqual(r["result"]["protocolVersion"],
                         remote_mcp.PROTOCOL_VERSIONS[0])

    def test_notification_is_202(self):
        _, tok = self.connect()
        r = self.rpc(tok["access_token"], "notifications/initialized",
                     msg_id=None)
        self.assertEqual(r.status_code, 202)

    def test_tools_follow_scope(self):
        _, cap = self.connect(("capture",))
        names = {t["name"] for t in self.rpc(
            cap["access_token"], "tools/list").json()["result"]["tools"]}
        self.assertEqual(names, {"list_projects", "save_to_sparrow"})
        out = self.rpc(cap["access_token"], "tools/call",
                       {"name": "memory_search",
                        "arguments": {"query": "x"}}).json()["result"]
        self.assertTrue(out["isError"])

        _, both = self.connect(("capture", "read"))
        tools = self.rpc(both["access_token"], "tools/list").json()["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertIn("memory_search", names)
        self.assertIn("save_to_sparrow", names)
        by = {t["name"]: t for t in tools}
        self.assertTrue(by["memory_search"]["annotations"]["readOnlyHint"])
        self.assertFalse(by["save_to_sparrow"]["annotations"]["readOnlyHint"])

    def test_get_is_405(self):
        self.assertEqual(self.client.get("/mcp").status_code, 405)


class InboxTests(RemoteMcpBase):
    def save(self, access, **args):
        out = self.rpc(access, "tools/call", {
            "name": "save_to_sparrow", "arguments": args}).json()["result"]
        import json
        return json.loads(out["content"][0]["text"])

    def test_capture_waits_for_review(self):
        _, tok = self.connect()
        r = self.save(tok["access_token"], text="We chose Postgres for "
                      "the records layer because of row-level security.",
                      project="Records", kind="decision")
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "pending")
        items = assistant_inbox.items("pending")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["assistant"], "claude")
        self.assertEqual(items[0]["kind"], "decision")
        dup = self.save(tok["access_token"], text=items[0]["text"],
                        project="Records")
        self.assertTrue(dup["duplicate"])
        self.assertEqual(len(assistant_inbox.items("pending")), 1)

    def test_oversize_and_empty_are_refused(self):
        _, tok = self.connect()
        self.assertFalse(self.save(tok["access_token"], text="")["ok"])
        self.assertFalse(self.save(tok["access_token"],
                                   text="x" * (assistant_inbox.MAX_TEXT + 1))["ok"])

    def test_accept_stores_an_observed_never_authorizing_event(self):
        _, tok = self.connect()
        self.save(tok["access_token"], project="Onboarding",
                  text="Ship the onboarding redesign by Nov 14; key "
                       "sk-ant-api03-abcdefghijklmnop0123456789 goes in vault.")
        item = assistant_inbox.items("pending")[0]
        self.assertIn("anthropic_key", item["flags"])
        stored = []

        class FakeStore:
            def insert(self, ev):
                stored.append(ev)
                return 4242

        from app.services.worker import worker
        with mock.patch("app.storage.get_store", return_value=FakeStore()), \
                mock.patch("app.services.attachments._index_event"), \
                mock.patch.object(worker, "enqueue") as enq:
            r = self.ui_post(f"/assistants/inbox/{item['id']}/accept",
                             {"project": "Onboarding v2"})
        self.assertEqual(r.json(), {"ok": True, "event_id": 4242})
        ev = stored[0]
        self.assertEqual(ev.source, "assistant.claude")
        self.assertTrue(ev.meta["never_authorizes"])
        self.assertEqual(ev.meta["epistemic"], "observed")
        self.assertEqual(ev.meta["project"], "Onboarding v2")
        self.assertIn("Project: Onboarding v2", ev.raw)
        self.assertNotIn("sk-ant-api03", ev.raw)
        enq.assert_called_once()
        self.assertEqual(enq.call_args[0][0], "assistant_capture")
        self.assertEqual(assistant_inbox.items("accepted")[0]["event_id"], 4242)
        again = self.ui_post(f"/assistants/inbox/{item['id']}/accept")
        self.assertFalse(again.json()["ok"])

    def test_remove_erases_an_accepted_note(self):
        _, tok = self.connect()
        self.save(tok["access_token"], text="Kickoff moved to Dec 3.")
        item = assistant_inbox.items("pending")[0]
        erased = []

        class FakeStore:
            def insert(self, ev):
                return 77

            def erase_event(self, eid):
                erased.append(eid)
                return {"event_id": eid, "ok": True}

        from app.services.worker import worker
        with mock.patch("app.storage.get_store", return_value=FakeStore()), \
                mock.patch("app.services.attachments._index_event"), \
                mock.patch.object(worker, "enqueue"):
            self.ui_post(f"/assistants/inbox/{item['id']}/accept")
            r = self.ui_post(f"/assistants/inbox/{item['id']}/remove")
            again = self.ui_post(f"/assistants/inbox/{item['id']}/remove")
        self.assertTrue(r.json()["ok"])
        self.assertEqual(erased, [77])
        self.assertFalse(again.json()["ok"])
        gone = assistant_inbox.items("removed")[0]
        self.assertEqual(gone["text"], "")
        self.assertEqual(assistant_inbox.items("accepted"), [])

    def test_assistant_first_person_is_not_the_user(self):
        from app.services import chat_ingest
        facts = {"tasks": [{"text": "Draft the spec", "owner": "me"},
                           {"text": "Send deck", "owner": "Dana"}],
                 "relations": [{"subject": "me", "object": "Dana"}]}
        out = chat_ingest._drop_speaker_self(facts)
        self.assertEqual(out["tasks"][0]["owner"], "")
        self.assertEqual(out["tasks"][1]["owner"], "Dana")
        self.assertEqual(out["relations"], [])

    def test_cross_origin_ui_posts_are_refused(self):
        r = self.client.post("/assistants/tokens", json={"label": "x"},
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_reject_wipes_the_text(self):
        _, tok = self.connect()
        self.save(tok["access_token"], text="Draft budget is 40k for Q1 pilots.")
        item = assistant_inbox.items("pending")[0]
        self.assertTrue(self.ui_post(
            f"/assistants/inbox/{item['id']}/reject").json()["ok"])
        gone = assistant_inbox.items("rejected")[0]
        self.assertEqual(gone["text"], "")

    def test_revoke_and_static_tokens(self):
        minted = self.ui_post("/assistants/tokens", {
            "label": "Grok API", "scope": "capture"}).json()
        self.assertTrue(minted["token"].startswith("spk_"))
        self.assertEqual(self.rpc(minted["token"], "ping").status_code, 200)
        conns = self.client.get("/assistants/status").json()["connections"]
        row = next(c for c in conns if c["id"] == minted["grant_id"])
        self.assertEqual(row["assistant"], "grok")
        self.ui_post(f"/assistants/connections/{minted['grant_id']}/revoke")
        self.assertEqual(self.rpc(minted["token"], "ping").status_code, 401)


class SourcePolicyTests(unittest.TestCase):
    def test_assistant_captures_have_their_own_class(self):
        from app.services import source_policy
        for src in ("assistant.chatgpt", "assistant.claude", "assistant.grok"):
            self.assertEqual(source_policy.classify_source(event_source=src),
                             "assistant_capture")
        pol = source_policy.policy_for("assistant_capture")
        self.assertTrue(pol.create_claims)
        self.assertFalse(pol.extract_contacts)
        self.assertFalse(pol.identity_evidence)


if __name__ == "__main__":
    unittest.main()
