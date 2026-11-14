"""Google Drive connector — registry and catalog, the shared-client config
fallback, Google's auth URL and scope guard, token exchange and refresh,
file listing and text export mapping, metadata-only grants, and landing
through the scheduler next to the Gmail connector's untouched token."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import connectors
from app.services.connectors import google_drive as gd
from app.services.connectors import oauth_base as ob

NOW = 1_762_800_000.0
ISO_RECENT = "2025-11-10T12:00:00.000Z"
ISO_OLD = "2024-01-01T00:00:00.000Z"


class RegistryTests(unittest.TestCase):
    def test_ready_in_directory_google_still_first(self) -> None:
        self.assertIsInstance(connectors.get("google_drive"), gd.GoogleDriveConnector)
        self.assertEqual(connectors.for_tool("Google Drive").id, "google_drive")
        self.assertEqual(connectors.for_tool("Google Docs").id, "google_drive")
        self.assertEqual(connectors.for_tool("Gmail").id, "google")
        ready = [c.id for c in connectors.all() if c.kind == "directory"]
        self.assertEqual(ready[0], "google")
        self.assertIn("google_drive", ready)
        rows = {c["id"]: c for c in connectors.directory()["connectors"]}
        row = rows["google_drive"]
        self.assertEqual(row["category"], "productivity")
        self.assertEqual(row["auth"], "oauth")
        self.assertIn("drive.readonly", row["scopes_blurb"])
        self.assertEqual(row["scopes"], [gd.CONTENT_SCOPE])
        from app.services import api_auth
        self.assertTrue(api_auth.path_is_exempt("/oauth/google_drive/callback", "GET"))

    def test_source_policy_and_lane_know_it(self) -> None:
        from app.services import source_policy
        source_policy._connector_ids.cache_clear()
        self.assertEqual(source_policy.classify_source(event_source="google_drive.document"),
                         "connector_item")
        from app.services.connectors import lane
        self.assertEqual(lane.connector_for_host("docs.google.com")["id"], "google_drive")
        self.assertEqual(lane.connector_for_host("drive.google.com")["id"], "google_drive")
        self.assertEqual(lane.connector_for_host("mail.google.com")["id"], "google")

    def test_consent_sentence_is_read_only_and_ten_minutes(self) -> None:
        from app.services.connectors import scheduler
        with patch.dict(os.environ, {"QUILL_CONNECTOR_SYNC_S_GOOGLE_DRIVE": ""}, clear=False):
            s = scheduler.consent_sentence(gd.google_drive)
        self.assertIn("read-only", s)
        self.assertIn("Docs, Sheets and Slides", s)
        self.assertIn("10 minutes", s)


class ConfigTests(unittest.TestCase):
    def test_falls_back_to_the_gmail_client_until_overridden(self) -> None:
        # The dataclass freezes at import like every other config here, so
        # the fallback lives in a helper that reads env live.
        from app import config
        with patch.dict(os.environ, {"GOOGLE_OAUTH_CLIENT_ID": "g-id",
                                     "GOOGLE_OAUTH_CLIENT_SECRET": "g-secret",
                                     "GOOGLE_DRIVE_OAUTH_CLIENT_ID": "",
                                     "GOOGLE_DRIVE_OAUTH_CLIENT_SECRET": ""}, clear=False):
            self.assertEqual((config._google_drive_client("ID"),
                              config._google_drive_client("SECRET")), ("g-id", "g-secret"))
            os.environ["GOOGLE_DRIVE_OAUTH_CLIENT_ID"] = "d-id"
            os.environ["GOOGLE_DRIVE_OAUTH_CLIENT_SECRET"] = "d-secret"
            self.assertEqual((config._google_drive_client("ID"),
                              config._google_drive_client("SECRET")), ("d-id", "d-secret"))
        self.assertEqual(config.settings.google_drive.scopes,
                         os.environ.get("GOOGLE_DRIVE_OAUTH_SCOPES") or gd.CONTENT_SCOPE)

    def test_token_lives_apart_from_the_gmail_token(self) -> None:
        from app import config
        with patch.dict(os.environ, {"QUILL_DATA_DIR": "/d"}, clear=False):
            os.environ.pop("QUILL_GOOGLE_DRIVE_TOKEN_PATH", None)
            os.environ.pop("QUILL_EXHAUST_TOKEN", None)
            drive = config.GoogleDriveConfig().token_path
            state = config.GoogleDriveConfig().oauth_state_path
            mail = config.ExhaustConfig().token_path
        self.assertEqual(drive, "/d/connectors/google_drive/token.json")
        self.assertEqual(state, "/d/connectors/google_drive/oauth_state.json")
        self.assertNotEqual(drive, mail)


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data = Path(self._td.name)
        self._env = patch.dict(os.environ, {
            "QUILL_PUBLIC_BASE_URL": "", "QUILL_PEER_BASE_URL": "",
            "QUILL_OAUTH_REDIRECT_BASE": "", "QUILL_GOOGLE_DRIVE_MAX_BODIES": "",
            "QUILL_GOOGLE_DRIVE_MAX_PAGES": ""}, clear=False)
        self._env.start(); self.addCleanup(self._env.stop)
        self.cfg = SimpleNamespace(
            client_id="drive-client", client_secret="drive-secret",
            scopes=gd.CONTENT_SCOPE,
            token_path=str(self.data / "connectors" / "google_drive" / "token.json"),
            oauth_state_path=str(self.data / "connectors" / "google_drive" / "oauth_state.json"))
        p = patch.object(gd.GoogleDriveConnector, "_cfg", lambda self, _c=self.cfg: _c)
        p.start(); self.addCleanup(p.stop)
        gd.google_drive._last_counts = {}


class _Resp:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n=-1): return self.body if n < 0 else self.body[:n]


class OAuthTests(_Sandbox):
    def test_auth_url_is_google_offline_with_consent(self) -> None:
        r = gd.google_drive.begin_connect(public_base="https://seat.example.com",
                                          return_path="/settings")
        self.assertTrue(r["ok"], r)
        url = r["auth_url"]
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)
        self.assertIn("scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.readonly", url)
        self.assertIn("%2Foauth%2Fgoogle_drive%2Fcallback", url)
        self.assertEqual(r["redirect_uri"], "https://seat.example.com/oauth/google_drive/callback")
        self.assertEqual(r["return_path"], "/settings")

    def test_not_configured_names_both_client_pairs(self) -> None:
        self.cfg.client_id = ""
        r = gd.google_drive.begin_connect(public_base="https://seat.example.com")
        self.assertFalse(r["ok"]); self.assertTrue(r.get("skip"))
        self.assertIn("GOOGLE_OAUTH_CLIENT_ID", r["error"])
        self.assertIn("GOOGLE_DRIVE_OAUTH_CLIENT_ID", r["error"])
        self.assertIn("/oauth/google_drive/callback", r["error"])

    def test_scope_guard(self) -> None:
        c = gd.google_drive
        c.assert_scopes(gd.CONTENT_SCOPE)
        c.assert_scopes(gd.METADATA_SCOPE)          # Google narrowed the grant
        c.assert_scopes("")
        with self.assertRaises(PermissionError):
            c.assert_scopes(f"{gd.CONTENT_SCOPE} https://www.googleapis.com/auth/drive")
        with self.assertRaises(PermissionError):
            c.assert_scopes("https://www.googleapis.com/auth/gmail.readonly")

    def test_exchange_is_form_post_and_records_scope(self) -> None:
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url
            captured["body"] = dict(x.split("=", 1) for x in req.data.decode().split("&"))
            return _Resp(json.dumps({
                "access_token": "ya29.x", "refresh_token": "1//r", "expires_in": 3599,
                "scope": gd.CONTENT_SCOPE, "token_type": "Bearer"}).encode())

        with patch.object(ob, "urlopen", fake_urlopen):
            r = gd.google_drive.complete_oauth_redirect(
                "c0de", "bad-state", redirect_uri="https://x/oauth/google_drive/callback")
            self.assertFalse(r["ok"])
            start = gd.google_drive.start_oauth_redirect("https://seat.example.com")
            r = gd.google_drive.complete_oauth_redirect("c0de", start["state"], redirect_uri="")
        self.assertTrue(r["ok"], r)
        self.assertEqual(captured["url"], "https://oauth2.googleapis.com/token")
        self.assertEqual(captured["body"]["grant_type"], "authorization_code")
        self.assertEqual(captured["body"]["client_id"], "drive-client")
        self.assertEqual(captured["body"]["redirect_uri"],
                         "https%3A%2F%2Fseat.example.com%2Foauth%2Fgoogle_drive%2Fcallback")
        self.assertTrue(gd.google_drive.connected())
        self.assertTrue(gd.google_drive.content_scope_granted())
        self.assertTrue(gd.google_drive.status()["content_scope_granted"])
        self.assertEqual(gd.google_drive.auth_mode(), "oauth")

    def test_exchange_refuses_a_wider_grant(self) -> None:
        with patch.object(ob, "urlopen", lambda req, timeout=30: _Resp(json.dumps({
                "access_token": "ya29.x", "scope": "https://www.googleapis.com/auth/drive"
                }).encode())):
            start = gd.google_drive.start_oauth_redirect("https://seat.example.com")
            r = gd.google_drive.complete_oauth_redirect("c0de", start["state"], redirect_uri="")
        self.assertFalse(r["ok"]); self.assertIn("disallowed", r["error"])
        self.assertFalse(gd.google_drive.connected())

    def test_refresh_keeps_the_refresh_token(self) -> None:
        gd.google_drive._save_json(Path(self.cfg.token_path), {
            "access_token": "old", "refresh_token": "1//r", "expires_in": 3600,
            "obtained_at": time.time() - 7200, "scope": gd.CONTENT_SCOPE})
        seen = {}

        def fake_urlopen(req, timeout=30):
            seen["body"] = dict(x.split("=", 1) for x in req.data.decode().split("&"))
            # Google's refresh reply carries no refresh_token.
            return _Resp(b'{"access_token":"fresh","expires_in":3599,"token_type":"Bearer"}')

        with patch.object(ob, "urlopen", fake_urlopen):
            self.assertEqual(gd.google_drive._access_token(), "fresh")
        self.assertEqual(seen["body"]["grant_type"], "refresh_token")
        tok = gd.google_drive.load_tokens()
        self.assertEqual((tok["access_token"], tok["refresh_token"]), ("fresh", "1//r"))

    def test_metadata_only_grant_is_recorded(self) -> None:
        gd.google_drive._save_tokens({"access_token": "ya29.x", "scope": gd.METADATA_SCOPE})
        self.assertTrue(gd.google_drive.connected())
        self.assertFalse(gd.google_drive.content_scope_granted())

    def test_disconnect_leaves_the_gmail_token_alone(self) -> None:
        gmail = self.data / "connectors" / "google" / "token.json"
        gmail.parent.mkdir(parents=True)
        gmail.write_text('{"access_token": "mail"}')
        gd.google_drive._save_tokens({"access_token": "ya29.x", "scope": gd.CONTENT_SCOPE})
        out = gd.google_drive.disconnect()
        self.assertEqual(out["removed"], [self.cfg.token_path])
        self.assertFalse(gd.google_drive.connected())
        self.assertEqual(json.loads(gmail.read_text())["access_token"], "mail")


DOC = {"id": "d-1", "name": "Q4 roadmap", "mimeType": "application/vnd.google-apps.document",
       "modifiedTime": ISO_RECENT, "createdTime": "2025-10-01T00:00:00.000Z",
       "webViewLink": "https://docs.google.com/document/d/d-1/edit", "parents": ["folder-9"],
       "owners": [{"displayName": "Sam Okafor", "emailAddress": "sam@acme.test"}],
       "lastModifyingUser": {"displayName": "Priya Natarajan", "emailAddress": "priya@acme.test"}}
SHEET = {"id": "s-1", "name": "Hiring plan", "mimeType": "application/vnd.google-apps.spreadsheet",
         "modifiedTime": ISO_RECENT, "webViewLink": "https://docs.google.com/spreadsheets/d/s-1",
         "owners": [{"displayName": "Priya Natarajan"}],
         "lastModifyingUser": {"displayName": "Priya Natarajan"}}
PDF = {"id": "p-1", "name": "MSA signed.pdf", "mimeType": "application/pdf", "size": "48211",
       "modifiedTime": ISO_OLD, "sharedWithMeTime": ISO_RECENT,
       "webViewLink": "https://drive.google.com/file/d/p-1/view",
       "owners": [{"emailAddress": "legal@partner.test"}],
       "lastModifyingUser": {"displayName": "Dana Legal"}, "description": "Countersigned copy"}
FOLDER = {"id": "f-1", "name": "Contracts", "mimeType": gd.FOLDER, "modifiedTime": ISO_RECENT}
NOTE = {"id": "n-1", "name": "notes.md", "mimeType": "text/markdown", "size": "120",
        "modifiedTime": ISO_RECENT, "webViewLink": "https://drive.google.com/file/d/n-1/view"}
BIG_TXT = {"id": "b-1", "name": "dump.txt", "mimeType": "text/plain", "size": str(5 * 1024 * 1024),
           "modifiedTime": ISO_RECENT}

PAGE_1 = {"files": [DOC, FOLDER, SHEET], "nextPageToken": "pg2"}
PAGE_2 = {"files": [PDF, NOTE, BIG_TXT]}


def _fake_request(calls: list):
    def req(self, url, *, method="GET", body=None, headers=None):
        calls.append(url)
        if "/files?" in url:
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(url).query)
            assert "modifiedTime >" in qs["q"][0] and "sharedWithMeTime >" in qs["q"][0]
            assert qs["includeItemsFromAllDrives"] == ["true"]
            return (PAGE_2 if qs.get("pageToken") == ["pg2"] else PAGE_1), {}
        if url.endswith("about?fields=user(displayName,emailAddress)"):
            return {"user": {"displayName": "Sam Okafor", "emailAddress": "sam@acme.test"}}, {}
        raise AssertionError(url)
    return req


def _fake_text(calls: list):
    def fetch(self, url):
        calls.append(url)
        if "/files/d-1/export?mimeType=text%2Fplain" in url:
            return "﻿Q4 roadmap\n\nShip search by December. Owner: Priya.\n"
        if "/files/s-1/export?mimeType=text%2Fcsv" in url:
            return "Role,Owner,Target\nSRE,Priya,Jan\n"
        if "/files/n-1?alt=media" in url:
            return "# notes\n- call Dana re: MSA\n"
        raise AssertionError(url)
    return fetch


class MappingTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        gd.google_drive._save_tokens({"access_token": "ya29.x", "scope": gd.CONTENT_SCOPE})

    def test_fetch_items_exports_text_and_skips_folders(self) -> None:
        calls, texts = [], []
        with patch.object(gd.GoogleDriveConnector, "_request", _fake_request(calls)), \
             patch.object(gd.GoogleDriveConnector, "_fetch_text", _fake_text(texts)):
            items, cursor = gd.google_drive.fetch_items(cursor=None, now=NOW)
        self.assertEqual([i["external_id"] for i in items],
                         ["file:d-1", "file:s-1", "file:p-1", "file:n-1", "file:b-1"])
        self.assertEqual(sum("/files?" in u for u in calls), 2)
        # One export per Doc/Sheet, one download for the small note, none for
        # the PDF or the 5 MB text file.
        self.assertEqual(len(texts), 3)
        doc = items[0]
        self.assertEqual(doc["kind"], "document")
        self.assertEqual(doc["title"], "Q4 roadmap")
        self.assertTrue(doc["text"].startswith("Google Drive doc Q4 roadmap\n"))
        self.assertIn("Edited by Priya Natarajan · Owner Sam Okafor", doc["text"])
        self.assertIn("Ship search by December.", doc["text"])
        self.assertNotIn("﻿", doc["text"])
        self.assertEqual(doc["people"], ["Priya Natarajan", "Sam Okafor"])
        self.assertEqual(doc["thread_id"], "folder-9")
        self.assertEqual(doc["url"], "https://docs.google.com/document/d/d-1/edit")
        self.assertEqual(doc["meta"]["folder_id"], "folder-9")
        self.assertAlmostEqual(doc["ts"], ob.parse_iso(ISO_RECENT))
        sheet = items[1]
        self.assertIn("SRE,Priya,Jan", sheet["text"])
        self.assertNotIn("Owner Priya", sheet["text"])   # owner == editor: said once
        pdf = items[2]
        self.assertEqual(pdf["kind"], "file")
        self.assertTrue(pdf["text"].startswith("Google Drive PDF MSA signed.pdf: Countersigned copy\n"))
        self.assertIn("shared with you", pdf["text"])
        self.assertEqual(pdf["people"], ["Dana Legal", "legal@partner.test"])
        self.assertTrue(pdf["meta"]["shared_with_me"])
        self.assertAlmostEqual(pdf["ts"], ob.parse_iso(ISO_OLD))
        self.assertIn("call Dana re: MSA", items[3]["text"])
        self.assertEqual(items[4]["text"], "Google Drive text file dump.txt")
        self.assertEqual(cursor["since"], NOW - ob.FETCH_OVERLAP_S)
        self.assertEqual(gd.google_drive.status_detail(), "Last check found 2 documents, 3 files.")

    def test_metadata_grant_reads_no_text(self) -> None:
        gd.google_drive._save_tokens({"access_token": "ya29.x", "scope": gd.METADATA_SCOPE})
        texts = []
        with patch.object(gd.GoogleDriveConnector, "_request", _fake_request([])), \
             patch.object(gd.GoogleDriveConnector, "_fetch_text", _fake_text(texts)):
            items, _ = gd.google_drive.fetch_items(cursor={"since": NOW - 3600}, now=NOW)
        self.assertEqual(texts, [])
        self.assertEqual(items[0]["text"],
                         "Google Drive doc Q4 roadmap\nEdited by Priya Natarajan · Owner Sam Okafor\n"
                         "https://docs.google.com/document/d/d-1/edit")

    def test_body_budget_and_export_failure(self) -> None:
        def flaky(self, url):
            raise ob.OAuthApiError(403, "export denied")
        with patch.dict(os.environ, {"QUILL_GOOGLE_DRIVE_MAX_BODIES": "1"}), \
             patch.object(gd.GoogleDriveConnector, "_request", _fake_request([])), \
             patch.object(gd.GoogleDriveConnector, "_fetch_text", flaky):
            items, _ = gd.google_drive.fetch_items(cursor=None, now=NOW)
        self.assertEqual(len(items), 5)
        self.assertTrue(all("Ship search" not in i["text"] for i in items))

    def test_whoami_and_fetch_text_headers(self) -> None:
        with patch.object(gd.GoogleDriveConnector, "_request", _fake_request([])):
            self.assertEqual(gd.google_drive._whoami(),
                             {"account": "sam@acme.test", "name": "Sam Okafor"})
        seen = {}

        def fake_urlopen(req, timeout=30):
            seen["auth"] = req.get_header("Authorization")
            seen["url"] = req.full_url
            return _Resp("﻿hello".encode("utf-8"))

        with patch.object(gd, "urlopen", fake_urlopen):
            self.assertEqual(gd.google_drive.file_text(DOC), "hello")
        self.assertEqual(seen["auth"], "Bearer ya29.x")
        self.assertEqual(seen["url"],
                         f"{gd.API}/files/d-1/export?mimeType=text%2Fplain")

    def test_lookup_matches_titles(self) -> None:
        with patch.object(gd.GoogleDriveConnector, "_request", _fake_request([])), \
             patch.object(gd.GoogleDriveConnector, "_fetch_text", _fake_text([])):
            hits = gd.google_drive.lookup("the Q4 roadmap doc")
        self.assertEqual([h["external_id"] for h in hits], ["file:d-1"])


class LandingTests(_Sandbox):
    def test_lands_once_with_link_and_connector_stamp(self) -> None:
        from app.services.connectors import scheduler
        from app.storage import Store
        store = Store(self.data / "t.db")
        gd.google_drive._save_tokens({"access_token": "ya29.x", "scope": gd.CONTENT_SCOPE})
        with patch.object(gd.GoogleDriveConnector, "_request", _fake_request([])), \
             patch.object(gd.GoogleDriveConnector, "_fetch_text", _fake_text([])):
            r1 = scheduler.sync_connector(gd.google_drive, store=store, now=NOW, force=True)
            r2 = scheduler.sync_connector(gd.google_drive, store=store, now=NOW + 600, force=True)
        self.assertEqual((r1["landed"], r2["landed"]), (5, 0))
        doc = next(r for r in store.recent_events(source_substr="google_drive.document", limit=5)
                   if r["meta"]["external_id"] == "file:d-1")
        self.assertEqual(doc["meta"]["connector_id"], "google_drive")
        self.assertEqual(doc["meta"]["url"], "https://docs.google.com/document/d/d-1/edit")
        self.assertTrue(doc["meta"]["never_authorizes"])
        self.assertIn("Ship search by December.", doc["raw"])
        pdf = next(r for r in store.recent_events(source_substr="google_drive.file", limit=5)
                   if r["meta"]["external_id"] == "file:p-1")
        self.assertEqual(pdf["meta"]["mime_type"], "application/pdf")


if __name__ == "__main__":
    unittest.main()
