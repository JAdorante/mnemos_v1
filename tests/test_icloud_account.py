"""Unit tests for app.services.icloud_account — the guided iCloud connect.

Public-product contract: credentials are validated against Apple BEFORE being
stored, the credentials file is upserted (never clobbered), status masks the
identity and never exposes a secret, and disconnect blanks only our keys.
Apple's endpoint is mocked — no network, no real account.

Run with either:
    python -m unittest discover -s tests
    pytest tests/
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import icloud_account as ic  # noqa: E402


def _resp(status: int):
    r = mock.Mock()
    r.status_code = status
    return r


class IcloudAccountBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="icloud_")
        self._cred = Path(self._tmp) / "creds.env"
        os.environ["QUILL_CREDENTIALS_FILE"] = str(self._cred)

    def tearDown(self) -> None:
        os.environ.pop("QUILL_CREDENTIALS_FILE", None)
        os.environ.pop("QUILL_ICLOUD_USER", None)
        os.environ.pop("QUILL_ICLOUD_APP_PASSWORD", None)


class VerifyTests(IcloudAccountBase):
    def test_input_sanity_no_network(self) -> None:
        with mock.patch.object(ic.requests, "request") as req:
            self.assertFalse(ic.verify("not-an-email", "abcd-efgh-ijkl-mnop")["ok"])
            self.assertFalse(ic.verify("a@b.com", "short")["ok"])
            req.assert_not_called()

    def test_valid_pair_accepted(self) -> None:
        with mock.patch.object(ic.requests, "request", return_value=_resp(207)):
            self.assertTrue(ic.verify("a@b.com", "abcd-efgh-ijkl-mnop")["ok"])

    def test_rejected_pair_points_at_app_password(self) -> None:
        with mock.patch.object(ic.requests, "request", return_value=_resp(401)):
            res = ic.verify("a@b.com", "abcd-efgh-ijkl-mnop")
        self.assertFalse(res["ok"])
        self.assertIn("APP-SPECIFIC", res["error"])

    def test_network_failure_is_an_error_not_a_crash(self) -> None:
        with mock.patch.object(ic.requests, "request",
                               side_effect=ic.requests.ConnectionError()):
            res = ic.verify("a@b.com", "abcd-efgh-ijkl-mnop")
        self.assertFalse(res["ok"])
        self.assertIn("Apple", res["error"])


class ConnectPersistTests(IcloudAccountBase):
    def _connect(self) -> dict:
        with mock.patch.object(ic.requests, "request", return_value=_resp(207)):
            return ic.connect("justin@example.com", "abcd-efgh-ijkl-mnop")

    def test_connect_persists_and_masks(self) -> None:
        res = self._connect()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["user"], "j***@example.com")
        text = self._cred.read_text(encoding="utf-8")
        self.assertIn("QUILL_ICLOUD_USER=justin@example.com", text)
        self.assertIn("QUILL_ICLOUD_APP_PASSWORD=abcd-efgh-ijkl-mnop", text)
        # Applied to the live environment too — no restart needed.
        self.assertEqual(os.environ["QUILL_ICLOUD_USER"], "justin@example.com")

    def test_rejected_credentials_never_stored(self) -> None:
        with mock.patch.object(ic.requests, "request", return_value=_resp(401)):
            res = ic.connect("justin@example.com", "abcd-efgh-ijkl-mnop")
        self.assertFalse(res["ok"])
        self.assertFalse(self._cred.is_file())
        self.assertNotIn("QUILL_ICLOUD_USER", os.environ)

    def test_upsert_preserves_other_lines(self) -> None:
        self._cred.write_text("OTHER_SECRET=keepme\nQUILL_ICLOUD_USER=old@x.com\n",
                              encoding="utf-8")
        self._connect()
        text = self._cred.read_text(encoding="utf-8")
        self.assertIn("OTHER_SECRET=keepme", text)
        self.assertIn("QUILL_ICLOUD_USER=justin@example.com", text)
        self.assertNotIn("old@x.com", text)

    def test_status_never_leaks_secrets(self) -> None:
        self._connect()
        st = ic.status()
        self.assertTrue(st["connected"])
        self.assertEqual(st["user"], "j***@example.com")
        self.assertNotIn("abcd-efgh-ijkl-mnop", str(st))

    def test_disconnect_blanks_only_our_keys(self) -> None:
        self._cred.write_text("OTHER_SECRET=keepme\n", encoding="utf-8")
        self._connect()
        out = ic.disconnect()
        self.assertFalse(out["connected"])
        text = self._cred.read_text(encoding="utf-8")
        self.assertIn("OTHER_SECRET=keepme", text)
        self.assertIn("QUILL_ICLOUD_USER=\n", text + "\n")
        self.assertFalse(ic.status()["connected"])
        self.assertNotIn("QUILL_ICLOUD_USER", os.environ)


if __name__ == "__main__":
    unittest.main()
