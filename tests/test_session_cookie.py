"""Plan 6.3 — session cookie is HMAC-derived; theft ≠ LAN token."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import api_auth  # noqa: E402


class SessionTokenUnitTests(unittest.TestCase):
    def test_session_token_differs_from_api_token(self):
        token = "lan-secret-token-abc"
        salt = "unit-test-salt"
        sess = api_auth.session_token(token, salt=salt)
        self.assertTrue(sess)
        self.assertNotEqual(sess, token)
        self.assertEqual(len(sess), 64)  # sha256 hex
        # Stable
        self.assertEqual(sess, api_auth.session_token(token, salt=salt))
        # Salt changes value
        self.assertNotEqual(sess, api_auth.session_token(token, salt="other"))

    def test_session_matches_not_raw_token(self):
        token = "lan-secret-token-xyz"
        salt = "fixed-salt"
        with mock.patch.object(api_auth, "get_api_token", return_value=token), \
             mock.patch.object(api_auth, "get_session_salt", return_value=salt):
            sess = api_auth.session_token()
            self.assertTrue(api_auth.session_matches(sess))
            self.assertFalse(api_auth.session_matches(token))
            self.assertTrue(api_auth.token_matches(token))
            self.assertFalse(api_auth.token_matches(sess))

    def test_salt_file_minted(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".session_salt"
            with mock.patch.object(api_auth, "session_salt_path",
                                   return_value=path), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("QUILL_SESSION_SALT", None)
                a = api_auth.get_session_salt()
                b = api_auth.get_session_salt()
                self.assertEqual(a, b)
                self.assertTrue(path.is_file())


class SessionCookieIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    def test_cookie_is_derived_not_raw_and_authorizes(self):
        token = "unit-test-lan-token-6-3"
        salt = "unit-session-salt-6-3"
        expected_sess = api_auth.session_token(token, salt=salt)
        with mock.patch.object(api_auth, "bind_is_loopback", return_value=False), \
             mock.patch.object(api_auth, "get_api_token", return_value=token), \
             mock.patch.object(api_auth, "get_session_salt", return_value=salt), \
             mock.patch.object(api_auth, "client_is_loopback", return_value=False):
            # Fresh client jar
            from fastapi.testclient import TestClient
            from app.main import app
            client = TestClient(app)

            denied = client.get("/memory/events")
            self.assertEqual(denied.status_code, 401)

            ok = client.post("/auth/unlock", json={"token": token})
            self.assertEqual(ok.status_code, 200)
            cookie = client.cookies.get(api_auth.COOKIE_NAME)
            self.assertEqual(cookie, expected_sess)
            self.assertNotEqual(cookie, token)

            # Cookie authorizes
            allowed = client.get("/memory/events")
            self.assertEqual(allowed.status_code, 200)

            # Stolen cookie as Bearer does NOT yield the LAN token privilege
            # (session HMAC ≠ raw token).
            stolen = client.get(
                "/credentials",
                headers={"Authorization": f"Bearer {cookie}"},
            )
            # Still has cookie in jar from unlock — clear cookies for Bearer-only
            client.cookies.clear()
            stolen = client.get(
                "/credentials",
                headers={"Authorization": f"Bearer {cookie}"},
            )
            self.assertEqual(stolen.status_code, 401)

            # Raw Bearer still works
            bearer = client.get(
                "/credentials",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(bearer.status_code, 200)

            # Raw token in cookie is rejected (legacy cookies invalid)
            client.cookies.clear()
            client.cookies.set(api_auth.COOKIE_NAME, token)
            legacy = client.get("/memory/events")
            self.assertEqual(legacy.status_code, 401)


if __name__ == "__main__":
    unittest.main()
