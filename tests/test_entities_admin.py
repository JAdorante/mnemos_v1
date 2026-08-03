"""Orgs & tools tab — entity roster/detail/rename/alias/kind/note/forget,
plus the alias-aware entity mention scanning in graph.rebuild."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.storage import Store

NOW = 1_000_000_000.0


class StoreEntityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_get_and_rename_keeps_alias(self):
        eid = self.store.resolve_entity("Foundry Capitol", kind="org", ts=NOW)
        self.assertTrue(self.store.rename_entity(eid, "Foundry Capital"))
        e = self.store.get_entity(eid)
        self.assertEqual(e["name"], "Foundry Capital")
        self.assertIn("Foundry Capitol", e["aliases"])
        self.assertIsNone(self.store.get_entity(9999))

    def test_rename_refuses_case_insensitive_collision(self):
        a = self.store.resolve_entity("Figma", kind="tool", ts=NOW)
        self.store.resolve_entity("Linear", kind="tool", ts=NOW)
        self.assertFalse(self.store.rename_entity(a, "linear"))
        self.assertEqual(self.store.get_entity(a)["name"], "Figma")


class EntityEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_ranks_by_evidence(self):
        hot = self.store.resolve_entity("Mnemos", kind="project", ts=NOW)
        self.store.resolve_entity("Stale Corp", kind="org", ts=NOW - 200 * 86400)
        f = self.store.add_claim("Mnemos demo went well", extracted_at=NOW)
        self.store.add_relation("fact", f, "about", "entity", hot, ts=NOW)
        import time as _time
        with patch.object(_time, "time", return_value=NOW):
            d = self.client.get("/entities/list").json()
        names = [x["name"] for x in d["entities"]]
        self.assertEqual(names[0], "Mnemos")
        self.assertGreater(d["entities"][0]["weight"],
                           d["entities"][-1]["weight"])

    def test_detail_facts_people_and_kind_note_flow(self):
        eid = self.store.resolve_entity("Figma", kind="tool", ts=NOW)
        pid = self.store.resolve_person("Alice", ts=NOW)
        self.store.add_relation("person", pid, "associated_with", "entity",
                                eid, ts=NOW)
        with patch("app.api.routes.memory.index_fact", lambda *a, **k: None):
            n = self.client.post(f"/entities/{eid}/note",
                                 json={"text": "we design the deck in it"}).json()
        self.assertIn("Figma", n["text"])
        self.client.post(f"/entities/{eid}/kind", json={"kind": "software"})
        d = self.client.get(f"/entities/{eid}").json()
        self.assertEqual(d["kind"], "tool")           # normalized
        self.assertEqual([p["name"] for p in d["people"]], ["Alice"])
        self.assertEqual([f["fact_id"] for f in d["facts"]], [n["fact_id"]])
        self.assertEqual(
            self.store.get_fact(n["fact_id"])["review"], "approved")

    def test_bad_kind_400_and_forget(self):
        eid = self.store.resolve_entity("Junk Thing", kind="idea", ts=NOW)
        r = self.client.post(f"/entities/{eid}/kind", json={"kind": "wizard"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/entities/{eid}/forget")
        self.assertEqual(r.json()["deleted"], "Junk Thing")
        self.assertIsNone(self.store.get_entity(eid))
        self.assertEqual(self.client.get("/entities/99999").status_code, 404)


class EntityAliasRebuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_alias_mentions_build_about_edges(self):
        from app.services import graph
        eid = self.store.resolve_entity("FL Studio", kind="tool", ts=NOW)
        self.store.touch_entity(eid, NOW, alias="Fruity Loops")
        self.store.add_claim("mixed the track in Fruity Loops", extracted_at=NOW)
        graph.rebuild(self.store)
        preds = [e["predicate"] for e in
                 self.store.relations_of("entity", eid)["in"]]
        self.assertIn("about", preds)

    def test_two_letter_alias_never_matches(self):
        from app.services import graph
        eid = self.store.resolve_entity("Artificial Intelligence Lab",
                                        kind="org", ts=NOW)
        self.store.touch_entity(eid, NOW, alias="AI")
        self.store.add_claim("the ai summary looked fine", extracted_at=NOW)
        graph.rebuild(self.store)
        preds = [e["predicate"] for e in
                 self.store.relations_of("entity", eid)["in"]]
        self.assertNotIn("about", preds)


if __name__ == "__main__":
    unittest.main()
