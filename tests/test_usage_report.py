"""WS-A — the share path: report JSON, redaction, and the consent gate.

The privacy commitment is that nothing leaves the machine without an explicit
act. These tests are how that commitment is enforced mechanically: the weekly
ping must make *zero* requests unless a URL is configured AND consent is
stored, and a failed ping must never surface into a caller.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import crash_report, usage_ledger as ul
from app.services.usage_ledger import UsageLedger
from app.storage import USAGE_COUNTER_COLUMNS, Store
from app.version import __version__


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_ur_"))
        self.env = patch.dict(os.environ, {
            "QUILL_DATA_DIR": str(self.tmp),
            "QUILL_USAGE_LEDGER": "1",
            "QUILL_USAGE_PING_URL": "",
        }, clear=False)
        self.env.start()
        self.store = Store(db_path=self.tmp / "quill.db",
                           audio_dir=self.tmp / "audio")
        self.led = UsageLedger()
        self.now = 1_756_000_000.0
        self.led.bump("searches", 4, now=self.now)
        self.led.bump("chat_turns", 2, now=self.now)
        self.led.mark_active(now=self.now)
        self.led.flush(self.store, now=self.now)

    def tearDown(self) -> None:
        self.env.stop()


class SchemaTests(_Base):
    def test_payload_shape(self) -> None:
        p = ul.report_payload(now=self.now, store=self.store)
        self.assertEqual(p["schema"], "mnemos.usage/1")
        self.assertEqual(p["version"], __version__)
        self.assertEqual(p["timezone"], "UTC")
        self.assertEqual(p["install_id"], ul.install_id())
        self.assertEqual(len(p["days"]), 1)
        self.assertEqual(p["days"][0]["searches"], 4)
        self.assertIn("is_wau", p["metrics"])
        self.assertIn("retained_wk2", p["metrics"])

    def test_payload_keys_are_exactly_the_known_columns(self) -> None:
        """No column may appear in the share that is not in the whitelist."""
        p = ul.report_payload(now=self.now, store=self.store)
        allowed = set(USAGE_COUNTER_COLUMNS) | {"day", "install_id",
                                                "version", "os"}
        for row in p["days"]:
            self.assertEqual(set(row) - allowed, set())

    def test_payload_values_are_numbers_and_enum_ish_strings(self) -> None:
        p = ul.report_payload(now=self.now, store=self.store)
        import platform
        for row in p["days"]:
            for col in USAGE_COUNTER_COLUMNS:
                self.assertIsInstance(row[col], int)
            self.assertEqual(row["os"], platform.system())
            self.assertEqual(row["version"], __version__)
            self.assertRegex(row["day"], r"^\d{4}-\d{2}-\d{2}$")

    def test_preview_and_ping_send_the_same_bytes(self) -> None:
        """The payload shown before consent IS the payload that would be sent."""
        shown = ul.redacted_report_json(now=self.now, store=self.store)
        sent: list[bytes] = []
        ul.set_ping_consent(True)
        with patch.dict(os.environ, {"QUILL_USAGE_PING_URL": "https://x/i"},
                        clear=False):
            ul.maybe_ping(now=self.now, store=self.store, force=True,
                          transport=lambda url, body: sent.append(body))
        self.assertEqual(sent[0].decode("utf-8"), shown)


class RedactionTests(_Base):
    def test_redact_is_a_no_op_on_a_compliant_payload(self) -> None:
        """Defense in depth, not a fix: on a clean payload it changes nothing.

        If this ever starts differing, a counter has regressed into storing
        content and the ledger — not the redactor — is the bug.
        """
        raw = json.dumps(ul.report_payload(now=self.now, store=self.store),
                         indent=2, sort_keys=True)
        self.assertEqual(crash_report._redact(raw), raw)
        self.assertEqual(ul.redacted_report_json(now=self.now, store=self.store),
                         raw)

    def test_redact_would_still_scrub_a_regressed_payload(self) -> None:
        leaked = json.dumps({"note": "sk-ant-api03-SECRETKEYVALUEHERE0001",
                             "line": "spouse therapy appointment"}, indent=2)
        out = crash_report._redact(leaked)
        self.assertNotIn("SECRETKEYVALUEHERE", out)
        self.assertIn("[redacted personal-class line]", out)

    def test_written_report_is_clean_and_parses(self) -> None:
        out = ul.write_report(now=self.now, store=self.store)
        self.assertTrue(out["ok"])
        path = Path(out["path"])
        self.assertTrue(path.is_file())
        self.assertIn(ul.install_id(), path.name)
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)   # still valid JSON after redaction
        self.assertEqual(payload["schema"], "mnemos.usage/1")
        self.assertNotIn("REDACTED", text)


class PingGateTests(_Base):
    def test_no_ping_without_a_url(self) -> None:
        ul.set_ping_consent(True)
        calls: list = []
        out = ul.maybe_ping(now=self.now, store=self.store,
                            transport=lambda u, b: calls.append(u))
        self.assertEqual(out["reason"], "no_url")
        self.assertEqual(calls, [])

    def test_no_ping_without_consent(self) -> None:
        calls: list = []
        with patch.dict(os.environ, {"QUILL_USAGE_PING_URL": "https://x/i"},
                        clear=False):
            out = ul.maybe_ping(now=self.now, store=self.store,
                                transport=lambda u, b: calls.append(u))
        self.assertEqual(out["reason"], "no_consent")
        self.assertEqual(calls, [])

    def test_zero_requests_are_made_by_default(self) -> None:
        """The default install must not open a socket for this, ever."""
        with patch("app.services.usage_ledger._post") as post:
            for _ in range(5):
                ul.maybe_ping(now=self.now, store=self.store)
            self.assertFalse(ul.ping_status()["will_ping"])
            post.assert_not_called()

    def test_ping_fires_only_with_both_url_and_consent(self) -> None:
        calls: list = []
        ul.set_ping_consent(True)
        with patch.dict(os.environ, {"QUILL_USAGE_PING_URL": "https://x/i"},
                        clear=False):
            self.assertTrue(ul.ping_status()["will_ping"])
            out = ul.maybe_ping(now=self.now, store=self.store,
                                transport=lambda u, b: calls.append(u))
        self.assertTrue(out["sent"])
        self.assertEqual(calls, ["https://x/i"])

    def test_withdrawn_consent_stops_the_ping(self) -> None:
        ul.set_ping_consent(True)
        ul.set_ping_consent(False)
        calls: list = []
        with patch.dict(os.environ, {"QUILL_USAGE_PING_URL": "https://x/i"},
                        clear=False):
            out = ul.maybe_ping(now=self.now, store=self.store,
                                transport=lambda u, b: calls.append(u))
        self.assertEqual(out["reason"], "no_consent")
        self.assertEqual(calls, [])


class PingCadenceTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        ul.set_ping_consent(True, now=self.now)
        self.envp = patch.dict(os.environ,
                               {"QUILL_USAGE_PING_URL": "https://x/i"},
                               clear=False)
        self.envp.start()
        self.calls: list = []

    def tearDown(self) -> None:
        self.envp.stop()
        super().tearDown()

    def _ping(self, now: float, fail: bool = False):
        def transport(url, body):
            self.calls.append(now)
            if fail:
                raise OSError("network unreachable")
        return ul.maybe_ping(now=now, store=self.store, transport=transport)

    def test_weekly_not_daily(self) -> None:
        self.assertTrue(self._ping(self.now)["sent"])
        self.assertEqual(self._ping(self.now + 86400 * 3)["reason"], "not_due")
        self.assertEqual(self._ping(self.now + 86400 * 6.9)["reason"], "not_due")
        self.assertTrue(self._ping(self.now + 86400 * 7)["sent"])
        self.assertEqual(len(self.calls), 2)

    def test_failure_is_not_retried_more_than_once_a_day(self) -> None:
        out = self._ping(self.now, fail=True)
        self.assertFalse(out["sent"])
        self.assertEqual(out["reason"], "error")
        self.assertEqual(self._ping(self.now + 3600, fail=True)["reason"],
                         "not_due")
        self.assertEqual(self._ping(self.now + 86400 * 0.9, fail=True)["reason"],
                         "not_due")
        self.assertFalse(self._ping(self.now + 86400, fail=True)["sent"])
        self.assertEqual(len(self.calls), 2)   # one attempt per day, no more

    def test_failure_never_raises_into_the_caller(self) -> None:
        for exc in (OSError("dns"), TimeoutError(), RuntimeError("HTTP 500")):
            def boom(url, body, e=exc):
                raise e
            out = ul.maybe_ping(now=self.now, store=self.store,
                                transport=boom, force=True)
            self.assertFalse(out["ok"])
        self.assertIsNotNone(ul.ping_consent()["last_error"])

    def test_flush_timer_tick_swallows_a_ping_failure(self) -> None:
        """The ping rides the flush thread; a dead endpoint must not stop it."""
        store = self.store
        with patch("app.services.usage_ledger._post",
                   side_effect=OSError("network unreachable")), \
                patch.object(UsageLedger, "_store", lambda *_a, **_k: store):
            self.led.bump("searches")
            self.led._tick()          # flush + maybe_ping, must not raise
            self.led.stop()
        # The counts still landed even though the ping blew up.
        total = sum(r["searches"] for r in store.list_usage_daily())
        self.assertEqual(total, 5)


class RouteTests(_Base):
    """The HTTP surface reads and writes this machine only."""

    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        # No lifespan: these routes need no startup, and booting capture
        # threads for a JSON assertion is exactly the kind of slow test the
        # house rules push back on.
        client = TestClient(app)
        client.get("/auth/status")   # seeds the CSRF cookie
        return client

    def _post(self, client, path, **kw):
        from app.services import api_auth
        headers = {"X-CSRF-Token": client.cookies.get(api_auth.CSRF_COOKIE) or ""}
        return client.post(path, headers=headers, **kw)

    def test_routes_read_and_write_only_locally(self) -> None:
        store = self.store
        with patch("app.services.usage_ledger._post") as post, \
                patch("app.services.usage_ledger.usage", self.led), \
                patch.object(UsageLedger, "_store", lambda *_a, **_k: store):
            client = self._client()

            stats = client.get("/usage/stats").json()
            self.assertTrue(stats["ok"])
            self.assertIn("metrics", stats)
            self.assertFalse(stats["ping"]["will_ping"])

            preview = client.get("/usage/preview").json()
            self.assertEqual(preview["payload"]["schema"], "mnemos.usage/1")
            self.assertEqual(preview["text"],
                             ul.redacted_report_json(store=store))

            rep = self._post(client, "/usage/report").json()
            self.assertTrue(rep["ok"])
            self.assertTrue(Path(rep["path"]).is_file())

            on = self._post(client, "/usage/ping/consent",
                            json={"consented": True}).json()
            self.assertTrue(on["consented"])
            self.assertFalse(on["will_ping"])   # still no URL configured

            off = self._post(client, "/usage/ping/consent",
                             json={"consented": False}).json()
            self.assertFalse(off["consented"])
            # Not one request left the box across all of that.
            post.assert_not_called()

    def test_console_request_marks_the_minute_active(self) -> None:
        """The active-minute middleware fires on a Console request."""
        store = self.store
        with patch("app.services.usage_ledger.usage", self.led), \
                patch.object(UsageLedger, "_store", lambda *_a, **_k: store):
            before = self.led.pending()["minutes"]
            self._client().get("/usage/stats")     # not an /active/ prefix
            self.assertEqual(self.led.pending()["minutes"], before)
            self._client().get("/console/jobs")
            self.assertNotEqual(self.led.pending()["minutes"], before)


if __name__ == "__main__":
    unittest.main()
