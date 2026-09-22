"""Outlook connector — registry, Microsoft OAuth redirect/state, scope
guard, Graph metadata mapping, background fetch_items, People-seeding
ingest under the outlook provider, and the generic OAuth callback relay."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import connectors
from app.services import exhaust_ingest as ex
from app.services.connectors import outlook as ol

NOW = 1_758_500_000.0  # 2025-09-22ish, fixed


class RegistryTests(unittest.TestCase):
    def test_outlook_is_ready_not_planned(self) -> None:
        c = connectors.get("outlook")
        self.assertIsInstance(c, ol.OutlookConnector)
        self.assertEqual(c.availability, "ready")
        self.assertEqual(connectors.for_tool("Outlook").id, "outlook")
        self.assertEqual(connectors.for_tool("Microsoft Calendar").id, "outlook")
        from app.services.connectors.planned import PLANNED
        self.assertNotIn("outlook", [p.id for p in PLANNED])

    def test_directory_card_and_status_map(self) -> None:
        d = connectors.directory()
        row = next(c for c in d["connectors"] if c["id"] == "outlook")
        self.assertEqual(row["category"], "calendar")
        self.assertEqual(row["availability"], "ready")
        self.assertNotIn("planned", row["description"].lower())
        self.assertIn("Mail.ReadBasic", row.get("scopes_blurb", ""))
        self.assertEqual(connectors.tool_status_map()["Outlook"]["availability"],
                         "ready")

    def test_session_source_prefixes_come_off_the_class(self) -> None:
        from app.services.connectors import session
        prefixes = session.source_prefixes()["outlook"]
        self.assertIn("exhaust.outlook", prefixes)
        self.assertIn("outlook.mail", prefixes)


class ScopeGuardTests(unittest.TestCase):
    def test_accepts_qualified_and_bare_with_platform_extras(self) -> None:
        ol.assert_metadata_scopes(
            "https://graph.microsoft.com/Mail.ReadBasic "
            "https://graph.microsoft.com/Calendars.Read "
            "offline_access openid profile email User.Read")
        ol.assert_metadata_scopes("Mail.ReadBasic Calendars.Read")

    def test_refuses_extra_or_missing(self) -> None:
        with self.assertRaises(PermissionError):
            ol.assert_metadata_scopes("Mail.ReadBasic Calendars.Read Mail.Send")
        with self.assertRaises(PermissionError):
            ol.assert_metadata_scopes("Mail.Read Calendars.Read")
        with self.assertRaises(PermissionError):
            ol.assert_metadata_scopes("Mail.ReadBasic")


class _Sandbox(unittest.TestCase):
    """Token/state files under a temp dir; a client id so oauth is configured."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data = Path(self._td.name)
        from app import config as _cfg
        cfg = _cfg.OutlookConfig.__new__(_cfg.OutlookConfig)
        object.__setattr__(cfg, "client_id", "11111111-2222-3333-4444-555555555555")
        object.__setattr__(cfg, "client_secret", "")
        object.__setattr__(cfg, "tenant", "common")
        self._cfg = patch.object(ol, "_cfg", lambda: cfg)
        self._cfg.start(); self.addCleanup(self._cfg.stop)
        self._tp = patch.object(ol, "_token_path",
                                lambda: self.data / "connectors/outlook/token.json")
        self._tp.start(); self.addCleanup(self._tp.stop)
        self._sp = patch.object(ol, "_oauth_state_path",
                                lambda: self.data / "connectors/outlook/oauth_state.json")
        self._sp.start(); self.addCleanup(self._sp.stop)


class OAuthRedirectTests(_Sandbox):
    def test_begin_redirect_writes_state_and_ms_auth_url(self) -> None:
        r = ol.start_oauth_redirect("https://user1.example.com",
                                    return_path="/console?tab=people")
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "redirect")
        self.assertIn("login.microsoftonline.com/common/oauth2/v2.0/authorize",
                      r["auth_url"])
        self.assertIn("Mail.ReadBasic", r["auth_url"])
        self.assertIn("Calendars.Read", r["auth_url"])
        self.assertIn("offline_access", r["auth_url"])
        self.assertEqual(r["redirect_uri"],
                         "https://user1.example.com/oauth/outlook/callback")
        self.assertEqual(r["return_path"], "/console?tab=people")
        states = json.loads(
            (self.data / "connectors/outlook/oauth_state.json").read_text())
        self.assertIn(r["state"], states)
        self.assertTrue(ol.has_oauth_state(r["state"]))
        self.assertEqual(ol.state_return_origin(r["state"]),
                         "https://user1.example.com")

    def test_relay_anchor_owns_the_redirect_uri(self) -> None:
        r = ol.start_oauth_redirect("https://rot-123.trycloudflare.com",
                                    redirect_base="https://gb10.ts.net")
        self.assertEqual(r["redirect_uri"], "https://gb10.ts.net/oauth/outlook/callback")
        self.assertEqual(r["return_origin"], "https://rot-123.trycloudflare.com")

    def test_http_base_and_unconfigured_refused(self) -> None:
        self.assertFalse(ol.start_oauth_redirect("http://127.0.0.1:8001")["ok"])
        with patch.object(ol, "oauth_configured", lambda: False):
            r = ol.start_oauth_redirect("https://x.example.com")
            self.assertFalse(r["ok"]); self.assertTrue(r.get("skip"))

    def test_complete_consumes_state_and_saves_tokens(self) -> None:
        start = ol.start_oauth_redirect("https://user1.example.com",
                                        return_path="/privacy")
        seen: dict = {}

        def fake_exchange(code, redirect):
            seen["code"], seen["redirect"] = code, redirect
            return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                    "scope": "Mail.ReadBasic Calendars.Read offline_access"}

        with patch.object(ol, "_exchange_code", fake_exchange):
            r = ol.complete_oauth_redirect("the-code", start["state"], redirect_uri="")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["return_path"], "/privacy")
        self.assertEqual(seen["redirect"],
                         "https://user1.example.com/oauth/outlook/callback")
        self.assertTrue(ol.connected())
        self.assertTrue(connectors.get("outlook").connected())
        # State is single-use.
        self.assertFalse(ol.complete_oauth_redirect("x", start["state"])["ok"])
        # Disconnect drops the token file.
        self.assertTrue(connectors.get("outlook").disconnect()["ok"])
        self.assertFalse(ol.connected())

    def test_complete_refuses_wide_grant(self) -> None:
        start = ol.start_oauth_redirect("https://user1.example.com")
        with patch.object(ol, "_token_request", lambda fields: {
                "access_token": "at", "scope": "Mail.ReadWrite Calendars.Read"}):
            r = ol.complete_oauth_redirect("c", start["state"])
        self.assertFalse(r["ok"])
        self.assertIn("disallowed", r["error"])
        self.assertFalse(ol.connected())

    def test_public_client_omits_secret_confidential_sends_it(self) -> None:
        captured = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"access_token":"a","scope":"Mail.ReadBasic Calendars.Read"}'

        def fake_urlopen(req, timeout=30):
            captured["body"] = req.data.decode()
            captured["url"] = req.full_url
            return _Resp()

        with patch.object(ol, "urlopen", fake_urlopen):
            ol._exchange_code("c", "https://x/oauth/outlook/callback")
        self.assertIn("login.microsoftonline.com/common/oauth2/v2.0/token", captured["url"])
        self.assertNotIn("client_secret", captured["body"])
        object.__setattr__(ol._cfg(), "client_secret", "s3cret")
        with patch.object(ol, "urlopen", fake_urlopen):
            ol._exchange_code("c", "https://x/oauth/outlook/callback")
        self.assertIn("client_secret=s3cret", captured["body"])

    def test_access_token_refreshes_when_stale(self) -> None:
        ol._save_tokens({"access_token": "old", "refresh_token": "rt",
                         "expires_in": 3600})
        tok = ol.load_tokens(); tok["obtained_at"] = NOW - 7200
        ol._save_json(ol._token_path(), tok)
        with patch.object(ol, "_refresh_token",
                          lambda r: {"access_token": "new", "expires_in": 3600}):
            self.assertEqual(ol._access_token(), "new")
        self.assertEqual(ol.load_tokens()["refresh_token"], "rt")


MAIL_PAGE_1 = {
    "value": [
        {"id": "AAMk1", "conversationId": "conv-1",
         "internetMessageId": "<m1@contoso.com>",
         "receivedDateTime": "2025-09-21T15:04:05.1234567Z",
         "from": {"emailAddress": {"name": "Priya Natarajan",
                                   "address": "Priya@Contoso.com"}},
         "toRecipients": [{"emailAddress": {"name": "Me", "address": "me@x.com"}}],
         "ccRecipients": [{"emailAddress": {"address": "sam@contoso.com"}}],
         "subject": "Q4 vendor contract", "isDraft": False},
        {"id": "AAMk2", "conversationId": "conv-2", "isDraft": True,
         "from": {"emailAddress": {"address": "me@x.com"}},
         "receivedDateTime": "2025-09-21T16:00:00Z", "subject": "draft"},
    ],
    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=100",
}
MAIL_PAGE_2 = {"value": [
    {"id": "AAMk3", "conversationId": "conv-1", "internetMessageId": "<m3@contoso.com>",
     "receivedDateTime": "2025-09-20T09:00:00Z",
     "from": {"emailAddress": {"name": "Sam Ortiz", "address": "sam@contoso.com"}},
     "toRecipients": [{"emailAddress": {"address": "me@x.com"}}],
     "subject": "Re: Q4 vendor contract", "isDraft": False}]}
CAL_PAGE = {"value": [
    {"id": "EV1", "subject": "Contract review", "isCancelled": False,
     "start": {"dateTime": "2025-09-22T14:00:00.0000000", "timeZone": "UTC"},
     "end": {"dateTime": "2025-09-22T14:30:00.0000000", "timeZone": "UTC"},
     "attendees": [{"emailAddress": {"name": "Priya Natarajan",
                                     "address": "priya@contoso.com"}},
                   {"emailAddress": {"address": ""}}],
     "organizer": {"emailAddress": {"name": "Me", "address": "ME@x.com"}},
     "seriesMasterId": "SER1"},
    {"id": "EV2", "subject": "Cancelled thing", "isCancelled": True,
     "start": {"dateTime": "2025-09-22T15:00:00"}, "end": {"dateTime": "2025-09-22T16:00:00"}},
]}


def _fake_graph(urls: list):
    def get(url):
        urls.append(url)
        if "/me/messages" in url:
            return MAIL_PAGE_2 if "$skip" in url else MAIL_PAGE_1
        if "/me/calendarView" in url:
            return CAL_PAGE
        raise AssertionError(url)
    return get


class GraphMappingTests(_Sandbox):
    def test_mail_headers_shape_and_pagination(self) -> None:
        urls: list = []
        with patch.object(ol, "_graph_get", _fake_graph(urls)):
            rows = ol.fetch_mail_headers(days=7, now=NOW, include_subject=True)
        self.assertEqual(len(urls), 2)
        self.assertIn("$select=", urls[0])
        self.assertIn("subject", urls[0])
        self.assertNotIn("body", urls[0])
        self.assertEqual([r["id"] for r in rows], ["<m1@contoso.com>", "<m3@contoso.com>"])
        m1 = rows[0]
        self.assertEqual(m1["thread_id"], "conv-1")
        self.assertEqual(m1["headers"]["from"], "Priya Natarajan <priya@contoso.com>")
        self.assertEqual(m1["headers"]["cc"], "sam@contoso.com")
        self.assertEqual(m1["headers"]["subject"], "Q4 vendor contract")
        self.assertAlmostEqual(m1["ts"], 1758467045.123456, places=3)
        # Header blob parses through the shared RFC 2822 helper.
        parties = ex.parse_rfc2822_addr(m1["headers"]["from"])
        self.assertEqual(parties[0]["email"], "priya@contoso.com")

    def test_mail_without_subject_by_default(self) -> None:
        with patch.object(ol, "_graph_get", _fake_graph([])):
            rows = ol.fetch_mail_headers(days=7, now=NOW)
        self.assertNotIn("subject", rows[0]["headers"])

    def test_body_in_response_aborts(self) -> None:
        page = {"value": [{**MAIL_PAGE_1["value"][0], "body": {"content": "hi"}}]}
        with patch.object(ol, "_graph_get", lambda url: page):
            with self.assertRaises(RuntimeError):
                ol.fetch_mail_headers(days=1, now=NOW)

    def test_calendar_view_mapping(self) -> None:
        urls: list = []
        with patch.object(ol, "_graph_get", _fake_graph(urls)):
            evs = ol.fetch_calendar_events(days=7, now=NOW)
        self.assertIn("/me/calendarView?startDateTime=", urls[0])
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev["title"], "Contract review")
        self.assertEqual(ev["end"] - ev["start"], 1800.0)
        self.assertEqual(ev["attendees"], [{"email": "priya@contoso.com",
                                            "name": "Priya Natarajan"}])
        self.assertEqual(ev["organizer"]["email"], "me@x.com")
        self.assertEqual(ev["recurrence"], ["SER1"])


class BackgroundCaptureTests(_Sandbox):
    def test_fetch_items_lands_via_scheduler_and_is_idempotent(self) -> None:
        from app.services.connectors import scheduler
        from app.storage import Store
        store = Store(self.data / "t.db")
        c = connectors.get("outlook")
        with patch.object(ol, "_graph_get", _fake_graph([])):
            items, cur = c.fetch_items(cursor={}, now=NOW)
        self.assertEqual(cur["since"], NOW)
        kinds = sorted(i["kind"] for i in items)
        self.assertEqual(kinds, ["calendar", "mail", "mail"])
        mail = next(i for i in items if i["external_id"] == "<m1@contoso.com>")
        self.assertEqual(mail["thread_id"], "conv-1")
        self.assertIn("Priya Natarajan", mail["people"])
        self.assertIn("Subject: Q4 vendor contract", mail["text"])
        cal = next(i for i in items if i["kind"] == "calendar")
        self.assertEqual(cal["people"], ["Priya Natarajan"])

        with patch.object(ol, "_graph_get", _fake_graph([])), \
             patch.object(ol, "connected", lambda: True), \
             patch("app.services.capture_consent.allows_connector", lambda cid: True):
            r1 = scheduler.sync_connector(c, store=store, now=NOW, force=True)
            r2 = scheduler.sync_connector(c, store=store, now=NOW + 600, force=True)
        self.assertEqual(r1["landed"], 3, r1)
        self.assertEqual(r2["landed"], 0, r2)
        rows = store.recent_events(source_substr="outlook", limit=20)
        srcs = {r["source"] for r in rows}
        self.assertIn("outlook.mail", srcs)
        self.assertIn("outlook.calendar", srcs)
        ev = next(r for r in rows if r["source"] == "outlook.mail")
        self.assertEqual(ev["meta"].get("connector_id"), "outlook")
        self.assertTrue(ev["meta"].get("never_authorizes"))

    def test_scheduler_treats_outlook_as_syncable_only_when_consented(self) -> None:
        from app.services.connectors import scheduler
        c = connectors.get("outlook")
        with patch.object(ol, "connected", lambda: True), \
             patch("app.services.capture_consent.allows_connector", lambda cid: False):
            self.assertFalse(scheduler.syncable(c))
        with patch.object(ol, "connected", lambda: True), \
             patch("app.services.capture_consent.allows_connector", lambda cid: True):
            self.assertTrue(scheduler.syncable(c))
        self.assertIn("Outlook", scheduler.consent_sentence(c))


class ProviderIngestTests(_Sandbox):
    def test_run_ingest_stamps_outlook_sources(self) -> None:
        from app.storage import Store
        store = Store(self.data / "t.db")
        with patch.object(ex, "_ledger_path", lambda: self.data / "ledger.json"):
            with patch.object(ol, "_graph_get", _fake_graph([])), \
                 patch.object(ol, "connected", lambda: True), \
                 patch("app.storage.get_store", lambda: store):
                out = ol.run_ingest(store=store, now=NOW)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out["messages"], 2)
        self.assertEqual(out["calendar_events"], 1)
        rows = store.recent_events(source_substr="exhaust", limit=50)
        srcs = {r["source"] for r in rows}
        self.assertIn("exhaust.outlook", srcs)
        self.assertIn("exhaust.mscalendar", srcs)
        self.assertNotIn("exhaust.gmail", srcs)
        raws = [r["raw"] for r in rows if r["source"] == "exhaust.outlook"]
        self.assertTrue(any("Outlook metadata ingest" in r for r in raws))
        self.assertEqual(ol.progress()["running"], False)

    def test_google_default_unchanged_and_fetch_refused_for_others(self) -> None:
        r = ex.run_ingest(fetch=True, provider="outlook")
        self.assertFalse(r["ok"])
        self.assertEqual(ex.PROVIDERS["google"]["mail"], ex.SOURCE_GMAIL)


class CallbackRouteTests(_Sandbox):
    """The generic /oauth/{provider}/callback serves outlook like google."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(f"fastapi TestClient unavailable: {exc}")

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.adoption import router
        app = FastAPI(); app.include_router(router)
        return TestClient(app)

    def test_outlook_callback_completes_and_returns(self) -> None:
        start = ol.start_oauth_redirect("https://seat.example.com",
                                        return_path="/console")
        with patch.object(ol, "_exchange_code", lambda c, r: {
                "access_token": "a", "refresh_token": "r",
                "scope": "Mail.ReadBasic Calendars.Read"}):
            resp = self._client().get(
                f"/oauth/outlook/callback?code=abc&state={start['state']}",
                headers={"origin": "https://seat.example.com"},
                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/console?connected=outlook")
        self.assertTrue(ol.connected())

    def test_outlook_callback_relays_to_minting_origin(self) -> None:
        state = ex._encode_state("https://rot-9.trycloudflare.com")
        resp = self._client().get(
            f"/oauth/outlook/callback?code=abc&state={state}",
            headers={"origin": "https://gb10.ts.net"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["location"].startswith(
            "https://rot-9.trycloudflare.com/oauth/outlook/callback?"))

    def test_error_and_unknown_provider(self) -> None:
        resp = self._client().get("/oauth/outlook/callback?error=access_denied",
                                  follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("oauth_error=access_denied", resp.headers["location"])
        self.assertEqual(self._client().get("/oauth/slack/callback?code=x",
                                            follow_redirects=False).status_code, 404)


if __name__ == "__main__":
    unittest.main()
