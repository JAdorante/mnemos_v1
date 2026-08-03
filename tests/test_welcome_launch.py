"""Welcome / launch gate — new setup vs continue on this machine."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import onboarding


class LaunchStatusTests(unittest.TestCase):
    def test_new_when_incomplete_and_unknown(self) -> None:
        with patch.object(onboarding, "status", return_value={
                "completed": False, "items_ingested": 0}), \
             patch("app.services.identity.user_identity", return_value={}):
            out = onboarding.launch_status(authorized=True, lan_gate=False)
        self.assertEqual(out["mode"], "new")
        self.assertFalse(out["needs_unlock"])
        self.assertEqual(out["home_url"], "/today")
        self.assertEqual(out["onboarding_url"], "/onboarding")

    def test_returning_when_completed(self) -> None:
        with patch.object(onboarding, "status", return_value={
                "completed": True, "items_ingested": 3}), \
             patch("app.services.identity.user_identity",
                   return_value={"name": "Justin", "role": "Founder"}):
            out = onboarding.launch_status(authorized=True, lan_gate=False)
        self.assertEqual(out["mode"], "returning")
        self.assertEqual(out["user_name"], "Justin")
        self.assertFalse(out["needs_unlock"])

    def test_returning_from_name_alone(self) -> None:
        with patch.object(onboarding, "status", return_value={
                "completed": False}), \
             patch("app.services.identity.user_identity",
                   return_value={"name": "Sam"}):
            out = onboarding.launch_status()
        self.assertEqual(out["mode"], "returning")
        self.assertEqual(out["user_name"], "Sam")

    def test_needs_unlock_on_lan_when_unauthorized(self) -> None:
        with patch.object(onboarding, "status", return_value={
                "completed": True}), \
             patch("app.services.identity.user_identity",
                   return_value={"name": "Justin"}):
            out = onboarding.launch_status(authorized=False, lan_gate=True)
        self.assertTrue(out["needs_unlock"])
        self.assertTrue(out["lan_gate"])


if __name__ == "__main__":
    unittest.main()
