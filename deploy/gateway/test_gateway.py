"""Gateway tests — run from deploy/gateway:  python3 -m unittest test_gateway -v

Spins a REAL upstream HTTP server and points a fake seat at it, so the proxy
path (Bearer injection, multi Set-Cookie, streaming, auth redirects) is
exercised end to end rather than mocked away.
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest

_TMP = tempfile.mkdtemp(prefix="gw-test-")
os.environ["GATEWAY_DATA_DIR"] = _TMP
os.environ["COOKIE_SECURE"] = "0"          # TestClient speaks http://

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import gateway
import provision
import store

# --- fake seat -------------------------------------------------------------
seat_app = FastAPI()


@seat_app.get("/health")
def _health():
    return {"ok": True}


@seat_app.get("/whoami")
def _whoami(request: Request):
    return {"auth": request.headers.get("authorization", ""),
            "cookie": request.headers.get("cookie", "")}


@seat_app.get("/setcookies")
def _setcookies():
    r = JSONResponse({"ok": True})
    r.set_cookie("quill_api_session", "sess-value")
    r.set_cookie("quill_csrf", "csrf-value")
    return r


@seat_app.get("/echoquery")
def _echoquery(request: Request):
    return {"q": request.url.query}


class _Server(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        pass


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(uvicorn.Config(
            seat_app, host="127.0.0.1", port=8791, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        while not cls.server.started:
            pass
        # Every seat resolves to the fake upstream; no Docker in tests.
        provision.seat_url = lambda seat: "http://127.0.0.1:8791"
        provision.seat_running = lambda seat: True
        provision.ensure_running = lambda seat: None
        provision.create_seat = lambda: ("seat-test", "upstream-token-abc")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def setUp(self):
        for p in (store.users_path(), store.sessions_path()):
            if p.is_file():
                p.unlink()
        store._failures.clear()
        # Each TestClient runs its own event loop; the cached upstream client
        # would otherwise stay bound to the previous (now closed) one.
        gateway._http = None
        self.c = TestClient(gateway.app)

    def _signup(self, email="a@b.com", password="correct-horse-battery"):
        return self.c.post("/api/signup",
                           json={"email": email, "password": password})

    # -- signup ------------------------------------------------------------
    def test_signup_creates_account_and_session(self):
        r = self._signup()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(store.get_user("a@b.com"))
        self.assertIn(gateway.COOKIE_NAME, self.c.cookies)

    def test_signup_rejects_short_password(self):
        r = self.c.post("/api/signup", json={"email": "a@b.com", "password": "short"})
        self.assertEqual(r.status_code, 400)

    def test_signup_rejects_bad_email(self):
        r = self.c.post("/api/signup", json={"email": "nope", "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 400)

    def test_signup_rejects_duplicate_email(self):
        self._signup()
        r = self._signup()
        self.assertEqual(r.status_code, 409)

    def test_signup_honors_invite_code(self):
        gateway.INVITE_CODE = "letmein"
        try:
            bad = self.c.post("/api/signup", json={
                "email": "c@d.com", "password": "correct-horse-battery",
                "invite": "wrong"})
            self.assertEqual(bad.status_code, 403)
            ok = self.c.post("/api/signup", json={
                "email": "c@d.com", "password": "correct-horse-battery",
                "invite": "letmein"})
            self.assertEqual(ok.status_code, 200, ok.text)
        finally:
            gateway.INVITE_CODE = ""

    # -- signin ------------------------------------------------------------
    def test_signin_roundtrip(self):
        self._signup()
        self.c.post("/api/signout")
        r = self.c.post("/api/signin", json={
            "email": "a@b.com", "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_signin_wrong_password_is_401(self):
        self._signup()
        self.c.post("/api/signout")
        r = self.c.post("/api/signin", json={"email": "a@b.com", "password": "nope-nope-nope"})
        self.assertEqual(r.status_code, 401)

    def test_signin_does_not_leak_account_existence(self):
        self._signup()
        self.c.post("/api/signout")
        missing = self.c.post("/api/signin", json={
            "email": "ghost@b.com", "password": "nope-nope-nope"})
        wrong = self.c.post("/api/signin", json={
            "email": "a@b.com", "password": "nope-nope-nope"})
        self.assertEqual(missing.status_code, wrong.status_code)
        self.assertEqual(missing.json()["detail"], wrong.json()["detail"])

    def test_signin_throttles_after_repeated_failures(self):
        self._signup()
        self.c.post("/api/signout")
        codes = [self.c.post("/api/signin", json={
            "email": "a@b.com", "password": "bad-bad-bad-bad"}).status_code
            for _ in range(store.MAX_FAILURES + 2)]
        self.assertIn(429, codes)

    def test_signout_revokes_the_session(self):
        self._signup()
        self.c.post("/api/signout")
        r = self.c.get("/whoami", follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    # -- proxy -------------------------------------------------------------
    def test_anonymous_get_redirects_to_signin_with_next(self):
        r = self.c.get("/capture", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/signin?next=/capture", r.headers["location"])

    def test_anonymous_post_is_401_not_a_redirect(self):
        r = self.c.post("/some/api", json={})
        self.assertEqual(r.status_code, 401)

    def test_proxy_injects_the_seat_token(self):
        self._signup()
        r = self.c.get("/whoami")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["auth"], "Bearer upstream-token-abc")

    def test_proxy_overrides_client_supplied_authorization(self):
        """A user must not be able to present a token of their own choosing."""
        self._signup()
        r = self.c.get("/whoami", headers={"Authorization": "Bearer attacker"})
        self.assertEqual(r.json()["auth"], "Bearer upstream-token-abc")

    def test_gateway_cookie_is_stripped_from_upstream(self):
        self._signup()
        r = self.c.get("/whoami")
        self.assertNotIn(gateway.COOKIE_NAME, r.json()["cookie"])

    def test_both_upstream_cookies_survive(self):
        """Regression: a dict of headers would keep only the last Set-Cookie."""
        self._signup()
        r = self.c.get("/setcookies")
        raw = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
        joined = " ".join(raw)
        self.assertIn("quill_api_session=", joined)
        self.assertIn("quill_csrf=", joined)

    def test_repeated_query_params_are_preserved(self):
        self._signup()
        r = self.c.get("/echoquery?tag=a&tag=b")
        self.assertEqual(r.json()["q"], "tag=a&tag=b")

    def test_cross_origin_post_is_rejected(self):
        self._signup()
        r = self.c.post("/some/api", json={},
                        headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_same_origin_post_is_allowed_through(self):
        self._signup()
        r = self.c.post("/whoami", headers={"Origin": "http://testserver"})
        self.assertNotEqual(r.status_code, 403)

    # -- seat status -------------------------------------------------------
    def test_seat_status_requires_a_session(self):
        self.assertEqual(self.c.get("/api/seat/status").status_code, 401)

    def test_seat_status_ready_when_upstream_healthy(self):
        self._signup()
        self.assertTrue(self.c.get("/api/seat/status").json()["ready"])

    # -- isolation ---------------------------------------------------------
    def test_two_users_get_distinct_seats_and_tokens(self):
        seats = iter([("seat-one", "tok-one"), ("seat-two", "tok-two")])
        provision.create_seat = lambda: next(seats)
        self._signup("one@x.com")
        c2 = TestClient(gateway.app)
        c2.post("/api/signup", json={"email": "two@x.com",
                                     "password": "correct-horse-battery"})
        u1, u2 = store.get_user("one@x.com"), store.get_user("two@x.com")
        self.assertNotEqual(u1["seat"], u2["seat"])
        self.assertNotEqual(u1["token"], u2["token"])
        provision.create_seat = lambda: ("seat-test", "upstream-token-abc")

    def test_session_token_is_not_stored_in_plaintext(self):
        self._signup()
        cookie = self.c.cookies[gateway.COOKIE_NAME]
        self.assertNotIn(cookie, store.sessions_path().read_text())

    def test_password_is_not_stored_in_plaintext(self):
        self._signup()
        self.assertNotIn("correct-horse-battery", store.users_path().read_text())


if __name__ == "__main__":
    unittest.main()
