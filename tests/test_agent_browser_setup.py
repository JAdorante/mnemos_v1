"""Guided agent-browser connection — detection, persist/disconnect, routes."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import agent_browser_setup as abs_  # noqa: E402


class _CredMixin:
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cred = Path(self.td.name) / ".credentials.env"
        self._p = mock.patch("app.services.icloud_account._cred_path",
                             return_value=self.cred)
        self._p.start()
        self._env = {k: os.environ.pop(k, None)
                     for k in ("QUILL_AGENT_PROFILE", "QUILL_AGENT_CHANNEL")}
        abs_.reset_for_tests()

    def tearDown(self):
        self._p.stop()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.td.cleanup()


class SetupUnitTests(_CredMixin, unittest.TestCase):
    def test_status_shape(self):
        s = abs_.status()
        self.assertIn("available", s)
        self.assertFalse(s["configured"])
        ids = [c["id"] for c in s["channels"]]
        self.assertEqual(ids[0], "")  # bundled Chromium always offered
        self.assertIn("chrome", ids)
        self.assertIn("msedge", ids)
        self.assertTrue(s["channels"][0]["installed"])

    def test_persist_and_disconnect_roundtrip(self):
        self.cred.write_text("QUILL_ICLOUD_USER=x@y.z\n", encoding="utf-8")
        abs_._persist("chrome")
        raw = self.cred.read_text(encoding="utf-8")
        self.assertIn("QUILL_AGENT_PROFILE=main", raw)
        self.assertIn("QUILL_AGENT_CHANNEL=chrome", raw)
        self.assertIn("QUILL_ICLOUD_USER=x@y.z", raw)  # other creds untouched
        self.assertEqual(os.environ.get("QUILL_AGENT_PROFILE"), "main")
        self.assertTrue(abs_.status()["configured"])

        # Re-persist with no channel drops the channel line.
        abs_._persist("")
        raw = self.cred.read_text(encoding="utf-8")
        self.assertNotIn("QUILL_AGENT_CHANNEL", raw)
        self.assertNotIn("QUILL_AGENT_CHANNEL", os.environ)

        out = abs_.disconnect()
        self.assertTrue(out["ok"])
        raw = self.cred.read_text(encoding="utf-8")
        self.assertNotIn("QUILL_AGENT_PROFILE", raw)
        self.assertIn("QUILL_ICLOUD_USER=x@y.z", raw)
        self.assertNotIn("QUILL_AGENT_PROFILE", os.environ)
        self.assertFalse(abs_.status()["configured"])

    def test_start_signin_rejects_bad_channel(self):
        out = abs_.start_signin("firefox")
        self.assertFalse(out["ok"])

    def test_start_signin_refused_headless(self):
        with mock.patch.object(abs_, "headed_available", return_value=False):
            out = abs_.start_signin("")
        self.assertFalse(out["ok"])
        self.assertIn("display", out["error"])

    def test_start_signin_refused_missing_browser(self):
        with mock.patch.object(abs_, "_installed", return_value=False):
            out = abs_.start_signin("chrome")
        self.assertFalse(out["ok"])

    def test_start_signin_single_flight(self):
        with mock.patch.object(abs_.threading, "Thread") as th, \
             mock.patch.object(abs_, "headed_available", return_value=True):
            first = abs_.start_signin("")
            second = abs_.start_signin("")
        self.assertTrue(first["ok"])
        self.assertTrue(second.get("already"))
        self.assertEqual(th.call_count, 1)

    def test_default_channel_never_raises(self):
        self.assertIn(abs_.default_channel(), ("", "chrome", "msedge"))


class SetupRouteTests(_CredMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)
        cls.client.get("/auth/status")  # prime the CSRF cookie

    def _post(self, url, json=None):
        csrf = self.client.get("/auth/status").json()["csrf_token"]
        return self.client.post(url, json=json or {},
                                headers={"x-csrf-token": csrf})

    def test_status_route(self):
        r = self.client.get("/agent/browser/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("channels", r.json())

    def test_signin_route_rejects_bad_channel(self):
        r = self._post("/agent/browser/signin/start",
                       json={"channel": "netscape"})
        self.assertEqual(r.status_code, 400)

    def test_disconnect_route(self):
        r = self._post("/agent/browser/disconnect")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])


if __name__ == "__main__":
    unittest.main()
