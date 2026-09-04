"""Owner account — password sign-in, server-side sessions, auth routes."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import account, api_auth  # noqa: E402


class _AccountDirMixin:
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        d = Path(self.td.name)
        self._patches = [
            mock.patch.object(account, "account_path",
                              return_value=d / ".account.json"),
            mock.patch.object(account, "sessions_path",
                              return_value=d / ".web_sessions.json"),
        ]
        for p in self._patches:
            p.start()
        account.reset_throttle()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.td.cleanup()


class AccountUnitTests(_AccountDirMixin, unittest.TestCase):
    def test_create_verify_and_single_account(self):
        self.assertFalse(account.exists())
        self.assertTrue(account.create("hunter22pass",
                                       email="A@Example.com")["ok"])
        self.assertTrue(account.exists())
        self.assertEqual(account.account_email(), "a@example.com")
        self.assertTrue(account.verify_password("hunter22pass"))
        self.assertFalse(account.verify_password("wrong"))
        # No second account; no plaintext on disk.
        self.assertFalse(account.create("otherpassword")["ok"])
        raw = account.account_path().read_text(encoding="utf-8")
        self.assertNotIn("hunter22pass", raw)

    def test_short_password_refused(self):
        self.assertFalse(account.create("short")["ok"])
        self.assertFalse(account.exists())

    def test_sessions_lifecycle(self):
        tok = account.new_session(remember=True)
        self.assertTrue(account.session_valid(tok))
        self.assertFalse(account.session_valid("s_forged"))
        # Store holds only hashes.
        raw = account.sessions_path().read_text(encoding="utf-8")
        self.assertNotIn(tok, raw)
        account.revoke_session(tok)
        self.assertFalse(account.session_valid(tok))
        t2 = account.new_session(remember=False)
        account.revoke_all_sessions()
        self.assertFalse(account.session_valid(t2))

    def test_session_expiry(self):
        tok = account.new_session(remember=False)
        with mock.patch("app.services.account.time.time",
                        return_value=__import__("time").time()
                        + account.SESSION_TTL_SHORT_S + 5):
            self.assertFalse(account.session_valid(tok))

    def test_api_auth_session_matches_account_session(self):
        tok = account.new_session()
        self.assertTrue(api_auth.session_matches(tok))
        self.assertFalse(api_auth.session_matches("s_nope"))

    def test_throttle(self):
        for _ in range(account._FAIL_MAX):
            account.record_failure("1.2.3.4")
        self.assertFalse(account.throttle_ok("1.2.3.4"))
        self.assertTrue(account.throttle_ok("5.6.7.8"))


class AccountRouteTests(_AccountDirMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)
        cls.client.get("/auth/status")  # prime the CSRF cookie

    def _post(self, url, json=None, **kw):
        csrf = self.client.get("/auth/status").json()["csrf_token"]
        return self.client.post(url, json=json or {},
                                headers={"x-csrf-token": csrf}, **kw)

    def test_register_login_logout_flow(self):
        r = self.client.get("/auth/account")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["configured"])

        # TestClient peers as loopback -> register allowed (bootstrap).
        r = self._post("/auth/register",
                             json={"password": "webpassword1"})
        self.assertEqual(r.status_code, 200, r.text)
        cookie = r.cookies.get(api_auth.COOKIE_NAME)
        self.assertTrue(cookie and cookie.startswith("s_"))
        self.assertTrue(account.session_valid(cookie))

        self.assertTrue(self.client.get("/auth/account").json()["configured"])

        # Second register refused.
        r = self._post("/auth/register",
                             json={"password": "webpassword2"})
        self.assertEqual(r.status_code, 400)

        # Wrong password 401; right password sets a fresh revocable session.
        r = self.client.post("/auth/login", json={"password": "nope-nope"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/auth/login",
                             json={"password": "webpassword1",
                                   "remember": False})
        self.assertEqual(r.status_code, 200)
        tok = r.cookies.get(api_auth.COOKIE_NAME)
        self.assertTrue(account.session_valid(tok))

        # Logout revokes server-side.
        r = self._post("/auth/logout",
                             cookies={api_auth.COOKIE_NAME: tok})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(account.session_valid(tok))

    def test_login_without_account_404(self):
        r = self.client.post("/auth/login", json={"password": "whatever1"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
