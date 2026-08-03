"""Entity kind hygiene — normalize, person-shaped gate, batch apply."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path


class NormalizeEntityKindTests(unittest.TestCase):
    def test_remap_table(self):
        from app.services.name_quality import normalize_entity_kind

        self.assertEqual(normalize_entity_kind("product"), "tool")
        self.assertEqual(normalize_entity_kind("software"), "tool")
        self.assertEqual(normalize_entity_kind("company"), "org")
        self.assertEqual(normalize_entity_kind("organization"), "org")
        self.assertEqual(normalize_entity_kind("other"), "idea")
        self.assertEqual(normalize_entity_kind(None), "idea")
        self.assertEqual(normalize_entity_kind(""), "idea")
        self.assertEqual(normalize_entity_kind("location"), "place")
        self.assertEqual(normalize_entity_kind("project"), "project")
        self.assertEqual(normalize_entity_kind("org"), "org")


class PersonShapedGateTests(unittest.TestCase):
    def test_person_shaped_detection(self):
        from app.services.name_quality import (
            is_person_shaped_entity_name,
            should_mint_as_entity,
        )

        self.assertTrue(is_person_shaped_entity_name("Bill Clinton"))
        self.assertTrue(is_person_shaped_entity_name("Justin Adorante"))
        self.assertFalse(is_person_shaped_entity_name("OpenAI"))
        self.assertFalse(is_person_shaped_entity_name("Acme Corp"))
        # Must NOT title-case lowercase debris into "people"
        self.assertFalse(is_person_shaped_entity_name("webcam pipeline"))
        self.assertFalse(is_person_shaped_entity_name("quantum computing"))
        # Title-Case project/tool phrases are not people
        self.assertFalse(is_person_shaped_entity_name("Project Nexus"))
        self.assertFalse(is_person_shaped_entity_name("Memory Console"))
        self.assertFalse(is_person_shaped_entity_name("Claude Code"))
        self.assertFalse(is_person_shaped_entity_name("Google Calendar"))
        self.assertFalse(is_person_shaped_entity_name("United States"))
        self.assertFalse(is_person_shaped_entity_name("Diligence Robotics"))
        self.assertFalse(should_mint_as_entity("Bill Clinton", "project"))
        self.assertTrue(should_mint_as_entity("Chrysalis", "project"))
        self.assertTrue(should_mint_as_entity("Anthropic", "org"))
        self.assertTrue(should_mint_as_entity("Project Nexus", "project"))
        self.assertTrue(should_mint_as_entity("Design Review", "project"))
        # "Memory Console" is the app's OWN UI surface — since the July 28
        # self-suppression work it is a self-token and must never mint.
        self.assertFalse(should_mint_as_entity("Memory Console", "project"))

    def test_resolver_skips_person_shaped_project(self):
        from app.services.resolution import Resolver
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                r = Resolver(store=store)
                eid = r.resolve_entity("Bill Clinton", "project", ts=time.time())
                self.assertEqual(eid, 0)
                people = store.all_people()
                names = {(p.get("name") or p.get("canonical_name") or "").lower()
                         for p in people}
                self.assertIn("bill clinton", names)
            finally:
                store.close()

    def test_store_normalizes_product_on_insert(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                eid = store.resolve_entity("ChatGPT", kind="product", ts=time.time())
                row = next(e for e in store.all_entities() if e["id"] == eid)
                self.assertEqual(row.get("kind"), "tool")
            finally:
                store.close()


class BatchHygieneTests(unittest.TestCase):
    def test_plan_and_apply_fixture(self):
        from app.services import ambient_cleanup as ac
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                # Insert raw kinds bypassing normalize for product (simulate legacy)
                # resolve_entity now normalizes — use SQL for legacy product row.
                with store._lock:
                    store._conn.execute(
                        "INSERT INTO entities (canonical_name, kind, aliases, "
                        "first_seen, last_seen, canonical_id) "
                        "VALUES ('LegacySoft', 'product', '[]', 1, 1, "
                        "lower(hex(randomblob(16))))")
                    store._conn.execute(
                        "INSERT INTO entities (canonical_name, kind, aliases, "
                        "first_seen, last_seen, canonical_id) "
                        "VALUES ('Bill Clinton', 'project', '[]', 1, 1, "
                        "lower(hex(randomblob(16))))")
                    store._conn.commit()
                org_id = store.resolve_entity("Anthropic", kind="org", ts=time.time())

                self.assertEqual(graph.entity_constellation_kind("org"), "org")

                plan = ac.plan_entities(store, limit=100)
                by_name = {e["name"]: e for e in plan}
                self.assertIn("LegacySoft", by_name)
                self.assertEqual(by_name["LegacySoft"]["action"], "reclassify")
                self.assertEqual(by_name["LegacySoft"]["to_kind"], "tool")
                self.assertIn("Bill Clinton", by_name)
                self.assertEqual(by_name["Bill Clinton"]["action"], "hide_person")
                # Real org should not be in the hygiene plan.
                self.assertNotIn("Anthropic", by_name)

                applied = ac.apply(store, {"people": [], "entities": plan})
                self.assertTrue(applied["entities"])

                ents = {e["name"]: e for e in store.all_entities(include_hidden=True)}
                self.assertEqual(ents["LegacySoft"].get("kind"), "tool")
                self.assertTrue(ents["Bill Clinton"].get("hidden")
                                or ents["Bill Clinton"].get("hidden") == 1)
                # Org still visible and still org.
                org = next(e for e in store.all_entities(include_hidden=False)
                           if e["id"] == org_id)
                self.assertEqual(org.get("kind"), "org")
                people_names = {
                    (p.get("name") or p.get("canonical_name") or "").lower()
                    for p in store.all_people()
                }
                self.assertIn("bill clinton", people_names)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
