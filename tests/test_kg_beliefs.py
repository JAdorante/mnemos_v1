"""KG-A — belief dual-write, knowledge_entities gate, explain API."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.storage import Store

NOW = 1_700_000_000.0


def _mk(td: str) -> Store:
    return Store(db_path=Path(td) / "t.db", audio_dir=Path(td) / "audio")


class KgBeliefTests(unittest.TestCase):
    def test_asserted_relation_dual_writes_evidence(self):
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Patrick Adorante", ts=NOW)
                eid = store.resolve_entity("Dell", "org", ts=NOW)
                store.add_relation(
                    "person", pid, "works_at", "entity", eid,
                    origin="asserted", source_event_id=None,
                    confidence=0.9, ts=NOW,
                    quote="Patrick from Dell on the call",
                    source_class="meeting_transcript")
                pred = store.find_kg_predicate(
                    subj_type="person", subj_id=pid, predicate="works_at",
                    obj_type="entity", obj_id=eid)
                self.assertIsNotNone(pred)
                ev = store.list_kg_evidence(int(pred["id"]))
                self.assertGreaterEqual(len(ev), 1)
                self.assertIn("Dell", ev[0].get("quote") or "")
                exp = kg_beliefs.explain_predicate(store, int(pred["id"]))
                self.assertTrue(exp["ok"])
                self.assertIn("works_at", exp["explanation"])
                self.assertGreater(exp["confidence"], 0.3)
            finally:
                store.close()

    def test_derived_relation_skips_belief_store(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                eid = store.resolve_entity("Acme", "org", ts=NOW)
                store.add_relation(
                    "person", pid, "associated_with", "entity", eid,
                    origin="derived", ts=NOW)
                pred = store.find_kg_predicate(
                    subj_type="person", subj_id=pid,
                    predicate="associated_with",
                    obj_type="entity", obj_id=eid)
                self.assertIsNone(pred)
            finally:
                store.close()

    def test_evidence_accumulates_and_confidence_rises(self):
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Patrick", ts=NOW)
                eid = store.resolve_entity("Dell", "org", ts=NOW)
                store.add_relation(
                    "person", pid, "works_at", "entity", eid,
                    origin="asserted", ts=NOW, quote="first",
                    source_class="email", confidence=0.7)
                pred = store.find_kg_predicate(
                    subj_type="person", subj_id=pid, predicate="works_at",
                    obj_type="entity", obj_id=eid)
                c1 = kg_beliefs.recompute_confidence(store, int(pred["id"]),
                                                     now=NOW)
                kg_beliefs.record_from_relation(
                    store, subj_type="person", subj_id=pid,
                    predicate="works_at", obj_type="entity", obj_id=eid,
                    origin="asserted", ts=NOW + 10, quote="second sighting",
                    source_class="meeting_transcript")
                c2 = kg_beliefs.recompute_confidence(store, int(pred["id"]),
                                                     now=NOW + 10)
                self.assertGreaterEqual(c2, c1)
                self.assertGreaterEqual(
                    len(store.list_kg_evidence(int(pred["id"]))), 2)
            finally:
                store.close()


class KnowledgeEntitiesGateTests(unittest.TestCase):
    def test_news_denies_mint_allows_bind(self):
        from app.services.extractor import Extractor
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                existing = store.resolve_entity("Dell", "org", ts=NOW)
                ex = Extractor()
                ex._store = store
                facts = {
                    "entities": [
                        {"name": "BrandNewCorp", "kind": "org"},
                        {"name": "Dell", "kind": "org"},
                    ],
                    "relations": [],
                }
                with patch.dict("os.environ", {"QUILL_PEOPLE_V2": "1"}):
                    n, _ = ex._persist_entities(
                        facts, None, NOW,
                        event_source="desktop.screen",
                        window="TMZ - Chrome",
                        text="Breaking exclusive celebrity news")
                # Dell may be touched; BrandNewCorp must not appear.
                names = {e["name"] for e in store.recent_entities(limit=50)}
                # recent_entities returns last_seen ordered — also check exact
                self.assertIsNotNone(store.find_entity_exact("Dell"))
                self.assertIsNone(store.find_entity_exact("BrandNewCorp"))
                self.assertEqual(existing, store.find_entity_exact("Dell"))
            finally:
                store.close()


class KgExplainEndpointTests(unittest.TestCase):
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
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        import app.api.routes as routes_mod
        from app.services import memory as mem
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_explain_endpoint(self):
        pid = self.store.resolve_person("Pat", ts=NOW)
        eid = self.store.resolve_entity("Dell", "org", ts=NOW)
        self.store.add_relation(
            "person", pid, "works_at", "entity", eid,
            origin="user", ts=NOW, quote="I set this",
            source_class="user")
        r = self.client.get(
            "/kg/explain",
            params={"subj_type": "person", "subj_id": pid,
                    "predicate": "works_at", "obj_type": "entity",
                    "obj_id": eid})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("ok"))
        self.assertIn("works_at", r.json().get("explanation", ""))


if __name__ == "__main__":
    unittest.main()
