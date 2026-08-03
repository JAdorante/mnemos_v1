"""The self node — first-person memory routing and the living user profile.

Identity resolution is patched (who the user is comes from onboarding at
runtime); the store is real (temp) so linking/lifecycle behavior is proven."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import self_profile
from app.storage import Store


class FirstPersonTests(unittest.TestCase):
    def test_first_person_positives(self):
        for t in ("I prefer morning meetings",
                  "my office moved to Radnor",
                  "I'm switching the deck to Figma",
                  "send the notes to me by Friday",
                  "I'll handle the pilot myself"):
            self.assertTrue(self_profile.is_first_person(t), t)

    def test_third_person_negatives(self):
        for t in ("Hugh prefers morning meetings",
                  "the office moved to Radnor",
                  "Marc said forty-nine a month",
                  "mining the data is slow",       # 'mine' must be word-bounded
                  "immediate action required"):    # 'i' inside a word
            self.assertFalse(self_profile.is_first_person(t), t)

    def test_we_is_not_self(self):
        self.assertFalse(self_profile.is_first_person("we ship the pilot"))

    def test_self_name_tokens(self):
        for n in ("me", "I", "Myself", "the user", "self"):
            self.assertTrue(self_profile.is_self_name(n), n)
        for n in ("Mel", "Ira", "Hugh", ""):
            self.assertFalse(self_profile.is_self_name(n), n)


class SelfNodeTests(unittest.TestCase):
    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def _known_user(self, name="Test Person"):
        return patch("app.services.identity.user_identity",
                     return_value={"name": name, "source": "profile"})

    def test_self_person_id_creates_and_caches(self):
        with self._known_user():
            pid = self_profile.self_person_id(self.store)
            self.assertIsNotNone(pid)
            names = {p["name"] for p in self.store.all_people()}
            self.assertIn("Test Person", names)
        # cached: no identity lookup needed the second time
        self.assertEqual(self_profile.self_person_id(self.store), pid)

    def test_unknown_user_resolves_none(self):
        with patch("app.services.identity.user_identity", return_value={}):
            self.assertIsNone(self_profile.self_person_id(self.store))

    def test_link_self_and_profile_lines(self):
        with self._known_user():
            f1 = self.store.add_claim("I prefer morning meetings",
                                      extracted_at=100.0)
            f2 = self.store.add_claim("I am off coffee", extracted_at=200.0)
            self.assertTrue(self_profile.link_self(self.store, f1, 100.0))
            self.assertTrue(self_profile.link_self(self.store, f2, 200.0))
            lines = self_profile.profile_lines(self.store)
        self.assertTrue(lines[0].startswith("USER PROFILE"))
        body = "\n".join(lines)
        self.assertIn("off coffee", body)
        self.assertIn("morning meetings", body)
        # freshest first
        self.assertLess(body.index("off coffee"), body.index("morning meetings"))

    def test_superseded_self_fact_leaves_profile(self):
        with self._known_user():
            old = self.store.add_claim("I love espresso", extracted_at=100.0)
            new = self.store.add_claim("I am off coffee", extracted_at=200.0)
            self_profile.link_self(self.store, old, 100.0)
            self_profile.link_self(self.store, new, 200.0)
            self.store.supersede_fact(old, new, 250.0)
            body = "\n".join(self_profile.profile_lines(self.store))
        self.assertIn("off coffee", body)
        self.assertNotIn("espresso", body)

    def test_no_links_no_section(self):
        with self._known_user():
            self.assertEqual(self_profile.profile_lines(self.store), [])

    def test_link_self_without_known_user_is_false(self):
        with patch("app.services.identity.user_identity", return_value={}):
            fid = self.store.add_claim("I like tea", extracted_at=100.0)
            self.assertFalse(self_profile.link_self(self.store, fid, 100.0))


class OwnerRoutingTests(unittest.TestCase):
    """documents._persist_facts routes owner='me' to the self node and links
    first-person claims — the path typed chat ingestion uses."""

    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_me_owner_and_first_person_claim_reach_self_node(self):
        from app.services.documents import _persist_facts
        facts = {
            "tasks": [{"text": "send Marc the revised deck", "owner": "me",
                       "source_span": "send Marc the revised deck",
                       "confidence": 0.9}],
            "commitments": [],
            "claims": [{"text": "I prefer short emails",
                        "source_span": "I prefer short emails",
                        "confidence": 0.9}],
            "entities": [], "relations": [],
        }
        with patch("app.services.identity.user_identity",
                   return_value={"name": "Test Person", "source": "profile"}), \
             patch("app.services.extractor._index_fact", lambda *a, **k: None), \
             patch("app.services.fact_gate._similar_active", return_value=[]):
            n = _persist_facts(self.store, facts, None,
                               "send Marc the revised deck. "
                               "I prefer short emails", 100.0)
        self.assertEqual(n, 2)
        pid = self_profile.self_person_id(self.store)
        task = [f for f in self.store.facts_since(0.0)
                if f["kind"] == "task"][0]
        self.assertEqual(task["owner"], "Test Person")
        edges = self.store.relations_of("person", pid)["out"]
        self_edges = [e for e in edges
                      if e["predicate"] == self_profile.SELF_PREDICATE]
        self.assertEqual(len(self_edges), 1)
        # and the profile section renders from it
        body = "\n".join(self_profile.profile_lines(self.store))
        self.assertIn("short emails", body)


class ProfilePageTests(unittest.TestCase):
    """The /profile page + /profile/data endpoint (served via TestClient)."""

    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_page_renders(self):
        r = self.client.get("/profile")
        self.assertEqual(r.status_code, 200)
        for marker in ("You", "/profile/data", "What you", "Forget"):
            self.assertIn(marker, r.text)

    def test_profile_data_groups_self_facts_and_owned_work(self):
        import app.api.routes as routes_mod
        with patch("app.services.identity.user_identity",
                   return_value={"name": "Test Person", "source": "profile"}), \
             patch.object(routes_mod.memory, "_ensure_store",
                          return_value=self.store):
            pid = self_profile.self_person_id(self.store)
            claim = self.store.add_claim("I prefer short emails",
                                         extracted_at=100.0)
            self_profile.link_self(self.store, claim, 100.0)
            self.store.add_task("send Marc the deck", extracted_at=200.0,
                                owner_person_id=pid)
            self.store.add_task("someone else's task", extracted_at=200.0)
            d = self.client.get("/profile/data").json()
        self.assertTrue(d["self_known"])
        self.assertEqual(d["identity"]["name"], "Test Person")
        self.assertEqual([f["text"] for f in d["about"]],
                         ["I prefer short emails"])
        self.assertEqual([f["text"] for f in d["work"]],
                         ["send Marc the deck"])

    def test_profile_data_unknown_user(self):
        import app.api.routes as routes_mod
        with patch("app.services.identity.user_identity", return_value={}), \
             patch.object(routes_mod.memory, "_ensure_store",
                          return_value=self.store):
            d = self.client.get("/profile/data").json()
        self.assertFalse(d["self_known"])
        self.assertEqual(d["about"], [])
        self.assertEqual(d["work"], [])


if __name__ == "__main__":
    unittest.main()
