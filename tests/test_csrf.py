"""Plan 6.4 — CSRF: cross-origin POST rejected; same-origin / token OK."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import api_auth  # noqa: E402


class CsrfUnitTests(unittest.TestCase):
    def test_origin_ok_matches_host(self):
        req = mock.Mock()
        req.headers = {
            "host": "192.168.1.10:8000",
            "origin": "http://192.168.1.10:8000",
        }
        self.assertTrue(api_auth.origin_ok(req))
        req.headers["origin"] = "https://evil.example"
        self.assertFalse(api_auth.origin_ok(req))

    def test_referer_fallback(self):
        req = mock.Mock()
        req.headers = {
            "host": "127.0.0.1:8000",
            "referer": "http://127.0.0.1:8000/chat",
        }
        self.assertTrue(api_auth.origin_ok(req))

    def test_null_origin_rejected(self):
        req = mock.Mock()
        req.headers = {"host": "127.0.0.1:8000", "origin": "null"}
        self.assertFalse(api_auth.origin_ok(req))

    def test_csrf_header_double_submit(self):
        req = mock.Mock()
        req.cookies = {api_auth.CSRF_COOKIE: "abc123"}
        req.headers = {api_auth.CSRF_HEADER: "abc123"}
        self.assertTrue(api_auth.csrf_header_ok(req))
        req.headers = {api_auth.CSRF_HEADER: "wrong"}
        self.assertFalse(api_auth.csrf_header_ok(req))

    def test_bearer_skips_csrf(self):
        token = "lan-token-csrf"
        req = mock.Mock()
        req.method = "POST"
        req.url = mock.Mock(path="/facts/1/approve")
        req.headers = {"authorization": f"Bearer {token}"}
        req.cookies = {}
        with mock.patch.object(api_auth, "get_api_token", return_value=token):
            self.assertFalse(api_auth.csrf_applies(req))


class CsrfIntegrationTests(unittest.TestCase):
    def test_cross_origin_post_rejected(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        # Ensure CSRF is on
        with mock.patch.object(api_auth, "csrf_enabled", return_value=True):
            r = client.post(
                "/ui/hold-tip",
                json={"seen": True},
                headers={"Origin": "https://evil.example"},
            )
        self.assertEqual(r.status_code, 403)
        self.assertIn("CSRF", (r.json() or {}).get("detail", ""))

    def test_same_origin_post_allowed(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        # Seed CSRF cookie via GET
        client.get("/auth/status")
        csrf = client.cookies.get(api_auth.CSRF_COOKIE)
        self.assertTrue(csrf)
        r = client.post(
            "/ui/hold-tip",
            json={"seen": True},
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(r.status_code, 200)

    def test_csrf_header_alone_allows_when_origin_missing(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        client.get("/auth/status")
        csrf = client.cookies.get(api_auth.CSRF_COOKIE)
        r = client.post(
            "/ui/hold-tip",
            json={"seen": True},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(r.status_code, 200)

    def test_unlock_exempt_then_sets_csrf(self):
        from fastapi.testclient import TestClient
        from app.main import app

        token = "csrf-unlock-token"
        client = TestClient(app)
        with mock.patch.object(api_auth, "bind_is_loopback", return_value=False), \
             mock.patch.object(api_auth, "get_api_token", return_value=token), \
             mock.patch.object(api_auth, "client_is_loopback", return_value=False), \
             mock.patch.object(api_auth, "get_session_salt", return_value="salt"):
            # Cross-origin unlock still allowed (exempt) so phone can unlock
            # from the auth page itself — Origin will match when real.
            r = client.post(
                "/auth/unlock",
                json={"token": token},
                headers={"Origin": "https://evil.example"},
            )
            self.assertEqual(r.status_code, 200)
            self.assertTrue(client.cookies.get(api_auth.CSRF_COOKIE))


if __name__ == "__main__":
    unittest.main()
