"""Entity model, Phase 0c: a note relayed by a cloud assistant binds, never mints.

An assistant's note can name anything. Once the user accepts it, the note may
attach facts to things the graph already holds, but it cannot create a tool,
org or idea of its own. The one thing an accepted note does create is the
project the user filed it under, and that row is written before the event is,
so extraction finds it instead of minting it.

The claim path follows the same gate (a claim's subject is not minted from a
bind-only source), and a claim's value ("$49") is kept as a hidden node so it
can carry the belief without showing up on the Orgs & tools tab.

Every test runs against a real Store on a temp db; the only stand-in is the
model call that produces the extraction.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Imported here, before any test patches `app.storage.get_store`: a module
# first imported under that patch would keep the stand-in for good.
import app.api.routes  # noqa: F401
import app.services.extractor  # noqa: F401
from app.services import assistant_inbox, chat_ingest, projects
from app.services.memory import memory
from app.services.resolution import Resolver
from app.storage import Store

NOW = 1_700_000_000.0
GRANT = {"id": "g1", "assistant": "claude", "label": "Claude"}

NOTE = ("Atlas will use Postgres for the pilot. The pilot plan is $99 a "
        "month and Atlas costs $49 a month.")
FACTS = {
    "tasks": [],
    "commitments": [],
    "claims": [
        {"text": "Atlas costs $49 a month", "confidence": 0.9,
         "source_span": "Atlas costs $49 a month",
         "assertion": "stated_by_other",
         "subject": "Atlas", "predicate": "costs", "object": "$49"},
        {"text": "The pilot plan is $99 a month", "confidence": 0.9,
         "source_span": "The pilot plan is $99 a month",
         "assertion": "stated_by_other",
         "subject": "pilot plan", "predicate": "costs", "object": "$99"},
    ],
    "entities": [
        # The model calls the project a tool, and names a tool nobody has.
        {"name": "Atlas", "kind": "tool", "confidence": 0.9},
        {"name": "Postgres", "kind": "tool", "confidence": 0.9},
    ],
    "relations": [
        {"subject": "Atlas", "subject_kind": "project", "predicate": "uses",
         "object": "Postgres", "object_kind": "tool",
         "source_span": "Atlas will use Postgres", "confidence": 0.9},
        {"subject": "Postgres", "subject_kind": "tool", "predicate": "uses",
         "object": "pgvector", "object_kind": "tool",
         "source_span": "Atlas will use Postgres", "confidence": 0.9},
    ],
}


def _mk(tmp: str) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio")


class PolicyFileTests(unittest.TestCase):
    def test_assistant_notes_may_not_mint_entities(self):
        from app.services import kg_beliefs, source_policy
        pol = source_policy.policy_for("assistant_capture")
        self.assertFalse(pol.knowledge_entities)
        # The rest of the class is unchanged: the note is still mined.
        self.assertTrue(pol.create_claims)
        for source in ("assistant.claude", "assistant.chatgpt",
                       "assistant.grok", "assistant.other"):
            ok, cls = kg_beliefs.allow_entity_mint(
                event_source=source, text="Atlas will use Postgres")
            self.assertEqual((ok, cls), (False, "assistant_capture"), source)
        ok, cls = kg_beliefs.allow_entity_mint(
            event_source="chat.user", text="Atlas will use Postgres")
        self.assertTrue(ok, cls)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_bind_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        from app.services import resolution
        self.queued: list[tuple[str, dict]] = []
        outer = self

        class _FakeWorker:
            def enqueue(self, kind, payload=None, *, unique=False):
                outer.queued.append((kind, payload or {}))
                return 1

        import app.services.worker as worker_mod
        for p in (
            mock.patch.dict(os.environ, {
                "QUILL_DATA_DIR": self.tmp, "QUILL_HEADS": "off",
                "QUILL_USAGE_LEDGER": "0"}),
            mock.patch.object(resolution, "resolver",
                              Resolver(store=self.store)),
            mock.patch("app.storage.get_store", return_value=self.store),
            mock.patch.object(memory, "_ensure_store",
                              return_value=self.store),
            mock.patch.object(worker_mod, "worker", _FakeWorker()),
            mock.patch.object(projects, "_scopes", return_value=[]),
            mock.patch.object(assistant_inbox, "_queue_route"),
            mock.patch("app.services.attachments._index_event"),
            mock.patch("app.services.extractor._index_fact",
                       lambda *a, **k: None),
            mock.patch("app.services.fact_gate._similar_active",
                       return_value=[]),
        ):
            p.start()
            self.addCleanup(p.stop)

    def hidden(self, entity_id: int) -> bool:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT hidden FROM entities WHERE id = ?",
                (int(entity_id),)).fetchone()
        return bool(row["hidden"])

    def names(self, *, include_hidden: bool = False) -> set[str]:
        return {e["name"] for e in
                self.store.all_entities(include_hidden=include_hidden)}

    def mine(self, event_source: str, text: str = NOTE, facts=None) -> int:
        """Store one event from `event_source` and run the real extraction
        job over it, with the model's answer fixed."""
        from app.events import Event, Modality
        eid = self.store.insert(Event(
            time=NOW, modality=Modality.TEXT, raw=text,
            summary=text[:40], source=event_source))
        with mock.patch("app.services.extractor.extractor._extract_text",
                        return_value=dict(facts or FACTS)):
            chat_ingest.run_job({"event_id": eid, "text": text})
        return eid


class AcceptedNoteTests(_Case):
    def accept(self, **kw) -> dict:
        out = assistant_inbox.propose(GRANT, {"text": NOTE, **kw})
        self.assertTrue(out.get("inbox_id"), out)
        res = assistant_inbox.accept(out["inbox_id"])
        self.assertTrue(res.get("ok"), res)
        return res

    def test_project_row_exists_before_the_event_is_written(self):
        seen: dict = {}
        real_insert = self.store.insert

        def spy(ev):
            # The store lock is free here: insert has not started.
            eid = self.store.find_entity_exact("Atlas")
            seen["entity"] = self.store.get_entity(eid) if eid else None
            seen["source"] = ev.source
            return real_insert(ev)

        with mock.patch.object(self.store, "insert", side_effect=spy):
            res = self.accept(project="Atlas")
        self.assertEqual(res["project"], "Atlas")
        self.assertEqual(seen["source"], "assistant.claude")
        self.assertIsNotNone(
            seen["entity"],
            "the filing must create the project before the event insert")
        self.assertEqual(
            (seen["entity"]["kind"], seen["entity"]["kind_source"]),
            ("project", "filing"))

    def test_accepted_note_binds_the_project_and_mints_nothing_else(self):
        res = self.accept(project="Atlas")
        atlas = self.store.find_entity_exact("Atlas")
        self.assertTrue(atlas)
        jobs = [p for kind, p in self.queued if kind == "assistant_capture"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["event_id"], res["event_id"])

        with mock.patch("app.services.extractor.extractor._extract_text",
                        return_value=dict(FACTS)):
            chat_ingest.run_job(jobs[0])

        # Only the filed project is on the tab. The tool the note named,
        # the relation endpoint and the claim subject were not minted.
        self.assertEqual(self.names(), {"Atlas"})
        for name in ("Postgres", "pgvector", "pilot plan"):
            self.assertIsNone(self.store.find_entity_exact(name), name)
        # The model's "tool" label did not override the filing.
        row = self.store.get_entity(atlas)
        self.assertEqual((row["kind"], row["kind_source"]),
                         ("project", "filing"))
        # No edge was written to a node that does not exist.
        self.assertEqual(
            [e for e in self.store.relations_of("entity", atlas)["out"]
             if e["predicate"] == "uses"], [])
        # The note's facts are still kept and filed under the project.
        claims = self.store.list_facts(kind="claim", limit=10)
        self.assertEqual(len(claims), 2)
        about = {int(e["subj_id"]) for e in
                 self.store.relations_of("entity", atlas)["in"]
                 if e["subj_type"] == "fact" and e["predicate"] == "about"}
        self.assertEqual(about, {int(c["fact_id"]) for c in claims})
        # A claim about the existing project binds to it...
        costs = self.store.list_kg_predicates(predicate="costs", limit=20)
        self.assertEqual(
            [(p["subj_type"], int(p["subj_id"])) for p in costs],
            [("entity", atlas)])
        # ...through a hidden value node, which never reaches the tab.
        value = self.store.get_entity(int(costs[0]["obj_id"]))
        self.assertEqual(value["name"], "$49")
        self.assertEqual((value["kind"], value["kind_source"]),
                         ("thing", "rule"))
        self.assertTrue(self.hidden(value["id"]))
        self.assertIsNone(self.store.find_entity_exact("$99"))

    def test_accepting_a_note_brings_back_a_project_the_user_forgot(self):
        first = self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.store.delete_entity(first, tombstone=True, reason="forget")
        self.assertTrue(self.store.is_entity_tombstoned("Atlas"))
        # The accept is the user's own click, so the filing may undo it.
        self.accept(project="Atlas")
        atlas = self.store.find_entity_exact("Atlas")
        self.assertTrue(atlas)
        row = self.store.get_entity(atlas)
        self.assertEqual((row["kind"], row["kind_source"]),
                         ("project", "filing"))
        self.assertFalse(self.store.is_entity_tombstoned("Atlas"))

    def test_job_after_a_forget_does_not_bring_the_project_back(self):
        # Accept, then forget the project before the queued job runs: the
        # job is automation and must leave it forgotten.
        self.accept(project="Atlas")
        atlas = self.store.find_entity_exact("Atlas")
        self.store.delete_entity(atlas, tombstone=True, reason="forget")
        job = [p for kind, p in self.queued if kind == "assistant_capture"][0]
        with mock.patch("app.services.extractor.extractor._extract_text",
                        return_value=dict(FACTS)):
            chat_ingest.run_job(job)
        self.assertEqual(self.names(include_hidden=True), set())
        self.assertTrue(self.store.is_entity_tombstoned("Atlas"))
        self.assertEqual(len(self.store.list_facts(kind="claim", limit=10)), 2)
        with self.store._lock:
            n = self.store._conn.execute(
                "SELECT COUNT(*) FROM relations WHERE obj_type='entity'"
            ).fetchone()[0]
        self.assertEqual(int(n), 0)

    def test_the_same_note_typed_by_the_user_does_mint(self):
        # Control for the test above: with a mint-eligible source the same
        # extraction creates the names the user typed. What the user did
        # not type as a name waits in the candidate ledger (Phase 1): the
        # endpoint the model added ("pgvector" is nowhere in the note) and
        # the lowercase claim subject ("pilot plan").
        self.mine("chat.user")
        self.assertEqual(self.names(), {"Atlas", "Postgres"})
        atlas = self.store.find_entity_exact("Atlas")
        postgres = self.store.find_entity_exact("Postgres")
        self.assertEqual(self.store.get_entity(postgres)["kind_source"],
                         "model")
        self.assertIn(("uses", postgres), {
            (e["predicate"], int(e["obj_id"]))
            for e in self.store.relations_of("entity", atlas)["out"]})
        subjects = {int(p["subj_id"]) for p in
                    self.store.list_kg_predicates(predicate="costs", limit=20)}
        self.assertEqual(subjects, {atlas})
        waiting = {m["name"] for m in
                   self.store.entity_mentions(status="pending")}
        self.assertEqual(waiting, {"pgvector", "pilot plan"})
        self.assertEqual(
            [(r["subj_id"], r["predicate"], r["obj_name"])
             for r in self.store.pending_entity_relations()],
            [(postgres, "uses", "pgvector")])

    def test_typed_note_mints_everything_with_the_candidate_gate_off(self):
        # The behaviour before Phase 1, still there behind the flag: the
        # tool, the endpoint and the claim subject at first sight.
        with mock.patch.dict(os.environ, {"QUILL_ENTITY_CANDIDATES": "0"}):
            self.mine("chat.user")
        self.assertTrue({"Atlas", "Postgres", "pgvector", "pilot plan"}
                        <= self.names())
        atlas = self.store.find_entity_exact("Atlas")
        postgres = self.store.find_entity_exact("Postgres")
        self.assertIn(("uses", self.store.find_entity_exact("pgvector")), {
            (e["predicate"], int(e["obj_id"]))
            for e in self.store.relations_of("entity", postgres)["out"]})
        subjects = {int(p["subj_id"]) for p in
                    self.store.list_kg_predicates(predicate="costs", limit=20)}
        self.assertEqual(
            subjects, {atlas, self.store.find_entity_exact("pilot plan")})
        self.assertEqual(self.store.entity_mentions(status="pending"), [])

    def test_note_filed_under_miscellaneous_mints_nothing_at_all(self):
        res = self.accept()
        self.assertEqual(res["project"], projects.MISC)
        job = [p for kind, p in self.queued if kind == "assistant_capture"][0]
        with mock.patch("app.services.extractor.extractor._extract_text",
                        return_value=dict(FACTS)):
            chat_ingest.run_job(job)
        self.assertEqual(self.names(), set())
        self.assertEqual(
            self.store.list_kg_predicates(predicate="costs", limit=20), [])
        self.assertEqual(len(self.store.list_facts(kind="claim", limit=10)), 2)


class BindOnlyPathTests(_Case):
    def test_bind_only_source_still_binds_what_exists(self):
        postgres = self.store.resolve_entity("Postgres", "tool", ts=NOW - 500)
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW - 500,
                                          kind_source="filing")
        self.mine("assistant.claude")
        self.assertEqual(self.names(), {"Atlas", "Postgres"})
        self.assertGreater(self.store.get_entity(postgres)["last_seen"],
                           NOW - 500)
        # Both endpoints exist, so the edge is written; the one whose
        # object is unknown is dropped.
        out = {(e["predicate"], int(e["obj_id"]))
               for e in self.store.relations_of("entity", atlas)["out"]}
        self.assertIn(("uses", postgres), out)
        self.assertEqual(self.store.relations_of("entity", postgres)["out"],
                         [])
        self.assertIsNone(self.store.find_entity_exact("pgvector"))

    def test_relation_endpoint_is_not_minted_from_screen_text(self):
        from app.services.extractor import Extractor
        facts = {"entities": [], "relations": [
            {"subject": "Atlas", "subject_kind": "project",
             "predicate": "uses", "object": "Postgres", "object_kind": "tool",
             "source_span": "Atlas uses Postgres", "confidence": 0.9}]}
        ex = Extractor(store=self.store)
        ne, nr = ex._persist_entities(
            facts, None, NOW, event_source="desktop.screen",
            window="Notes", text="Atlas uses Postgres")
        self.assertEqual((ne, nr), (0, 0))
        self.assertEqual(self.names(include_hidden=True), set())
        # Control: the same relation from a mint-eligible source creates
        # both endpoints and the edge.
        ne, nr = ex._persist_entities(
            facts, None, NOW, event_source="chat.user",
            text="Atlas uses Postgres")
        self.assertEqual(nr, 1)
        self.assertEqual(self.names(), {"Atlas", "Postgres"})

    def test_mint_gate_is_one_rule_for_every_extractor_path(self):
        from app.services.extractor import Extractor
        gate = Extractor._entity_mint_gate
        self.assertEqual(gate(event_source="chat.user", text="x")[0], True)
        self.assertEqual(gate(event_source="audio.whisper", text="x")[0], True)
        self.assertEqual(gate(event_source="assistant.claude", text="x"),
                         (False, "assistant_capture"))
        self.assertEqual(
            gate(event_source="desktop.screen", window="Notes", text="x")[0],
            False)

    def test_claim_value_is_hidden_and_the_belief_still_reads(self):
        from app.services import kg_beliefs
        self.mine("chat.user", text="Atlas costs $49 a month", facts={
            "tasks": [], "commitments": [], "entities": [], "relations": [],
            "claims": [FACTS["claims"][0]]})
        value_id = self.store.find_entity_exact("$49")
        self.assertTrue(value_id)
        value = self.store.get_entity(value_id)
        self.assertEqual((value["kind"], value["kind_source"]),
                         ("thing", "rule"))
        self.assertTrue(self.hidden(value_id))
        self.assertNotIn("$49", self.names())
        self.assertIn("$49", self.names(include_hidden=True))
        atlas = self.store.find_entity_exact("Atlas")
        pred = self.store.find_kg_predicate(
            subj_type="entity", subj_id=atlas, predicate="costs",
            obj_type="entity", obj_id=value_id)
        self.assertIsNotNone(pred)
        # Readers label the object through get_entity, which sees hidden
        # rows, so the belief still reads with its value.
        self.assertIn("$49", kg_beliefs.explain_predicate(
            self.store, int(pred["id"])).get("explanation", ""))

    def test_claim_value_reuses_a_visible_entity_without_hiding_it(self):
        # A claim object that is already a real entity is bound as it is.
        acme = self.store.resolve_entity("Acme", "org", ts=NOW,
                                         kind_source="user")
        self.mine("chat.user", text="Atlas is priced at Acme", facts={
            "tasks": [], "commitments": [], "entities": [], "relations": [],
            "claims": [{**FACTS["claims"][0],
                        "text": "Atlas costs Acme",
                        "source_span": "Atlas is priced at Acme",
                        "object": "Acme"}]})
        row = self.store.get_entity(acme)
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "user"))
        self.assertFalse(self.hidden(acme))
        self.assertIn("Acme", self.names())


if __name__ == "__main__":
    unittest.main()
