"""Identity grounding — the assistant knows what it is, and who the user is.

Covers both resolution layers (profile sheet, approved-claim fallback), the
honest "not known yet" state, the grounding-section rendering, that compose()
puts identity first, and — the generality rule — that the code carries no
hardcoded user and describes whoever's install it runs on.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import identity


class AssistantIdentityTests(unittest.TestCase):
    def test_assistant_is_vinceo(self):
        a = identity.assistant_identity()
        self.assertEqual(a["name"], "vinceo.ai")
        self.assertIn("memory assistant", a["role"])
        self.assertIn("vinceo.ai", a["summary"])


class UserFromProfileTests(unittest.TestCase):
    def _with_profile(self, prof):
        return mock.patch.object(identity, "_user_from_profile",
                                 return_value=prof)

    def test_reads_name_role_description(self):
        prof = {"name": "Dana Okonkwo", "role": "Marine biologist",
                "description": "Coral restoration.",
                "primary_email": "dana@lab.org",
                "secondary_email": "dana.ok@gmail.com",
                "phone": "+1 555 0100"}
        with mock.patch("app.services.onboarding.load_profile",
                        return_value={"identity": prof}):
            u = identity.user_identity()
        self.assertEqual(u["name"], "Dana Okonkwo")
        self.assertEqual(u["role"], "Marine biologist")
        self.assertEqual(u["primary_email"], "dana@lab.org")
        self.assertEqual(u["secondary_email"], "dana.ok@gmail.com")
        self.assertEqual(u["phone"], "+1 555 0100")
        self.assertEqual(u["source"], "profile")

    def test_empty_profile_is_not_known(self):
        with mock.patch("app.services.onboarding.load_profile",
                        return_value={"identity": {"name": "", "role": ""}}):
            self.assertEqual(identity.user_identity(), {})


class UserFromStoreTests(unittest.TestCase):
    """Fallback path: approved onboarding claims reconstruct identity when the
    profile JSON isn't on disk."""

    def setUp(self):
        from app.storage import Store
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_ident_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def _claim(self, text):
        import time
        eid = self.store.add_claim(text, extracted_at=time.time())
        self.store.review_fact(eid, "approved")

    def test_reconstructs_from_claims(self):
        self._claim("The user's name is Dana Okonkwo.")
        self._claim("The user works as a marine biologist.")
        self._claim("How the user describes their work: coral restoration.")
        self._claim("The user's primary email is dana@lab.org.")
        self._claim("The user's secondary email is dana.ok@gmail.com.")
        self._claim("The user's phone number is +1 555 0100.")
        with mock.patch("app.services.onboarding.load_profile", return_value=None):
            u = identity.user_identity(self.store)
        self.assertEqual(u["name"], "Dana Okonkwo")
        self.assertEqual(u["role"], "a marine biologist")
        self.assertIn("coral", u["description"].lower())
        self.assertEqual(u["primary_email"], "dana@lab.org")
        self.assertEqual(u["secondary_email"], "dana.ok@gmail.com")
        self.assertEqual(u["phone"], "+1 555 0100")
        self.assertEqual(u["source"], "memory")

    def test_profile_wins_over_store(self):
        self._claim("The user's name is Store Name.")
        with mock.patch("app.services.onboarding.load_profile",
                        return_value={"identity": {"name": "Profile Name"}}):
            u = identity.user_identity(self.store)
        self.assertEqual(u["name"], "Profile Name")
        self.assertEqual(u["source"], "profile")


class IdentityLinesTests(unittest.TestCase):
    def test_always_states_the_assistant(self):
        with mock.patch.object(identity, "user_identity", return_value={}):
            lines = identity.identity_lines()
        joined = " ".join(lines)
        self.assertIn("vinceo.ai", joined)
        self.assertTrue(any("who am i" in l.lower() for l in lines))

    def test_names_the_user_when_known(self):
        with mock.patch.object(identity, "user_identity",
                               return_value={"name": "Dana Okonkwo",
                                             "role": "Marine biologist",
                                             "description": "Coral work.",
                                             "primary_email": "dana@lab.org",
                                             "phone": "+1 555 0100"}):
            lines = identity.identity_lines()
        joined = " ".join(lines)
        self.assertIn("Dana Okonkwo", joined)
        self.assertIn("Marine biologist", joined)
        self.assertIn("dana@lab.org", joined)
        self.assertIn("+1 555 0100", joined)

    def test_unknown_user_is_honest(self):
        with mock.patch.object(identity, "user_identity", return_value={}):
            lines = identity.identity_lines()
        self.assertTrue(any("haven't introduced" in l or "don't know" in l.lower()
                            for l in lines))


class GroundingIntegrationTests(unittest.TestCase):
    def setUp(self):
        from app.storage import Store
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_ident_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def test_compose_puts_identity_first_and_names_user(self):
        import time
        eid = self.store.add_claim("The user's name is Dana Okonkwo.",
                                   extracted_at=time.time())
        self.store.review_fact(eid, "approved")
        from app.services.grounding import compose
        with mock.patch("app.services.onboarding.load_profile", return_value=None):
            g = compose("who am I?", store=self.store)
        block = g["block"]
        self.assertIn("ABOUT YOU", block)
        self.assertIn("Dana Okonkwo", block)
        # Clock may lead; identity is the first *named* person section.
        labels = [s["label"] for s in g["sources"]]
        self.assertIn("identity", labels)
        self.assertLess(labels.index("identity"),
                        next((i for i, L in enumerate(labels)
                              if L not in ("clock", "identity")), len(labels)))


class GeneralityTests(unittest.TestCase):
    def test_no_hardcoded_user_in_source(self):
        src = Path(identity.__file__).read_text(encoding="utf-8").lower()
        for leak in ("justin", "adorante", "jadorant", "villanova", "dt-capital",
                     "dtc venture"):
            self.assertNotIn(leak, src)


if __name__ == "__main__":
    unittest.main()
