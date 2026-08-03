"""Name-quality gate + graph hygiene — keep extractor junk out of the graph.

Cases are the ACTUAL junk found polluting the live constellation (pronouns,
role words, fragments, env/system tokens, file paths) alongside the real people
and entities that must survive. Also covers the resolver refusing to create a
junk node, and the store's detach-not-destroy delete.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.services import name_quality as nq


class PlausiblePersonTests(unittest.TestCase):
    def test_real_names_pass(self):
        for n in ["Justin Adorante", "Marc", "Chris Falloon", "Abby Nengel",
                  "Hugh Salva", "Dan", "Patrick Adorante"]:
            self.assertTrue(nq.is_plausible_person(n), n)

    def test_lowercase_asr_names_title_cased(self):
        self.assertEqual(nq.normalize_person_name("justin adorante"),
                         "Justin Adorante")
        self.assertTrue(nq.is_plausible_person("justin adorante"))
        self.assertTrue(nq.is_plausible_person("abby nengel"))

    def test_junk_people_rejected(self):
        for n in ["she", "us", "you", "user", "new user", "curator", "founder",
                  "board", "not specified", "autonomous browser agent",
                  "vinceo.ai", "QUILL_AGENT", "QA and CTO review", "set it to 0",
                  "vision classifies a page as todo_list"]:
            self.assertFalse(nq.is_plausible_person(n), n)

    def test_edge_cases(self):
        self.assertFalse(nq.is_plausible_person(""))
        self.assertFalse(nq.is_plausible_person("a"))
        self.assertFalse(nq.is_plausible_person("app/services/memory.py"))


class PlausibleEntityTests(unittest.TestCase):
    def test_real_entities_pass(self):
        for n in ["GitHub", "AWS", "DTC Venture Pulse", "Google Cloud Console",
                  "pyttsx3", "edge-tts", "Notion"]:
            self.assertTrue(nq.is_plausible_entity(n), n)

    def test_real_projects_survive(self):
        # REGRESSION GUARD: real project names are often snake_case or lowercase
        # multi-word — the gate must NOT reject them (that would delete the user's
        # own projects during cleanup).
        for n in ["alpaca_market_data", "dtc_agent_test", "sync shop campaign",
                  "synch shop campaign"]:
            self.assertTrue(nq.is_plausible_entity(n), n)

    def test_junk_entities_rejected(self):
        for n in ["app/services/memory.py", "data/quill.db", "scripts/phone_link/",
                  "vinceo.ai", "Mnemos", "Quill", "QUILL_AGENT", "www.dell.com",
                  "$49/mo", "X", "free neural (online) voices",
                  "Windows (primary)"]:
            self.assertFalse(nq.is_plausible_entity(n), n)


class ResolverGateTests(unittest.TestCase):
    def setUp(self):
        from app.services.resolution import Resolver
        from app.storage import Store
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_nq_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.r = Resolver(store=self.store)

    def test_resolver_refuses_junk_person(self):
        self.assertIsNone(self.r.resolve_person("QUILL_AGENT"))
        self.assertIsNone(self.r.resolve_person("set it to 0"))
        self.assertEqual(self.store.all_people(), [])   # nothing created

    def test_resolver_keeps_real_person(self):
        pid = self.r.resolve_person("Justin Adorante")
        self.assertIsNotNone(pid)
        self.assertEqual(len(self.store.all_people()), 1)

    def test_resolver_refuses_junk_entity(self):
        self.assertEqual(self.r.resolve_entity("data/quill.db"), 0)
        self.assertEqual(self.store.all_entities(), [])


class DeleteDetachesTests(unittest.TestCase):
    def setUp(self):
        from app.storage import Store
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_nq_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def test_delete_person_detaches_task_not_deletes_it(self):
        pid = self.store.resolve_person("Junk Owner")
        fid = self.store.add_task("real task text", owner_person_id=pid,
                                  extracted_at=time.time())
        row = self.store.delete_person(pid)
        self.assertEqual(row["id"], pid)
        # person gone, but the task survives with a null owner
        self.assertTrue(all(p["id"] != pid for p in self.store.all_people()))
        task = self.store.get_fact(fid)
        self.assertIsNotNone(task)

    def test_delete_entity_removes_row(self):
        eid = self.store.resolve_entity("JunkEntity", "project")
        row = self.store.delete_entity(eid)
        self.assertEqual(row["id"], eid)
        self.assertTrue(all(e["id"] != eid for e in self.store.all_entities()))


if __name__ == "__main__":
    unittest.main()
