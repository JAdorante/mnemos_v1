"""Tests for LAN API gate, phone device-limit claim, legacy soft-merge resolve."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import api_auth  # noqa: E402
from app.services import phone_channel as pc  # noqa: E402
from app.services.resolution import Resolver  # noqa: E402
from app.storage import Store  # noqa: E402


class ApiAuthUnitTests(unittest.TestCase):
    def test_path_exemptions(self) -> None:
        self.assertTrue(api_auth.path_is_exempt("/auth/unlock", "POST"))
        self.assertTrue(api_auth.path_is_exempt("/phone/pair/claim", "POST"))
        self.assertTrue(api_auth.path_is_exempt("/phone/ingest", "POST"))
        self.assertTrue(api_auth.path_is_exempt("/phone/setup", "GET"))
        self.assertTrue(api_auth.path_is_exempt("/chat", "GET"))
        self.assertTrue(api_auth.path_is_exempt("/ui", "GET"))
        self.assertTrue(api_auth.path_is_exempt("/today", "GET"))
        self.assertTrue(api_auth.path_is_exempt("/memory", "GET"))
        self.assertFalse(api_auth.path_is_exempt("/credentials", "GET"))
        self.assertFalse(api_auth.path_is_exempt("/memory/events", "GET"))
        self.assertFalse(api_auth.path_is_exempt("/phone/pair/start", "POST"))
        # Desktop enqueue must NOT ride the phone outbox GET exemption.
        self.assertTrue(api_auth.path_is_exempt("/phone/outbox", "GET"))
        self.assertFalse(api_auth.path_is_exempt("/phone/outbox/queue", "POST"))

    def test_token_matches(self) -> None:
        with mock.patch.dict(os.environ, {"QUILL_API_TOKEN": "secret-token-xyz"}):
            self.assertTrue(api_auth.token_matches("secret-token-xyz"))
            self.assertFalse(api_auth.token_matches("wrong"))
            self.assertFalse(api_auth.token_matches(None))


class LanMiddlewareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    def test_unlock_roundtrip_under_open_bind(self) -> None:
        token = "unit-test-lan-token-please"
        with mock.patch.object(api_auth, "bind_is_loopback", return_value=False), \
             mock.patch.object(api_auth, "get_api_token", return_value=token), \
             mock.patch.object(api_auth, "client_is_loopback", return_value=False):
            denied = self.client.get("/memory/events")
            self.assertEqual(denied.status_code, 401)

            bad = self.client.post("/auth/unlock", json={"token": "nope"})
            self.assertEqual(bad.status_code, 401)

            ok = self.client.post("/auth/unlock", json={"token": token})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json().get("ok"), True)

            # Cookie from unlock authorizes subsequent API calls.
            allowed = self.client.get("/memory/events")
            self.assertEqual(allowed.status_code, 200)

            bearer = self.client.get(
                "/credentials",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(bearer.status_code, 200)


class PhoneDeviceLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="phone_lim_")
        os.environ["QUILL_PHONE_DEVICES"] = str(Path(self._tmp) / "devices.json")
        os.environ["QUILL_PHONE_OUTBOX"] = str(Path(self._tmp) / "outbox.json")
        pc._pairing = None

    def tearDown(self) -> None:
        os.environ.pop("QUILL_PHONE_DEVICES", None)
        os.environ.pop("QUILL_PHONE_OUTBOX", None)
        pc._pairing = None

    def test_limit_preserves_pairing_code(self) -> None:
        # Pre-fill registry to the configured max.
        from app.config import settings as cfg
        max_n = cfg.phone.max_devices
        filled = {
            f"dev{i:02d}": {
                "name": f"p{i}", "platform": "ios",
                "token_sha256": "x" * 64,
                "created_at": 1.0, "last_seen": None,
                "last_kind": "", "events": 0,
            }
            for i in range(max_n)
        }
        Path(os.environ["QUILL_PHONE_DEVICES"]).write_text(
            json.dumps(filled), encoding="utf-8")

        start = pc.start_pairing()
        self.assertTrue(start["ok"])
        code = start["code"]
        self.assertTrue(pc.pairing_active())

        claim = pc.claim_pairing(code, "Overflow Phone", "ios")
        self.assertFalse(claim["ok"])
        self.assertIn("device limit", claim.get("error", ""))
        # Code must still be live so the user can revoke a device and retry.
        self.assertTrue(pc.pairing_active())
        again = pc.claim_pairing(code, "Overflow Phone", "ios")
        self.assertFalse(again["ok"])
        self.assertIn("device limit", again.get("error", ""))


class LegacyResolverSoftMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_res_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.resolver = Resolver(store=self.store)

    def test_exact_follows_soft_merge_redirect(self) -> None:
        survivor = self.store.insert_person("Alex Rivera", ts=1.0)
        absorbed = self.store.insert_person("Alex R.", ts=1.0)
        self.store.soft_merge_people(survivor, absorbed, reason="dup", actor="test")
        # Exact on absorbed name should resolve to the survivor, not the dead id.
        pid = self.store.find_person_exact("Alex R.")
        self.assertEqual(pid, survivor)

    def test_fuzzy_skips_hidden_people(self) -> None:
        survivor = self.store.insert_person("Jordan Lee", ts=1.0)
        absorbed = self.store.insert_person("Jordan L", ts=1.0)
        self.store.soft_merge_people(survivor, absorbed, reason="dup", actor="test")
        with mock.patch.object(self.resolver, "_embed", return_value=None):
            pid = self.resolver.resolve_person("Jordan L", ts=2.0)
        self.assertEqual(pid, survivor)


if __name__ == "__main__":
    unittest.main()
