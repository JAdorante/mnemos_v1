"""WS-D Tier 1 — invite-code onboarding.

The point of this tier is that it removes a funnel stop *without* changing what
Mnemos is: the vended key lands in the tester's own credentials file and is
used exactly like a pasted one. So the tests check both halves — redemption
works and fails legibly, and the bring-your-own-key path is untouched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import invite
from app.services.invite import InviteError

_SPEC = importlib.util.spec_from_file_location(
    "invite_service", Path(__file__).resolve().parent.parent
    / "scripts" / "invite_service.py")
svc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(svc)


class CodeShapeTests(unittest.TestCase):
    def test_normalize_accepts_what_a_human_types(self) -> None:
        for typed in ("ABCD-EFGH-JKLM", "abcd-efgh-jklm", "abcdefghjklm",
                      "ABCD EFGH JKLM", " abcd-EFGH jklm "):
            self.assertEqual(invite.normalize_code(typed), "ABCD-EFGH-JKLM")

    def test_bad_shapes_are_rejected_locally(self) -> None:
        """A typo costs no round trip and no confusing server error."""
        for bad in ("", "ABCD", "ABCD-EFGH-JKLM-NOPQ", "abc!-efgh-jklm",
                    "ABCDEFGHJKLMN"):
            self.assertFalse(invite.code_looks_valid(bad))

    def test_minted_codes_are_valid_and_unambiguous(self) -> None:
        codes = {svc.mint_code() for _ in range(200)}
        self.assertEqual(len(codes), 200)          # no collisions in practice
        for c in codes:
            self.assertTrue(invite.code_looks_valid(c))
            # No characters that get misheard on a phone call.
            self.assertNotRegex(c.replace("-", ""), r"[OI01]")


class RedemptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.url = "https://invites.invalid/invite/redeem"

    def test_happy_path_returns_provider_and_key(self) -> None:
        calls = []

        def transport(url, payload, timeout):
            calls.append((url, payload))
            return {"provider": "anthropic", "key": "sk-ant-vended-0001",
                    "label": "Dana"}

        out = invite.redeem("abcd efgh jklm", url=self.url, transport=transport)
        self.assertEqual(out["key"], "sk-ant-vended-0001")
        self.assertEqual(out["provider"], "anthropic")
        # The code is normalized before it goes out.
        self.assertEqual(calls[0][1], {"code": "ABCD-EFGH-JKLM"})

    def test_invalid_code_never_reaches_the_network(self) -> None:
        calls = []
        with self.assertRaises(InviteError) as ctx:
            invite.redeem("nope", url=self.url,
                          transport=lambda *a: calls.append(a))
        self.assertEqual(calls, [])
        self.assertIn("12 characters", str(ctx.exception))

    def test_no_configured_service_points_at_the_byo_path(self) -> None:
        with patch.dict(os.environ, {"QUILL_INVITE_URL": ""}, clear=False):
            with self.assertRaises(InviteError) as ctx:
                invite.redeem("ABCD-EFGH-JKLM")
        self.assertIn("paste your own API key", str(ctx.exception))

    def test_service_refusals_surface_the_service_message(self) -> None:
        def transport(url, payload, timeout):
            raise InviteError("That invite code has already been used.")
        with self.assertRaises(InviteError) as ctx:
            invite.redeem("ABCD-EFGH-JKLM", url=self.url, transport=transport)
        self.assertIn("already been used", str(ctx.exception))

    def test_unreachable_service_says_so_and_offers_the_fallback(self) -> None:
        def transport(url, payload, timeout):
            raise OSError("Name or service not known")
        with self.assertRaises(InviteError) as ctx:
            invite.redeem("ABCD-EFGH-JKLM", url=self.url, transport=transport)
        msg = str(ctx.exception)
        self.assertIn("Could not reach", msg)
        self.assertIn("paste your own API key", msg)

    def test_empty_or_unknown_provider_is_refused(self) -> None:
        with self.assertRaises(InviteError):
            invite.redeem("ABCD-EFGH-JKLM", url=self.url,
                          transport=lambda *a: {"provider": "anthropic",
                                                "key": "  "})
        with self.assertRaises(InviteError) as ctx:
            invite.redeem("ABCD-EFGH-JKLM", url=self.url,
                          transport=lambda *a: {"provider": "skynet",
                                                "key": "sk-x"})
        self.assertIn("unknown provider", str(ctx.exception))

    def test_http_reasons_are_written_for_a_non_engineer(self) -> None:
        for code in (400, 404, 409, 410, 429):
            msg = invite._http_reason(code)
            self.assertNotIn("HTTP", msg)
            self.assertTrue(msg[0].isupper())
        self.assertIn("HTTP 503", invite._http_reason(503))


class PersistenceTests(unittest.TestCase):
    """The vended key must land exactly where a pasted key lands."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_inv_"))
        self.cred = self.tmp / ".credentials.env"
        self.patcher = patch("app.services.icloud_account._cred_path",
                             lambda: self.cred)
        self.patcher.start()
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.env.stop()

    def test_redeem_and_save_writes_credentials_env(self) -> None:
        out = invite.redeem_and_save(
            "ABCD-EFGH-JKLM", url="https://x/i",
            transport=lambda *a: {"provider": "anthropic",
                                  "key": "sk-ant-vended-0002"})
        self.assertTrue(out["ok"])
        text = self.cred.read_text(encoding="utf-8")
        self.assertIn("ANTHROPIC_API_KEY=sk-ant-vended-0002", text)
        self.assertIn("QUILL_PARENT_PROVIDER=anthropic", text)
        # And it is live in-process, same as the paste path.
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-vended-0002")

    def test_a_vended_key_is_indistinguishable_downstream(self) -> None:
        """Nothing in the app knows or cares which path the key came from."""
        from app.services import parent_model
        invite.redeem_and_save(
            "ABCD-EFGH-JKLM", url="https://x/i",
            transport=lambda *a: {"provider": "anthropic", "key": "sk-ant-v3"})
        vended = self.cred.read_text(encoding="utf-8")
        self.cred.unlink()
        parent_model.save("anthropic", "sk-ant-v3")   # the BYO path
        self.assertEqual(self.cred.read_text(encoding="utf-8"), vended)

    def test_a_non_anthropic_provider_lands_in_its_own_var(self) -> None:
        invite.redeem_and_save(
            "ABCD-EFGH-JKLM", url="https://x/i",
            transport=lambda *a: {"provider": "openai", "key": "sk-openai-1"})
        text = self.cred.read_text(encoding="utf-8")
        self.assertIn("OPENAI_API_KEY=sk-openai-1", text)
        self.assertIn("QUILL_PARENT_PROVIDER=openai", text)


class VendingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Path(tempfile.mkdtemp(prefix="quill_invsvc_")) / "invites.json"
        self.env = patch.dict(os.environ, {"QUILL_INVITE_DB": str(self.db)},
                              clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def _mint(self, **kw) -> str:
        code = svc.mint_code()
        data = svc.load_db()
        data[code] = {"provider": "anthropic", "key": "sk-ant-pre-made",
                      "label": "Dana", "expires_at": time.time() + 86400.0,
                      **kw}
        svc.save_db(data)
        return code

    def test_happy_redemption(self) -> None:
        code = self._mint()
        status, body = svc.redeem(code)
        self.assertEqual(status, 200)
        self.assertEqual(body["key"], "sk-ant-pre-made")
        self.assertEqual(body["label"], "Dana")

    def test_a_code_works_exactly_once(self) -> None:
        code = self._mint()
        self.assertEqual(svc.redeem(code)[0], 200)
        status, body = svc.redeem(code)
        self.assertEqual(status, 409)
        self.assertIn("already been used", body["detail"])

    def test_expired_code_is_refused(self) -> None:
        code = self._mint(expires_at=time.time() - 1)
        status, body = svc.redeem(code)
        self.assertEqual(status, 410)
        self.assertIn("expired", body["detail"])

    def test_unknown_code_is_refused(self) -> None:
        status, body = svc.redeem("ZZZZ-ZZZZ-ZZZZ")
        self.assertEqual(status, 404)
        self.assertIn("not found", body["detail"])

    def test_revoked_code_is_refused(self) -> None:
        code = self._mint(revoked=True)
        self.assertEqual(svc.redeem(code)[0], 410)

    def test_revocation_is_per_tester(self) -> None:
        """One tester's key can be pulled without touching anyone else's."""
        a, b = self._mint(), self._mint()
        svc.main(["revoke", a])
        self.assertEqual(svc.redeem(a)[0], 410)
        self.assertEqual(svc.redeem(b)[0], 200)

    def test_reissue_unblocks_a_reinstall(self) -> None:
        code = self._mint()
        svc.redeem(code)
        svc.main(["reissue", code])
        self.assertEqual(svc.redeem(code)[0], 200)

    def test_operator_cli_mint_and_list(self) -> None:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            svc.main(["mint", "sk-ant-cli", "--label", "Sam", "--days", "7"])
        code = buf.getvalue().strip()
        self.assertTrue(invite.code_looks_valid(code))
        buf = io.StringIO()
        with redirect_stdout(buf):
            svc.main(["list"])
        self.assertIn("open", buf.getvalue())
        self.assertIn("Sam", buf.getvalue())

    def test_end_to_end_client_to_service(self) -> None:
        """The client's redeem() against the real service logic."""
        code = self._mint()

        def transport(url, payload, timeout):
            status, body = svc.redeem(payload["code"])
            if status != 200:
                raise InviteError(body["detail"])
            return body

        out = invite.redeem(code, url="https://x/i", transport=transport)
        self.assertEqual(out["key"], "sk-ant-pre-made")
        with self.assertRaises(InviteError):
            invite.redeem(code, url="https://x/i", transport=transport)


class SetupPageTests(unittest.TestCase):
    """The Setup page must offer the code path, not just the installer.

    A tester who presses Enter past the installer prompt lands on /onboarding.
    Without this, their invite code has nowhere to go and they are back at the
    account-and-credit-card funnel stop WS-D exists to remove.
    """

    def _page(self) -> str:
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app).get("/onboarding").text

    def test_the_setup_page_carries_the_invite_affordance(self) -> None:
        page = self._page()
        for needle in ('id="inviteBox"', 'id="invitecode"', 'id="inviteBtn"',
                       "/onboarding/invite"):
            self.assertIn(needle, page)
        # Rendered, not a raw template.
        self.assertNotIn("@@BRAND@@", page)

    def test_the_invite_box_is_hidden_until_a_service_is_configured(self) -> None:
        """No invite service on this build -> the BYO form is all a user sees."""
        page = self._page()
        box = page[page.index('id="inviteBox"'):]
        self.assertIn("hidden", box[:120])
        # And it is revealed only by the probe, never unconditionally.
        self.assertIn('if(s.configured) document.getElementById("inviteBox").hidden=false;',
                      page)

    def test_the_byo_key_form_is_still_there_and_first_class(self) -> None:
        page = self._page()
        for needle in ('id="apikey"', 'id="keyBtn"', 'id="provider"',
                       "/onboarding/api-key"):
            self.assertIn(needle, page)


class ByoPathUntouchedTests(unittest.TestCase):
    def test_no_invite_url_means_the_app_behaves_exactly_as_before(self) -> None:
        with patch.dict(os.environ, {"QUILL_INVITE_URL": ""}, clear=False):
            self.assertEqual(invite.vending_url(), "")
        # And the installer only offers the invite branch when a URL exists.
        ps1 = (Path(__file__).resolve().parent.parent
               / "scripts" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("if ($inviteUrl) {", ps1)
        self.assertIn("if (-not $invited) {", ps1)
        # The original paste prompt survives verbatim inside that fallback.
        self.assertIn("Paste YOUR Anthropic API key", ps1)

    def test_routes_expose_the_path_only_when_configured(self) -> None:
        from fastapi.testclient import TestClient
        from app.main import app
        from app.services import api_auth
        client = TestClient(app)
        client.get("/auth/status")
        with patch.dict(os.environ, {"QUILL_INVITE_URL": ""}, clear=False):
            self.assertFalse(client.get("/onboarding/invite").json()["configured"])
        with patch.dict(os.environ, {"QUILL_INVITE_URL": "https://x/i"},
                        clear=False):
            self.assertTrue(client.get("/onboarding/invite").json()["configured"])
            r = client.post(
                "/onboarding/invite", json={"code": "bad"},
                headers={"X-CSRF-Token": client.cookies.get(api_auth.CSRF_COOKIE)})
            self.assertEqual(r.status_code, 400)
            self.assertIn("12 characters", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
