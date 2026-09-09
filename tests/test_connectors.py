"""Connectors registry + Google web OAuth redirect helpers."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import connectors
from app.services import exhaust_ingest as ex
from app.services.connectors import google as google_mod
from app.services.connectors.planned import PlannedConnector


class RegistryTests(unittest.TestCase):
    def test_google_ready_and_for_tool(self) -> None:
        ids = [c.id for c in connectors.all()]
        self.assertIn("google", ids)
        self.assertEqual(connectors.get("google").id, "google")
        self.assertEqual(connectors.for_tool("Gmail").id, "google")
        self.assertEqual(connectors.for_tool("Google Calendar").id, "google")
        self.assertIsNone(connectors.for_tool("NoSuchTool"))

    def test_planned_stubs_refuse_connect(self) -> None:
        slack = connectors.get("slack")
        self.assertIsInstance(slack, PlannedConnector)
        self.assertEqual(slack.availability, "planned")
        r = slack.begin_connect()
        self.assertFalse(r.get("ok"))
        self.assertTrue(r.get("planned"))

    def test_tool_status_map_includes_gmail(self) -> None:
        m = connectors.tool_status_map()
        self.assertIn("Gmail", m)
        self.assertEqual(m["Gmail"]["connector_id"], "google")
        self.assertIn("Slack", m)
        self.assertEqual(m["Slack"]["availability"], "planned")

    def test_directory_has_categories_and_enrichment(self) -> None:
        d = connectors.directory()
        self.assertTrue(d["categories"])
        google = next(c for c in d["connectors"] if c["id"] == "google")
        self.assertEqual(google.get("category"), "calendar")
        self.assertTrue(google.get("description"))
        self.assertIn("tool_access", d)
        self.assertIn("session", d)


class SessionPrefsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        from app.services.connectors import prefs, session
        self._path = patch.object(
            prefs, "prefs_path",
            lambda: root / "connectors" / "prefs.json")
        self._path.start()
        self.addCleanup(self._path.stop)
        prefs.set_tool_access("auto")
        session.reset()
        self.addCleanup(session.reset)

    def test_auto_vs_on_demand(self) -> None:
        from app.services.connectors import prefs, session
        connected = {"google"}
        self.assertEqual(session.active_ids(connected=connected), {"google"})
        session.set_enabled("google", False)
        self.assertEqual(session.active_ids(connected=connected), set())
        session.reset()
        prefs.set_tool_access("on_demand")
        self.assertEqual(session.active_ids(connected=connected), set())
        session.set_enabled("google", True)
        self.assertEqual(session.active_ids(connected=connected), {"google"})

    def test_custom_mcp_register(self) -> None:
        from app.services.connectors import custom as custom_mod
        r = custom_mod.add(label="Dev MCP", url="http://127.0.0.1:39999")
        self.assertTrue(r.get("ok"))
        cid = r["connector"]["id"]
        self.assertTrue(cid.startswith("mcp-"))
        self.assertIsNotNone(connectors.get(cid))
        self.assertEqual(connectors.get(cid).kind, "custom")
        gone = custom_mod.remove(cid)
        self.assertTrue(gone.get("ok"))
        self.assertIsNone(connectors.get(cid))

    def test_team_policy_blocks(self) -> None:
        from app.services.connectors import prefs
        prefs.set_team_policy(enabled=True, allowed=["slack"])
        self.assertFalse(prefs.is_team_allowed("google"))
        self.assertTrue(prefs.is_team_allowed("slack"))
        prefs.set_team_policy(enabled=False, allowed=[])
        self.assertTrue(prefs.is_team_allowed("google"))

    def test_blocked_source_prefixes(self) -> None:
        from app.services.connectors import session
        session.reset()
        # No connected connectors → google not active → exhaust blocked.
        blocked = session.blocked_source_prefixes()
        self.assertIn("exhaust.gmail", blocked)
        self.assertIn("exhaust.calendar", blocked)


class PublicBaseTests(unittest.TestCase):
    def test_https_peer_base(self) -> None:
        with patch.dict("os.environ", {
            "QUILL_PUBLIC_BASE_URL": "",
            "QUILL_PEER_BASE_URL": "https://user1.example.com/",
        }, clear=False):
            self.assertEqual(
                connectors.public_base_url(), "https://user1.example.com")

    def test_public_overrides_peer(self) -> None:
        with patch.dict("os.environ", {
            "QUILL_PUBLIC_BASE_URL": "https://stable.example.com",
            "QUILL_PEER_BASE_URL": "https://other.example.com",
        }, clear=False):
            self.assertEqual(
                connectors.public_base_url(), "https://stable.example.com")

    def test_http_peer_ignored(self) -> None:
        with patch.dict("os.environ", {
            "QUILL_PUBLIC_BASE_URL": "",
            "QUILL_PEER_BASE_URL": "http://127.0.0.1:8001",
        }, clear=False):
            self.assertIsNone(connectors.public_base_url())

    def test_request_prefers_origin_over_env(self) -> None:
        from types import SimpleNamespace
        with patch.dict("os.environ", {
            "QUILL_PUBLIC_BASE_URL": "",
            "QUILL_PEER_BASE_URL": "https://stale.trycloudflare.com",
        }, clear=False):
            req = SimpleNamespace(headers={
                "origin": "https://live-site.trycloudflare.com",
            }, base_url="http://sparrow-user1:8000/")
            self.assertEqual(
                connectors.request_public_base(req),
                "https://live-site.trycloudflare.com")

    def test_request_forwarded_host(self) -> None:
        from types import SimpleNamespace
        with patch.dict("os.environ", {
            "QUILL_PUBLIC_BASE_URL": "",
            "QUILL_PEER_BASE_URL": "",
        }, clear=False):
            req = SimpleNamespace(headers={
                "x-forwarded-proto": "https",
                "x-forwarded-host": "pilot.example.com",
            }, base_url="http://127.0.0.1:8000/")
            self.assertEqual(
                connectors.request_public_base(req),
                "https://pilot.example.com")


    def test_oauth_redirect_base_env(self) -> None:
        with patch.dict("os.environ", {
            "QUILL_OAUTH_REDIRECT_BASE": "https://gb10.tail1234.ts.net/",
        }, clear=False):
            self.assertEqual(connectors.oauth_redirect_base(),
                             "https://gb10.tail1234.ts.net")

    def test_oauth_redirect_base_rejects_http(self) -> None:
        with patch.dict("os.environ", {
            "QUILL_OAUTH_REDIRECT_BASE": "http://gb10.local:8001",
        }, clear=False):
            self.assertIsNone(connectors.oauth_redirect_base())


class OAuthRedirectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.data = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._env = patch.dict("os.environ", {
            "QUILL_DATA_DIR": str(self.data),
            "GOOGLE_OAUTH_CLIENT_ID": "cid.apps.googleusercontent.com",
            "GOOGLE_OAUTH_CLIENT_SECRET": "csecret",
            "QUILL_EXHAUST_INGEST": "1",
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        # settings.exhaust is frozen at import — patch module helpers instead.
        self._tok = patch.object(
            ex, "_token_path",
            lambda: self.data / "connectors" / "google" / "token.json")
        self._leg = patch.object(
            ex, "_legacy_token_path",
            lambda: self.data / "google_oauth_token.json")
        self._st = patch.object(
            ex, "_oauth_state_path",
            lambda: self.data / "connectors" / "google" / "oauth_state.json")
        self._cfg = patch.object(ex, "oauth_configured", lambda: True)
        self._tok.start(); self.addCleanup(self._tok.stop)
        self._leg.start(); self.addCleanup(self._leg.stop)
        self._st.start(); self.addCleanup(self._st.stop)
        self._cfg.start(); self.addCleanup(self._cfg.stop)

    def test_begin_redirect_writes_state(self) -> None:
        r = ex.start_oauth_redirect("https://user1.example.com")
        self.assertTrue(r.get("ok"))
        self.assertEqual(r.get("mode"), "redirect")
        self.assertIn("accounts.google.com", r.get("auth_url", ""))
        self.assertEqual(
            r.get("redirect_uri"),
            "https://user1.example.com/oauth/google/callback")
        states = json.loads(
            (self.data / "connectors/google/oauth_state.json").read_text())
        self.assertIn(r["state"], states)

    def test_begin_rejects_http_base(self) -> None:
        r = ex.start_oauth_redirect("http://127.0.0.1:8001")
        self.assertFalse(r.get("ok"))

    def test_complete_rejects_bad_state(self) -> None:
        r = ex.complete_oauth_redirect(
            "code", "nope",
            redirect_uri="https://user1.example.com/oauth/google/callback")
        self.assertFalse(r.get("ok"))
        self.assertIn("state", (r.get("error") or "").lower())

    def test_complete_exchanges_and_saves(self) -> None:
        start = ex.start_oauth_redirect("https://user1.example.com")
        state = start["state"]
        redirect = start["redirect_uri"]

        def fake_exchange(code, redirect_uri):
            self.assertEqual(code, "authcode")
            self.assertEqual(redirect_uri, redirect)
            return {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "scope": " ".join(ex.SCOPES),
            }

        with patch.object(ex, "_exchange_code", fake_exchange):
            r = ex.complete_oauth_redirect(
                "authcode", state, redirect_uri=redirect)
        self.assertTrue(r.get("ok"))
        self.assertTrue(ex.connected())
        tok = ex.load_tokens()
        self.assertEqual(tok.get("access_token"), "at")

    def test_legacy_token_fallback(self) -> None:
        legacy = self.data / "google_oauth_token.json"
        legacy.write_text(json.dumps({
            "access_token": "legacy", "refresh_token": "lr",
        }), encoding="utf-8")
        self.assertTrue(ex.connected())
        self.assertEqual(ex.load_tokens().get("access_token"), "legacy")

    def test_clear_tokens(self) -> None:
        path = self.data / "connectors" / "google" / "token.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"access_token": "x"}), encoding="utf-8")
        self.assertTrue(ex.connected())
        r = ex.clear_tokens()
        self.assertTrue(r.get("ok"))
        self.assertFalse(ex.connected())

    def test_anchor_redirect_uri_with_live_return_origin(self) -> None:
        """One registered URI; state carries the rotating hostname back."""
        r = ex.start_oauth_redirect(
            "https://live-tunnel.trycloudflare.com",
            redirect_base="https://gb10.tail1234.ts.net")
        self.assertTrue(r.get("ok"))
        self.assertEqual(r.get("redirect_uri"),
                         "https://gb10.tail1234.ts.net/oauth/google/callback")
        self.assertEqual(r.get("return_origin"),
                         "https://live-tunnel.trycloudflare.com")
        self.assertEqual(ex.state_return_origin(r["state"]),
                         "https://live-tunnel.trycloudflare.com")
        self.assertTrue(ex.has_oauth_state(r["state"]))

    def test_anchor_falls_back_to_live_origin(self) -> None:
        r = ex.start_oauth_redirect("https://user1.example.com",
                                    redirect_base="http://not-https")
        self.assertEqual(r.get("redirect_uri"),
                         "https://user1.example.com/oauth/google/callback")

    def test_state_origin_unknown_for_foreign_state(self) -> None:
        self.assertFalse(ex.has_oauth_state("someone-elses.state"))
        self.assertIsNone(ex.state_return_origin("no-tail"))
        self.assertIsNone(ex.state_return_origin("x.bm90LWFuLW9yaWdpbg"))

    def test_relay_state_survives_a_foreign_instance(self) -> None:
        """The relay only needs the state string, never the state file."""
        r = ex.start_oauth_redirect(
            "https://live-tunnel.trycloudflare.com",
            redirect_base="https://gb10.tail1234.ts.net")
        state = r["state"]
        # Relay container: different data dir, so no local state entry.
        with patch.object(ex, "_oauth_state_path",
                          lambda: self.data / "relay-oauth_state.json"):
            self.assertFalse(ex.has_oauth_state(state))
            self.assertEqual(ex.state_return_origin(state),
                             "https://live-tunnel.trycloudflare.com")
        # Back on the originating container the exchange still works, using
        # the anchor redirect_uri saved at connect time.
        def fake_exchange(code, redirect_uri):
            self.assertEqual(
                redirect_uri,
                "https://gb10.tail1234.ts.net/oauth/google/callback")
            return {"access_token": "at", "refresh_token": "rt",
                    "expires_in": 3600, "scope": " ".join(ex.SCOPES)}

        with patch.object(ex, "_exchange_code", fake_exchange):
            done = ex.complete_oauth_redirect("authcode", state,
                                              redirect_uri="")
        self.assertTrue(done.get("ok"))
        self.assertTrue(ex.connected())

    def test_google_connector_redirect_mode(self) -> None:
        with patch.object(google_mod, "public_base_url",
                          lambda: "https://user1.example.com"):
            with patch.object(ex, "start_oauth_redirect",
                              return_value={"ok": True, "mode": "redirect",
                                            "auth_url": "https://x"}) as m:
                r = google_mod.google.begin_connect()
        self.assertEqual(r.get("mode"), "redirect")
        m.assert_called_once()


class RelayCallbackTests(unittest.TestCase):
    """The single registered callback bounces foreign states to their host."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.data = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        # Anchor container holds no state of its own.
        self._st = patch.object(
            ex, "_oauth_state_path", lambda: self.data / "oauth_state.json")
        self._st.start(); self.addCleanup(self._st.stop)

    @staticmethod
    def _req(origin: str):
        from types import SimpleNamespace
        return SimpleNamespace(headers={"origin": origin},
                               base_url="http://sparrow-user1:8000/")

    @staticmethod
    def _state_for(origin: str) -> str:
        return ex._encode_state(origin)

    def test_foreign_state_relays_to_return_origin(self) -> None:
        from app.api.adoption import google_oauth_callback
        state = self._state_for("https://live-tunnel.trycloudflare.com")
        resp = google_oauth_callback(
            self._req("https://gb10.tail1234.ts.net"),
            code="authcode", state=state)
        self.assertEqual(resp.status_code, 302)
        loc = resp.headers["location"]
        self.assertTrue(loc.startswith(
            "https://live-tunnel.trycloudflare.com/oauth/google/callback?"))
        self.assertIn("code=authcode", loc)
        self.assertIn("state=", loc)

    def test_relay_forwards_google_error(self) -> None:
        from app.api.adoption import google_oauth_callback
        state = self._state_for("https://live-tunnel.trycloudflare.com")
        resp = google_oauth_callback(
            self._req("https://gb10.tail1234.ts.net"),
            state=state, error="access_denied")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("live-tunnel.trycloudflare.com",
                      resp.headers["location"])
        self.assertIn("error=access_denied", resp.headers["location"])

    def test_own_expired_state_does_not_loop(self) -> None:
        """Same origin + no state = expired here, not a relay hop."""
        from app.api.adoption import google_oauth_callback
        origin = "https://live-tunnel.trycloudflare.com"
        resp = google_oauth_callback(self._req(origin), code="c",
                                     state=self._state_for(origin))
        self.assertEqual(resp.status_code, 302)
        loc = resp.headers["location"]
        self.assertTrue(loc.startswith("/onboarding?"))
        self.assertIn("oauth_error=", loc)


class ReturnPathTests(unittest.TestCase):
    """Connect from the Connections sheet lands back on that page."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.data = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._env = patch.dict("os.environ", {
            "QUILL_DATA_DIR": str(self.data),
            "QUILL_EXHAUST_INGEST": "1",
        }, clear=False)
        self._env.start(); self.addCleanup(self._env.stop)
        for attr, rel in (("_token_path", "token.json"),
                          ("_oauth_state_path", "oauth_state.json")):
            pt = patch.object(ex, attr, lambda rel=rel: self.data / rel)
            pt.start(); self.addCleanup(pt.stop)
        cfg = patch.object(ex, "oauth_configured", lambda: True)
        cfg.start(); self.addCleanup(cfg.stop)

    @staticmethod
    def _req(origin: str):
        from types import SimpleNamespace
        return SimpleNamespace(headers={"origin": origin},
                               base_url="http://sparrow-user1:8000/")

    def test_safe_return_path_rejects_offsite(self) -> None:
        from app.services.connectors import safe_return_path as f
        self.assertEqual(f("/console"), "/console")
        self.assertEqual(f("/console?tab=x"), "/console?tab=x")
        for bad in (None, "", "//evil.example", "https://evil.example",
                    "/a\nb", "\\evil", "/" + "x" * 600):
            self.assertEqual(f(bad), "/onboarding?step=2")

    def test_state_carries_the_page_and_completion_returns_it(self) -> None:
        r = ex.start_oauth_redirect("https://user1.example.com",
                                    return_path="/console")
        self.assertEqual(r.get("return_path"), "/console")
        self.assertEqual(ex.peek_oauth_state(r["state"]).get("return_path"),
                         "/console")

        def fake_exchange(code, redirect_uri):
            return {"access_token": "at", "refresh_token": "rt",
                    "expires_in": 3600, "scope": " ".join(ex.SCOPES)}

        with patch.object(ex, "_exchange_code", fake_exchange):
            done = ex.complete_oauth_redirect("authcode", r["state"],
                                              redirect_uri="")
        self.assertTrue(done.get("ok"))
        self.assertEqual(done.get("return_path"), "/console")

    def test_offsite_return_path_is_dropped_at_connect(self) -> None:
        r = ex.start_oauth_redirect("https://user1.example.com",
                                    return_path="https://evil.example/steal")
        self.assertEqual(r.get("return_path"), "/onboarding?step=2")

    def test_callback_lands_on_the_page_connect_was_pressed_on(self) -> None:
        from app.api.adoption import google_oauth_callback
        origin = "https://user1.example.com"
        r = ex.start_oauth_redirect(origin, return_path="/console")

        def fake_exchange(code, redirect_uri):
            return {"access_token": "at", "refresh_token": "rt",
                    "expires_in": 3600, "scope": " ".join(ex.SCOPES)}

        with patch.object(ex, "_exchange_code", fake_exchange):
            resp = google_oauth_callback(self._req(origin), code="authcode",
                                         state=r["state"])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/console?connected=google")

    def test_callback_error_returns_to_the_same_page(self) -> None:
        from app.api.adoption import google_oauth_callback
        origin = "https://user1.example.com"
        r = ex.start_oauth_redirect(origin, return_path="/console?tab=conn")
        resp = google_oauth_callback(self._req(origin), state=r["state"],
                                     error="access_denied")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"],
                         "/console?tab=conn&oauth_error=access_denied")

    def test_onboarding_default_is_unchanged(self) -> None:
        from app.api.adoption import google_oauth_callback
        origin = "https://user1.example.com"
        r = ex.start_oauth_redirect(origin)   # onboarding sends no path

        def fake_exchange(code, redirect_uri):
            return {"access_token": "at", "refresh_token": "rt",
                    "expires_in": 3600, "scope": " ".join(ex.SCOPES)}

        with patch.object(ex, "_exchange_code", fake_exchange):
            resp = google_oauth_callback(self._req(origin), code="c",
                                         state=r["state"])
        self.assertEqual(resp.headers["location"],
                         "/onboarding?step=2&connected=google")


if __name__ == "__main__":
    unittest.main()
