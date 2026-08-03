"""People tab — roster, detail, rename/alias/note, and forget (with the
self-node guard). Storage first, then the endpoints via TestClient."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import self_profile
from app.storage import Store

NOW = 1_000_000_000.0


class StorePeopleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_get_person_with_aliases(self):
        pid = self.store.resolve_person("Christopher Lane", ts=NOW)
        self.store.touch_person(pid, NOW, alias="Chris")
        p = self.store.get_person(pid)
        self.assertEqual(p["name"], "Christopher Lane")
        self.assertIn("Chris", p["aliases"])
        self.assertIsNone(self.store.get_person(9999))

    def test_rename_keeps_old_name_as_alias(self):
        pid = self.store.resolve_person("Hark", ts=NOW)   # ASR mishearing
        self.assertTrue(self.store.rename_person(pid, "Mark Sullivan"))
        p = self.store.get_person(pid)
        self.assertEqual(p["name"], "Mark Sullivan")
        self.assertIn("Hark", p["aliases"])

    def test_rename_refuses_collision_and_empty(self):
        a = self.store.resolve_person("Alice", ts=NOW)
        self.store.resolve_person("Bob", ts=NOW)
        self.assertFalse(self.store.rename_person(a, "Bob"))   # merge, not rename
        self.assertFalse(self.store.rename_person(a, "  "))
        self.assertEqual(self.store.get_person(a)["name"], "Alice")


class PeopleEndpointTests(unittest.TestCase):
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
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ident = patch("app.services.identity.user_identity",
                           return_value={"name": "Test Person",
                                         "source": "profile"})
        self.ident.start()
        self.addCleanup(self.ident.stop)

    def test_list_marks_self_and_ranks(self):
        me = self_profile.self_person_id(self.store)
        other = self.store.resolve_person("Alice", ts=NOW)
        f = self.store.add_task("call Alice", extracted_at=NOW)
        self.store.add_relation("person", other, "committed", "fact", f, ts=NOW)
        d = self.client.get("/people/list").json()
        by_name = {p["name"]: p for p in d["people"]}
        self.assertTrue(by_name["Test Person"]["is_self"])
        self.assertFalse(by_name["Alice"]["is_self"])
        self.assertEqual(me, by_name["Test Person"]["id"])

    def test_detail_includes_alive_facts_only(self):
        pid = self.store.resolve_person("Alice", ts=NOW)
        keep = self.store.add_claim("Alice runs platform", extracted_at=NOW)
        gone = self.store.add_claim("Alice runs platfrom", extracted_at=NOW)
        self.store.add_relation("person", pid, "mentioned_in", "fact", keep, ts=NOW)
        self.store.add_relation("person", pid, "mentioned_in", "fact", gone, ts=NOW)
        self.store.supersede_fact(gone, keep, NOW)
        d = self.client.get(f"/people/{pid}").json()
        self.assertEqual([f["fact_id"] for f in d["facts"]], [keep])
        self.assertFalse(d["is_self"])
        self.assertEqual(self.client.get("/people/99999").status_code, 404)

    def test_rename_alias_note_flow(self):
        pid = self.store.resolve_person("Hark", ts=NOW)
        r = self.client.post(f"/people/{pid}/rename",
                             json={"name": "Mark Sullivan"})
        self.assertTrue(r.json()["ok"])
        self.client.post(f"/people/{pid}/alias", json={"alias": "Marky"})
        with patch("app.api.routes.memory.index_fact", lambda *a, **k: None):
            n = self.client.post(f"/people/{pid}/note",
                                 json={"text": "runs ops at Foundry"}).json()
        self.assertIn("Mark Sullivan", n["text"])   # name prefixed into the note
        p = self.client.get(f"/people/{pid}").json()
        self.assertEqual(p["name"], "Mark Sullivan")
        self.assertIn("Marky", p["aliases"])
        fact = self.store.get_fact(n["fact_id"])
        self.assertEqual(fact["review"], "approved")   # human note = gold
        self.assertEqual([f["fact_id"] for f in p["facts"]], [n["fact_id"]])

    def test_rename_collision_400(self):
        a = self.store.resolve_person("Alice", ts=NOW)
        self.store.resolve_person("Bob", ts=NOW)
        r = self.client.post(f"/people/{a}/rename", json={"name": "Bob"})
        self.assertEqual(r.status_code, 400)

    def test_forget_person_but_never_self(self):
        me = self_profile.self_person_id(self.store)
        junk = self.store.resolve_person("Dell", ts=NOW)
        r = self.client.post(f"/people/{junk}/forget")
        self.assertEqual(r.json()["deleted"], "Dell")
        self.assertIsNone(self.store.get_person(junk))
        r = self.client.post(f"/people/{me}/forget")
        self.assertEqual(r.status_code, 400)
        self.assertIsNotNone(self.store.get_person(me))


class AliasMentionRebuildTests(unittest.TestCase):
    """graph.rebuild must match aliases, not just the canonical name — a
    cofounder whose facts say "Hugh" (never "Hugh Salva") scored 0.0 live."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_alias_mentions_build_edges(self):
        from app.services import graph
        pid = self.store.resolve_person("Hugo Salvatore", ts=NOW)
        self.store.touch_person(pid, NOW, alias="Hugo")
        self.store.add_claim("Hugo wants the demo Friday", extracted_at=NOW)
        graph.rebuild(self.store)
        preds = [e["predicate"] for e in
                 self.store.relations_of("person", pid)["out"]]
        self.assertIn("mentioned_in", preds)

    def test_junk_aliases_never_match(self):
        from app.services import graph
        pid = self.store.resolve_person("Melody Park", ts=NOW)
        self.store.touch_person(pid, NOW, alias="me")   # stop-word alias
        self.store.add_claim("remind me about the audit", extracted_at=NOW)
        graph.rebuild(self.store)
        preds = [e["predicate"] for e in
                 self.store.relations_of("person", pid)["out"]]
        self.assertNotIn("mentioned_in", preds)


if __name__ == "__main__":
    unittest.main()
