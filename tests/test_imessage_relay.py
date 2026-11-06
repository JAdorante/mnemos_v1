"""iMessage on a hosted seat: pairing, the push endpoint's distrust of what it
receives, consent, revocation, and the real helper zipapp against a real HTTP
server. The helper runs as a separate process from outside the repo, so a stray
import of the rest of Sparrow fails here rather than on a tester's Mac.

macOS itself (Full Disk Access, launchd) can't be exercised on the build box.
"""
from __future__ import annotations

import ast
import io
import os
import plistlib
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import adoption  # noqa: E402
from app.services import api_auth  # noqa: E402
from app.services import capture_consent  # noqa: E402
from app.services.connectors import imessage as im  # noqa: E402
from app.services.connectors import imessage_helper as helper  # noqa: E402
from app.services.connectors import imessage_reader as reader  # noqa: E402
from app.services.connectors import imessage_relay as relay  # noqa: E402
from app.services.connectors import scheduler  # noqa: E402
from app.storage import Store  # noqa: E402

NOW = 1_790_000_000.0


def _item(guid="g1", body="From Sam Lee: Can you send the deck?", **kw):
    return {"kind": "message", "external_id": guid, "thread_id": "t1", "ts": NOW - 60,
            "title": body[:50], "body": body, "people": ["Sam Lee"], "from": "+15551234567",
            "to": "me", "from_self": False, "meta": {"service": "iMessage"}, **kw}


def _make_chat_db(path: Path, *texts: str) -> None:
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS handle (ROWID INTEGER PRIMARY KEY, id TEXT);
      CREATE TABLE IF NOT EXISTS chat (ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT);
      CREATE TABLE IF NOT EXISTS chat_handle_join (chat_id INTEGER, handle_id INTEGER);
      CREATE TABLE IF NOT EXISTS chat_message_join (chat_id INTEGER, message_id INTEGER);
      CREATE TABLE IF NOT EXISTS message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT,
        text TEXT, attributedBody BLOB, handle_id INTEGER, date INTEGER,
        is_from_me INTEGER DEFAULT 0, service TEXT DEFAULT 'iMessage');
      INSERT OR IGNORE INTO handle VALUES (1, '+15551234567');
      INSERT OR IGNORE INTO chat VALUES (1, 'c1', '');
    """)
    if not c.execute("SELECT 1 FROM chat_handle_join").fetchone():
        c.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
    now_ns = reader.to_mac_ns(time.time() - 60)
    for t in texts:
        n = c.execute("SELECT COUNT(*) FROM message").fetchone()[0] + 1
        rid = c.execute("INSERT INTO message (guid, text, handle_id, date) VALUES (?, ?, 1, ?)",
                        (f"guid-{n}", t, now_ns)).lastrowid
        c.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rid,))
    c.commit()
    c.close()


class _Seat(unittest.TestCase):
    """A seat: relay on, no local Messages, its own state and store."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.td = Path(td.name)
        self.store = Store(self.td / "seat.db")
        self.consented = {"on": True}
        for p in (
            mock.patch.dict(os.environ, {"QUILL_IMESSAGE_RELAY": "1", "QUILL_IMESSAGE_DB": ""}),
            mock.patch.object(im.sys, "platform", "linux"),
            mock.patch.object(relay, "_state_path", lambda: self.td / "relay.json"),
            mock.patch("app.storage.get_store", lambda: self.store),
            mock.patch.object(capture_consent, "allows_connector",
                              lambda cid: self.consented["on"]),
            mock.patch.object(capture_consent, "record_connector",
                              mock.Mock(return_value={})),
            mock.patch("app.services.exemplar_store.delete_for_pair"),
            mock.patch("app.services.escalate_log.escalate_log.purge_containing",
                       return_value=0),
        ):
            p.start()
            self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(adoption.router)
        self.app = app
        self.client = TestClient(app)

    def paired(self) -> dict:
        code = relay.start_pairing()
        token = self.client.post("/imessage-relay/claim",
                                 json={"code": code, "device": "Sam's Mac"}).json()["token"]
        return {"Authorization": f"Bearer {token}"}

    def texts(self):
        return self.store.recent_events(source_substr="imessage", limit=50)


class ModeTests(unittest.TestCase):
    def test_flag_turns_a_seat_on_and_a_mac_stays_local(self):
        with mock.patch.dict(os.environ, {"QUILL_IMESSAGE_RELAY": "", "QUILL_IMESSAGE_DB": ""}), \
                mock.patch.object(im.sys, "platform", "linux"):
            self.assertFalse(im.relay_mode())
            self.assertFalse(im.imessage.configured())
        with mock.patch.dict(os.environ, {"QUILL_IMESSAGE_RELAY": "1", "QUILL_IMESSAGE_DB": ""}):
            with mock.patch.object(im.sys, "platform", "linux"):
                self.assertTrue(im.relay_mode())
                self.assertTrue(im.imessage.configured())
                self.assertTrue(im.imessage.push_only)
            with mock.patch.object(im.sys, "platform", "darwin"):
                self.assertFalse(im.relay_mode())

    def test_routes_do_not_exist_with_the_relay_off(self):
        app = FastAPI()
        app.include_router(adoption.router)
        client = TestClient(app)
        with mock.patch.dict(os.environ, {"QUILL_IMESSAGE_RELAY": "", "QUILL_IMESSAGE_DB": ""}):
            for call in (lambda: client.get("/imessage-relay/helper.pyz"),
                         lambda: client.post("/imessage-relay/claim", json={}),
                         lambda: client.post("/imessage-relay/push", json={"items": []})):
                self.assertEqual(call().status_code, 404)


class ConnectFlowTests(_Seat):
    def test_no_pairing_until_the_user_agrees_to_the_copy_off_their_mac(self):
        r = self.client.post("/connectors/imessage/connect").json()
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_consent"])
        self.assertIn("Whoever runs the server can read them", r["notice"])
        self.assertFalse(relay.pair_pending())
        capture_consent.record_connector.assert_not_called()
        r = self.client.post("/connectors/imessage/connect",
                             headers={"X-Consent-Confirmed": "1"}).json()
        self.assertEqual(r["mode"], "pair")
        self.assertTrue(relay.pair_pending())
        sentence = capture_consent.record_connector.call_args.kwargs["sentence"]
        self.assertIn("Whoever runs the server can read them", sentence)
        self.assertIn("/imessage-relay/helper.pyz", r["command"])

    def test_command_names_the_gateway_mount_for_this_seat(self):
        r = self.client.post(
            "/connectors/imessage/connect",
            headers={"X-Consent-Confirmed": "1", "Origin": "https://sparrow.example.com",
                     "X-Sparrow-Mcp-Mount": "/mcp/seat-1"}).json()
        self.assertIn("https://sparrow.example.com/mcp/seat-1/imessage-relay/helper.pyz",
                      r["command"])
        self.assertIn(" pair 'https://sparrow.example.com/mcp/seat-1' '", r["command"])

    def test_scheduler_never_polls_a_seat(self):
        self.paired()
        self.assertTrue(im.imessage.connected())
        self.assertFalse(scheduler.syncable(im.imessage))

    def test_status_says_which_mac_and_when(self):
        self.paired()
        st = im.imessage.status()
        self.assertTrue(st["connected"])
        self.assertIn("Sam's Mac checked in just now", st["status_detail"])


class PairingTests(_Seat):
    def test_code_is_single_use_and_expires(self):
        code = relay.start_pairing(now=NOW)
        self.assertIsNone(relay.claim(code, "Mac", now=NOW + relay.PAIR_TTL_S + 1))
        self.assertIsNone(relay.claim("wrong", "Mac", now=NOW))
        self.assertTrue(relay.claim(code, "Mac", now=NOW))
        self.assertIsNone(relay.claim(code, "Mac", now=NOW))

    def test_secrets_are_stored_only_as_hashes(self):
        code = relay.start_pairing()
        self.assertNotIn(code, (self.td / "relay.json").read_text())
        token = relay.claim(code, "Mac")
        self.assertNotIn(token, (self.td / "relay.json").read_text())
        self.assertEqual(oct((self.td / "relay.json").stat().st_mode & 0o777), "0o600")

    def test_a_new_mac_replaces_the_old_one(self):
        first = self.paired()
        self.paired()
        self.assertEqual(self.client.post("/imessage-relay/push", headers=first,
                                          json={"items": []}).status_code, 401)

    def test_an_abandoned_pairing_does_not_disturb_the_paired_mac(self):
        auth = self.paired()
        relay.start_pairing()
        self.assertEqual(self.client.post("/imessage-relay/push", headers=auth,
                                          json={"items": []}).status_code, 200)


class PushTests(_Seat):
    def test_requires_the_push_token_not_the_seat_token(self):
        self.paired()
        with mock.patch.dict(os.environ, {"QUILL_API_TOKEN": "seat-token"}):
            for headers in ({}, {"Authorization": "Bearer nope"},
                            {"Authorization": "Bearer seat-token"}):
                r = self.client.post("/imessage-relay/push", headers=headers,
                                     json={"items": [_item()]})
                self.assertEqual(r.status_code, 401)
        self.assertEqual(self.texts(), [])

    def test_lands_stamped_and_repeats_are_harmless(self):
        auth = self.paired()
        r = self.client.post("/imessage-relay/push", headers=auth, json={"items": [_item()]})
        self.assertEqual(r.json()["landed"], 1)
        r = self.client.post("/imessage-relay/push", headers=auth, json={"items": [_item()]})
        self.assertEqual(r.json()["landed"], 0)
        rows = self.texts()
        self.assertEqual(len(rows), 1)
        ev = rows[0]
        self.assertEqual(ev["source"], "imessage.message")
        self.assertEqual(ev["meta"]["privacy_class"], "sensitive")
        self.assertTrue(ev["meta"]["never_authorizes"])
        self.assertEqual(ev["meta"]["connector_id"], "imessage")
        self.assertEqual(self.store.connector_sync_get("imessage")["items_landed"], 1)

    def test_a_helper_cannot_lower_stamps_or_set_other_fields(self):
        auth = self.paired()
        evil = _item(meta={"privacy_class": "public", "never_authorizes": False,
                           "connector_id": "google", "service": "iMessage"},
                     kind="calendar", source="user.chat", never_authorizes=False)
        self.client.post("/imessage-relay/push", headers=auth, json={"items": [evil]})
        ev = self.texts()[0]
        self.assertEqual(ev["source"], "imessage.message")
        self.assertEqual(ev["meta"]["privacy_class"], "sensitive")
        self.assertTrue(ev["meta"]["never_authorizes"])
        self.assertEqual(ev["meta"]["connector_id"], "imessage")

    def test_filters_run_again_on_this_side(self):
        auth = self.paired()
        items = [_item("a", "From Sam: Your verification code is 482913"),
                 _item("b", "From Sam: key sk-ant-api03-" + "x" * 40),
                 _item("c", ""), "not an item", {"body": "no id"},
                 _item("d", "From Sam: dinner at 7?")]
        r = self.client.post("/imessage-relay/push", headers=auth, json={"items": items}).json()
        self.assertEqual(r["landed"], 1)
        self.assertEqual(r["dropped"], 5)
        self.assertEqual(len(self.texts()), 1)

    def test_a_future_timestamp_is_pulled_back_and_text_is_capped(self):
        out = relay.clean_item(_item(ts=NOW + 10 ** 6, body="x " * 20000), now=NOW)
        self.assertEqual(out["ts"], NOW)
        self.assertLessEqual(len(out["body"]), relay.MAX_TEXT)

    def test_oversized_and_malformed_batches_are_refused(self):
        auth = self.paired()
        big = b'{"items": ["' + b"x" * (relay.MAX_BODY_BYTES + 1) + b'"]}'
        self.assertEqual(self.client.post("/imessage-relay/push", headers=auth,
                                          content=big).status_code, 413)
        self.assertEqual(self.client.post("/imessage-relay/push", headers=auth,
                                          content=b"{nope").status_code, 400)
        self.assertEqual(self.client.post("/imessage-relay/push", headers=auth,
                                          json={"items": "x"}).status_code, 400)

    def test_withdrawn_consent_pauses_without_revoking(self):
        auth = self.paired()
        self.consented["on"] = False
        r = self.client.post("/imessage-relay/push", headers=auth, json={"items": [_item()]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.texts(), [])
        self.consented["on"] = True
        self.assertEqual(self.client.post("/imessage-relay/push", headers=auth,
                                          json={"items": [_item()]}).status_code, 200)

    def test_disconnect_revokes_at_once_and_purge_deletes(self):
        auth = self.paired()
        self.client.post("/imessage-relay/push", headers=auth, json={"items": [_item()]})
        self.assertTrue(self.client.post("/connectors/imessage/disconnect").json()["ok"])
        self.assertFalse(im.imessage.connected())
        self.assertEqual(self.client.post("/imessage-relay/push", headers=auth,
                                          json={"items": []}).status_code, 401)
        self.assertEqual(len(self.texts()), 1)
        r = self.client.post("/connectors/imessage/purge").json()
        self.assertEqual(r["deleted"], 1)
        self.assertEqual(self.texts(), [])

    def test_helper_can_report_only_known_problems(self):
        auth = self.paired()
        self.client.post("/imessage-relay/push", headers=auth,
                         json={"items": [], "error": "no_access"})
        self.assertIn("Full Disk Access", im.imessage.status()["error"])
        self.client.post("/imessage-relay/push", headers=auth,
                         json={"items": [], "error": "<script>anything</script>"})
        self.assertIsNone(im.imessage.status()["error"])

    def test_status_warns_when_the_mac_goes_quiet(self):
        code = relay.start_pairing(now=NOW)
        relay.claim(code, "Sam's Mac", now=NOW)
        line = relay.status_line(3, now=NOW + relay.STALE_AFTER_S + 60)
        self.assertIn("Is that Mac awake?", line)
        self.assertIn("3 texts kept", line)


class AuthGateTests(unittest.TestCase):
    def test_only_the_helper_routes_skip_the_lan_gate(self):
        for path, method in (("/imessage-relay/claim", "POST"),
                             ("/imessage-relay/push", "POST"),
                             ("/imessage-relay/helper.pyz", "GET")):
            self.assertTrue(api_auth.path_is_exempt(path, method), path)
        for path in ("/connectors/imessage/connect", "/connectors/imessage/purge",
                     "/connectors/imessage/disconnect"):
            self.assertFalse(api_auth.path_is_exempt(path, "POST"), path)


class GatewayTests(unittest.TestCase):
    def test_helper_routes_pass_through_by_seat_and_nothing_else_does(self):
        try:
            sys.path.insert(0, str(Path("deploy/gateway").resolve()))
            import gateway
        except ImportError as exc:
            self.skipTest(f"gateway deps missing: {exc}")
        finally:
            sys.path.pop(0)
        for rest in ("claim", "push", "helper.pyz"):
            self.assertEqual(gateway._mcp_route(f"mcp/seat-1/imessage-relay/{rest}"),
                             ("seat-1", f"/imessage-relay/{rest}"))
        self.assertIsNone(gateway._mcp_route("mcp/seat-1/connectors/imessage/purge"))
        self.assertIsNone(gateway._mcp_route("mcp/seat-1/imessage-relay/other"))


class HelperPackagingTests(unittest.TestCase):
    def test_zipapp_holds_only_what_the_helper_needs(self):
        with zipfile.ZipFile(io.BytesIO(relay.build_helper())) as z:
            names = set(z.namelist())
            self.assertEqual(names, {
                "__main__.py", "app/__init__.py", "app/services/__init__.py",
                "app/services/connectors/__init__.py", "app/services/redact.py",
                "app/services/privacy_class.py",
                "app/services/connectors/imessage_reader.py",
                "app/services/connectors/imessage_helper.py"})
            self.assertEqual(z.read("app/services/__init__.py"), b"")

    def test_helper_sources_parse_on_apples_python_39(self):
        # The Python on a Mac can be 3.9; a newer syntax would fail only there.
        for name in relay._HELPER_FILES:
            ast.parse((relay._ROOT / name).read_text("utf-8"), feature_version=(3, 9))

    def test_refuses_to_send_texts_over_plain_http(self):
        self.assertTrue(helper._safe_base("https://sparrow.example.com/mcp/s1"))
        self.assertTrue(helper._safe_base("http://127.0.0.1:8000"))
        self.assertFalse(helper._safe_base("http://sparrow.example.com"))
        self.assertEqual(helper.cmd_pair("http://sparrow.example.com", "c"), 2)

    def test_launch_agent_restarts_and_runs_the_helper(self):
        plist = plistlib.loads(plistlib.dumps(helper.launch_agent("/py", "/h.pyz")))
        self.assertEqual(plist["ProgramArguments"], ["/py", "/h.pyz", "run"])
        self.assertTrue(plist["KeepAlive"])
        self.assertTrue(plist["RunAtLoad"])


class SendWaitingTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.td = Path(td.name)
        _make_chat_db(self.td / "chat.db", "one", "two")
        for p in (mock.patch.dict(os.environ, {"QUILL_IMESSAGE_DB": str(self.td / "chat.db"),
                                               "QUILL_IMESSAGE_HELPER_HOME": str(self.td)}),
                  mock.patch.object(reader, "contact_names", lambda: {})):
            p.start()
            self.addCleanup(p.stop)
        self.cfg = {"base": "https://x", "token": "t", "cursor": {}}

    def test_place_only_moves_after_the_seat_accepts(self):
        for code in (403, 500):
            with mock.patch.object(helper, "_post", return_value=(code, {})):
                try:
                    helper.send_waiting(self.cfg)
                except OSError:
                    pass
            self.assertEqual(self.cfg["cursor"], {})
        with mock.patch.object(helper, "_post", return_value=(200, {"landed": 2})):
            self.assertEqual(helper.send_waiting(self.cfg), 2)
        self.assertEqual(self.cfg["cursor"]["rowid"], 2)

    def test_only_a_401_means_disconnected(self):
        with mock.patch.object(helper, "_post", return_value=(401, {})):
            with self.assertRaises(helper.Revoked):
                helper.send_waiting(self.cfg)
        for code in (404, 502, 530):
            with mock.patch.object(helper, "_post", return_value=(code, {})):
                with self.assertRaises(OSError):
                    helper.send_waiting(self.cfg)

    def test_backlog_drains_in_batches(self):
        _make_chat_db(self.td / "chat.db", *[f"m{i}" for i in range(5)])
        sent = []
        with mock.patch.object(helper, "BATCH", 2), mock.patch.object(
                helper, "_post",
                side_effect=lambda b, p, payload, t: (sent.append(len(payload["items"])) or (200, {}))):
            helper.send_waiting(self.cfg)
        self.assertEqual(sum(sent), 7)

    def test_helper_ships_the_cloudflare_safe_user_agent(self):
        self.assertNotIn("urllib", helper.USER_AGENT.lower())


class EndToEndTests(_Seat):
    """The built zipapp, in its own process, over real HTTP to the real routes."""

    def setUp(self):
        super().setUp()
        import uvicorn
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="error"))
        t = threading.Thread(target=self.server.run, daemon=True)
        t.start()
        self.addCleanup(t.join, 10)
        self.addCleanup(setattr, self.server, "should_exit", True)
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.mac = Path(tempfile.mkdtemp(dir=self.td))
        _make_chat_db(self.mac / "chat.db", "Can you send the deck?",
                      "Your verification code is 482913")
        self.pyz = self.mac / "sparrow-imessage.pyz"
        self.pyz.write_bytes(self.client.get("/imessage-relay/helper.pyz").content)

    def run_helper(self, *args, db=None):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.mac),
               "QUILL_IMESSAGE_DB": str(db or self.mac / "chat.db"),
               "QUILL_IMESSAGE_HELPER_HOME": str(self.mac / ".sparrow")}
        # -I and an unrelated cwd: nothing from this repo can be imported.
        return subprocess.run([sys.executable, "-I", str(self.pyz), *args], cwd=self.mac,
                              env=env, capture_output=True, text=True, timeout=60)

    def test_pair_send_more_disconnect(self):
        base = f"http://127.0.0.1:{self.port}"
        r = self.client.post("/connectors/imessage/connect",
                             headers={"X-Consent-Confirmed": "1",
                                      "Origin": "https://ignored.example"}).json()
        code = re.search(r"pair '[^']+' '([^']+)'", r["command"]).group(1)

        out = self.run_helper("pair", base, code)
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = self.texts()
        self.assertEqual(len(rows), 1)
        self.assertIn("Can you send the deck?", rows[0]["raw"])
        self.assertNotIn("482913", str(rows))
        self.assertTrue(im.imessage.connected())
        cfg = (self.mac / ".sparrow" / "imessage.json")
        self.assertEqual(oct(cfg.stat().st_mode & 0o777), "0o600")

        _make_chat_db(self.mac / "chat.db", "One more thing")
        out = self.run_helper("once")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.texts()), 2)

        out = self.run_helper("once")
        self.assertIn("Sent 0 texts", out.stdout)
        self.assertEqual(len(self.texts()), 2)

        self.client.post("/connectors/imessage/disconnect")
        out = self.run_helper("once")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("disconnected", out.stderr)
        self.assertFalse(cfg.exists())

    def test_unreadable_messages_keeps_the_code_for_a_retry(self):
        base = f"http://127.0.0.1:{self.port}"
        code = relay.start_pairing()
        out = self.run_helper("pair", base, code, db=self.mac / "missing.db")
        self.assertEqual(out.returncode, 3)
        self.assertIn("Full Disk Access", out.stderr)
        self.assertFalse(im.imessage.connected())
        self.assertEqual(self.run_helper("pair", base, code).returncode, 0)
        self.assertTrue(im.imessage.connected())

    def test_a_used_code_is_refused(self):
        base = f"http://127.0.0.1:{self.port}"
        code = relay.start_pairing()
        self.assertEqual(self.run_helper("pair", base, code).returncode, 0)
        out = self.run_helper("pair", base, code)
        self.assertEqual(out.returncode, 5)
        self.assertIn("didn't work", out.stderr)


if __name__ == "__main__":
    unittest.main()
