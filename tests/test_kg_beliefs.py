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


class StructuredClaimBeliefTests(unittest.TestCase):
    """Plan 2.5 — parseable claims → kg_beliefs; money conflict_flag."""

    def setUp(self) -> None:
        """Cut the semantic-dedup lookup off from the developer's real memory.

        `_mk()` isolates the SQLite store, but `fact_gate` does not use it for
        the embedding check: `_similar_active` goes through the process-wide
        `memory` singleton, whose vector index lives under the configured data
        dir — the machine's REAL one. So a claim these tests persist can be
        deduped ("cos 1.00 vs fact 1") against something the developer actually
        said months ago, and `_persist` returns 0 while the temp store's `facts`
        table is empty. The test then fails on a machine with data and passes on
        a clean checkout, which is the worst way for a test to behave.

        Nothing here is about dedup, so the honest isolation is to give the gate
        an empty neighbourhood. The underlying seam — a gate handed a `store`
        that it ignores for one of its two checks — is worth closing properly.
        """
        patcher = patch("app.services.fact_gate._similar_active",
                        return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_david_said_49_queryable_by_speaker(self):
        from app.services import kg_beliefs
        from app.services.consolidation import Turn
        from app.services.extractor import Extractor
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                ex = Extractor(store=store)
                turn = Turn(
                    start=NOW, end=NOW, speaker="Hugh",
                    text="David said the pilot plan is $49 a month",
                    event_ids=[], n_utterances=1)
                facts = {
                    "tasks": [], "commitments": [],
                    "claims": [{
                        "text": "David said the pilot plan is $49 a month",
                        "confidence": 0.9,
                        "source_span": "the pilot plan is $49 a month",
                        "assertion": "stated_by_other",
                        "subject": "pilot plan",
                        "predicate": "costs",
                        "object": "$49",
                        "speaker_is_source": False,
                    }],
                    "entities": [], "relations": [],
                }
                n = ex._persist(turn, facts, NOW)
                self.assertGreaterEqual(n, 1)
                hits = kg_beliefs.beliefs_by_speaker(store, "David")
                self.assertTrue(hits, "expected belief evidence attributed to David")
                pred = hits[0]["predicate"]
                self.assertEqual(pred.get("predicate"), "costs")
                # Object entity is the literal $49
                obj = store.get_entity(int(pred["obj_id"])) if hasattr(
                    store, "get_entity") else None
                if obj:
                    self.assertIn("49", obj.get("name") or "")
            finally:
                store.close()

    def test_simultaneous_price_conflict_flag_no_overwrite(self):
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                subj = store.resolve_entity("pilot plan", "other", ts=NOW)
                o49 = store.resolve_entity("$49", "other", ts=NOW)
                o55 = store.resolve_entity("$55", "other", ts=NOW)
                r1 = kg_beliefs.record_from_claim(
                    store, subj_type="entity", subj_id=subj,
                    predicate="costs", obj_type="entity", obj_id=o49,
                    fact_id=1, confidence=0.9, ts=NOW,
                    quote="pilot plan is $49", source_class="private_conversation",
                    speaker="David", speaker_is_source=False)
                self.assertTrue(r1.get("ok"))
                r2 = kg_beliefs.record_from_claim(
                    store, subj_type="entity", subj_id=subj,
                    predicate="costs", obj_type="entity", obj_id=o55,
                    fact_id=2, confidence=0.9, ts=NOW + 60,
                    quote="pilot plan is $55", source_class="private_conversation",
                    speaker="David", speaker_is_source=False)
                self.assertTrue(r2.get("ok"))
                # Both stay active — never silent overwrite
                p49 = store.find_kg_predicate(
                    subj_type="entity", subj_id=subj, predicate="costs",
                    obj_type="entity", obj_id=o49)
                p55 = store.find_kg_predicate(
                    subj_type="entity", subj_id=subj, predicate="costs",
                    obj_type="entity", obj_id=o55)
                self.assertIsNotNone(p49)
                self.assertIsNotNone(p55)
                self.assertTrue(p49.get("conflict"))
                self.assertTrue(p55.get("conflict"))
                flags = store.list_adjudications(kind="conflict_flag", limit=20)
                self.assertTrue(flags)
                self.assertEqual(flags[0].get("decision"), "defer")
            finally:
                store.close()

    def test_unparseable_claim_stays_flat(self):
        from app.services.consolidation import Turn
        from app.services.extractor import Extractor
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                ex = Extractor(store=store)
                turn = Turn(
                    start=NOW, end=NOW, speaker="Hugh",
                    text="The vibe in the room was weird",
                    event_ids=[], n_utterances=1)
                facts = {
                    "tasks": [], "commitments": [],
                    "claims": [{
                        "text": "The vibe in the room was weird",
                        "confidence": 0.7,
                        "source_span": "The vibe in the room was weird",
                        "assertion": "stated_by_user",
                        "subject": "",
                        "predicate": "",
                        "object": "",
                    }],
                    "entities": [], "relations": [],
                }
                ex._persist(turn, facts, NOW)
                # Flat claim exists; no costs/priced_at/due_on beliefs.
                claims = store.list_facts(kind="claim", limit=20)
                self.assertTrue(claims)
                preds = store.list_kg_predicates(predicate="costs", limit=20)
                self.assertEqual(preds, [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
