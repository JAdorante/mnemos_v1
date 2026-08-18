"""Workstream A — unified verdict harvesting (learning_store + learning_pairs).

Acceptance criteria covered here:
  * every surface adapter lands one correctly-populated row with source_refs
  * dedupe (UNIQUE task_type+content_hash) and stub-drop
  * redaction: no API keys/PII persist in input/target
  * personal-classed content is stamped shadow_eligible=0 (fail-closed)
  * hard delete cascades to the exemplar store
  * backfill (record_from_distill) is idempotent
  * invariant 3: the learning store never touches the approval/trust layer
"""
from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import learning_store as ls
from app.storage import Store


def mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp())
    return Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = mk_store()

    def _record(self, **kw):
        base = dict(
            task_type="extraction.task",
            input_text="Justin said he will send the deck to Sarah on Friday",
            final_target="Send the deck to Sarah by Friday",
            verdict="accepted",
            verdict_source="facts.review",
            source_refs={"fact_id": 7, "source_event_id": 42},
            store=self.store,
        )
        base.update(kw)
        return ls.record(**base)

    def test_record_populates_row(self) -> None:
        pid = self._record()
        self.assertIsNotNone(pid)
        row = self.store.get_learning_pair(pid)
        self.assertEqual(row["task_type"], "extraction.task")
        self.assertEqual(row["verdict"], "accepted")
        self.assertEqual(row["verdict_source"], "facts.review")
        self.assertEqual(row["source_refs"]["fact_id"], 7)
        self.assertEqual(row["source_refs"]["source_event_id"], 42)
        self.assertTrue(row["human_confirmed"])
        self.assertTrue(row["content_hash"])

    def test_dedupe_by_content_hash(self) -> None:
        self.assertIsNotNone(self._record())
        self.assertIsNone(self._record())          # identical → deduped
        self.assertEqual(
            len(self.store.list_learning_pairs(task_type="extraction.task")), 1)

    def test_stub_target_dropped(self) -> None:
        self.assertIsNone(self._record(final_target="ok"))

    def test_negative_verdict_needs_no_target(self) -> None:
        pid = self._record(verdict="dismissed", final_target="")
        self.assertIsNotNone(pid)
        self.assertEqual(self.store.get_learning_pair(pid)["verdict"],
                         "dismissed")

    def test_unknown_verdict_skipped(self) -> None:
        self.assertIsNone(self._record(verdict="meh"))

    def test_disabled_is_noop(self) -> None:
        with patch.dict(os.environ, {"QUILL_LEARNING": "0"}, clear=False):
            self.assertIsNone(self._record())

    def test_redaction_scrubs_secrets(self) -> None:
        pid = self._record(
            input_text="use key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
                       "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-AAAA "
                       "for the deploy step of the release runbook",
            final_target="Documented deploy step without the credential")
        self.assertIsNotNone(pid)
        row = self.store.get_learning_pair(pid)
        self.assertNotIn("sk-ant-", row["input_text"])

    def test_personal_content_not_shadow_eligible(self) -> None:
        pid = self._record(
            input_text="Sarah's personal cell is 610-555-0147, email "
                       "sarah.k@example.com — call her about the review",
            final_target="Call Sarah about the review meeting tomorrow")
        row = self.store.get_learning_pair(pid)
        self.assertFalse(row["shadow_eligible"])
        self.assertIn(row["privacy_class"],
                      ("personal", "sensitive", "never-send"))

    def test_neutral_content_is_shadow_eligible(self) -> None:
        pid = self._record()
        self.assertTrue(self.store.get_learning_pair(pid)["shadow_eligible"])


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = mk_store()

    def test_fact_verdict_adapter(self) -> None:
        fact = {"id": 3, "kind": "task", "source_event_id": None,
                "text": "Send the quarterly deck to Sarah",
                "source_span": "I'll send the quarterly deck to Sarah"}
        pid = ls.record_fact_verdict(fact, "edited",
                                     edited_text="Send Q3 deck to Sarah Kane",
                                     store=self.store)
        row = self.store.get_learning_pair(pid)
        self.assertEqual(row["task_type"], "extraction.task")
        self.assertEqual(row["verdict"], "edited")
        self.assertEqual(row["final_target"], "Send Q3 deck to Sarah Kane")
        self.assertEqual(row["source_refs"]["fact_id"], 3)

    def test_commitment_kind_maps(self) -> None:
        fact = {"id": 4, "kind": "commitment",
                "text": "Justin promised the report by Monday",
                "source_span": "I promise the report by Monday"}
        pid = ls.record_fact_verdict(fact, "accepted", store=self.store)
        self.assertEqual(self.store.get_learning_pair(pid)["task_type"],
                         "extraction.commitment")

    def test_reflection_kind_routing(self) -> None:
        stale = {"id": 9, "kind": "stale_fact",
                 "text": "Possibly stale (95d): project Atlas is paused",
                 "detail": "Review whether this still holds."}
        pid = ls.record_reflection_verdict(stale, "accepted", store=self.store)
        self.assertEqual(self.store.get_learning_pair(pid)["task_type"],
                         "audit.stale_fact")
        insight = {"id": 10, "kind": "recommendation",
                   "text": "Consider batching the review meetings weekly"}
        pid2 = ls.record_reflection_verdict(insight, "dismissed",
                                            store=self.store)
        self.assertEqual(self.store.get_learning_pair(pid2)["task_type"],
                         "brief.section")

    def test_person_merge_adapter(self) -> None:
        pid = ls.record_person_merge(
            {"id": 1, "name": "Sarah Kane"}, {"id": 2, "name": "S. Kane"},
            merge_id=77, store=self.store)
        row = self.store.get_learning_pair(pid)
        self.assertEqual(row["task_type"], "person.resolution")
        self.assertEqual(row["source_refs"]["merge_id"], 77)
        self.assertIn("S. Kane", row["input_text"])

    def test_kg_evidence_adapter(self) -> None:
        pid = ls.record_kg_evidence_verdict(
            {"id": 5, "predicate_id": 12,
             "quote": "Sarah moved to the platform team in July"},
            {"predicate": "works_on(Sarah, platform)"},
            "confirm", store=self.store)
        row = self.store.get_learning_pair(pid)
        self.assertEqual(row["task_type"], "extraction.claim")
        self.assertEqual(row["verdict"], "accepted")
        self.assertEqual(row["source_refs"]["evidence_id"], 5)


_DISTILL_ROW = {
    "id": "abc123", "time": 1700000000.0, "task": "chat", "reason": "low_confidence",
    "modality": "text", "local_model": "qwen2.5:7b-instruct",
    "local": {"text": "I think the meeting is Tuesday"},
    "parent": {"text": "Your meeting with Sarah is Wednesday at 2pm"},
    "meta": {"prompt_head": "when is my meeting with sarah?",
             "messages": [{"role": "user",
                           "text": "when is my meeting with sarah?"}]},
    "user_outcome": "accepted",
}


class DistillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = mk_store()

    def test_from_distill_and_idempotent(self) -> None:
        pid = ls.record_from_distill(_DISTILL_ROW, "accepted",
                                     verdict_source="legacy_distill",
                                     store=self.store)
        row = self.store.get_learning_pair(pid)
        self.assertEqual(row["task_type"], "escalation.text")
        self.assertEqual(row["final_target"],
                         "Your meeting with Sarah is Wednesday at 2pm")
        self.assertEqual(row["model_tag"], "qwen2.5:7b-instruct")
        self.assertEqual(row["source_refs"]["distill_id"], "abc123")
        # Backfill idempotency: the same row imports exactly once.
        self.assertIsNone(ls.record_from_distill(
            _DISTILL_ROW, "accepted", verdict_source="legacy_distill",
            store=self.store))

    def test_edited_beats_parent(self) -> None:
        row = dict(_DISTILL_ROW, id="def456")
        pid = ls.record_from_distill(row, "edited",
                                     edited_text="Wednesday 2pm with Sarah Kane",
                                     store=self.store)
        self.assertEqual(self.store.get_learning_pair(pid)["final_target"],
                         "Wednesday 2pm with Sarah Kane")


class DeleteCascadeTests(unittest.TestCase):
    def test_delete_cascades_to_exemplars(self) -> None:
        store = mk_store()
        pid = ls.record(task_type="extraction.task",
                        input_text="send the deck to sarah kane tomorrow",
                        final_target="Send the deck to Sarah Kane",
                        verdict="accepted", verdict_source="facts.review",
                        store=store)
        deleted: list[str] = []
        with patch("app.services.exemplar_store.delete_for_pair",
                   side_effect=deleted.append):
            self.assertTrue(ls.delete(pid, store=store))
        self.assertEqual(deleted, [pid])
        self.assertIsNone(store.get_learning_pair(pid))

    def test_counts_shape(self) -> None:
        store = mk_store()
        ls.record(task_type="extraction.task",
                  input_text="prepare the standup notes for tomorrow morning",
                  final_target="Prepare standup notes",
                  verdict="accepted", verdict_source="facts.review",
                  store=store)
        c = ls.counts(store=store)
        self.assertIn("extraction.task", c["total"])
        self.assertEqual(c["total"]["extraction.task"]["accepted"], 1)


class InvariantTests(unittest.TestCase):
    def test_learning_store_never_imports_approval_layer(self) -> None:
        """Invariant 3: learning affects proposal quality, never authority.
        The learning store must not import the trust/approval/readiness layer."""
        src = Path("app/services/learning_store.py").read_text(encoding="utf-8")
        banned = {"app.services.trust", "app.services.readiness"}
        found = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            if isinstance(node, ast.Import):
                for n in node.names:
                    found.add(n.name)
        self.assertFalse(found & banned)

    def test_trust_layer_never_imports_learning(self) -> None:
        """And the reverse: the risk table stays a lookup, not a learner."""
        src = Path("app/services/trust.py").read_text(encoding="utf-8")
        found = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            if isinstance(node, ast.Import):
                for n in node.names:
                    found.add(n.name)
        self.assertFalse(found & {"app.services.learning_store",
                                  "app.services.exemplar_store",
                                  "app.services.escalation_router"})


if __name__ == "__main__":
    unittest.main()
