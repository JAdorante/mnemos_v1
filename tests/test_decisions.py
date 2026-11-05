"""One Decision record for every human verdict.

Every approval surface writes one `decisions` row, and learning pairs for
human verdicts are derived from it: the pair carries the decision id and the
decision points back at the pair.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import decisions, learning_store
from app.storage import Store


def mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp(prefix="mnemos-test-decisions-"))
    return Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")


class _Base(unittest.TestCase):
    def setUp(self):
        self.store = mk_store()
        self._env = mock.patch.dict(os.environ, {"QUILL_LEARNING": "1",
                                                 "QUILL_EXEMPLARS": "0"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.store._conn.close()


class RecordTests(_Base):
    def test_normalizes_surface_vocabularies(self):
        for raw, want in (("approve", "approved"), ("accepted", "approved"),
                          ("cancel", "rejected"), ("deny", "rejected"),
                          ("edit", "edited"), ("removed", "undone")):
            self.assertEqual(decisions.normalize(raw), want, raw)
        self.assertIsNone(decisions.normalize("maybe"))

    def test_unknown_verdict_writes_nothing(self):
        self.assertIsNone(decisions.record(
            surface="x", subject="y:1", verdict="maybe", store=self.store))
        self.assertEqual(self.store.list_decisions(), [])

    def test_row_carries_what_was_shown_and_who(self):
        res = decisions.record(
            surface="agent.packet", subject="packet:7", verdict="approve",
            shown_hash="abc", shown={"summary": "Send"},
            options=["approve", "edit", "cancel"], via="button",
            store=self.store)
        row = self.store.get_decision(res["id"])
        self.assertEqual(row["verdict"], "approved")
        self.assertEqual(row["shown_hash"], "abc")
        self.assertEqual(row["shown"], {"summary": "Send"})
        self.assertEqual(row["options"], ["approve", "edit", "cancel"])
        self.assertEqual((row["actor"], row["via"]), ("user", "button"))
        self.assertIsNone(row["learning_pair_id"])

    def test_pair_is_derived_and_linked_both_ways(self):
        res = decisions.record(
            surface="facts.review", subject="fact:3", verdict="edited",
            edit="Send Dana the pricing deck", store=self.store,
            learn=dict(task_type="extraction.commitment",
                       input_text="I owe Dana the deck",
                       local_output="send deck",
                       final_target="Send Dana the pricing deck"))
        pid = res["learning_pair_id"]
        self.assertTrue(pid)
        pair = self.store.get_learning_pair(pid)
        self.assertEqual(pair["verdict"], "edited")
        self.assertEqual(pair["verdict_source"], "facts.review")
        self.assertEqual(pair["source_refs"]["decision_id"], res["id"])
        self.assertEqual(self.store.get_decision(res["id"])["learning_pair_id"],
                         pid)

    def test_verdicts_that_teach_nothing_write_no_pair(self):
        res = decisions.record(
            surface="facts.review", subject="fact:3", verdict="done",
            store=self.store,
            learn=dict(task_type="extraction.task", input_text="x",
                       final_target="something"))
        self.assertIsNone(res["learning_pair_id"])

    def test_undo_deletes_the_pair_it_produced(self):
        res = decisions.record(
            surface="assistant.inbox", subject="inbox:9", verdict="approved",
            store=self.store,
            learn=dict(task_type="assistant.save",
                       input_text="Q3 budget is 40k for pilots",
                       final_target="Q3 budget is 40k for pilots"))
        pid = res["learning_pair_id"]
        self.assertIsNotNone(self.store.get_learning_pair(pid))
        u = decisions.undo(surface="assistant.inbox", subject="inbox:9",
                           store=self.store)
        self.assertEqual(u["verdict"], "undone")
        self.assertIsNone(self.store.get_learning_pair(pid))
        row = self.store.get_decision(u["id"])
        self.assertEqual(row["meta"]["undoes"], res["id"])

    def test_summary_counts_learned(self):
        decisions.record(surface="peer.ask", subject="peer_ask:1",
                         verdict="rejected", store=self.store)
        decisions.record(surface="peer.ask", subject="peer_ask:2",
                         verdict="approved", store=self.store,
                         learn=dict(task_type="peer.disclosure",
                                    input_text="topic: work\nquestion: eta?",
                                    final_target="share work"))
        s = decisions.summary(store=self.store)
        self.assertEqual(s["surfaces"]["peer.ask"]["n"], 2)
        self.assertEqual(s["surfaces"]["peer.ask"]["learned"], 1)


class SurfaceTests(_Base):
    def test_fact_verdict_via_get_fact_keeps_the_fact_id(self):
        # Store.get_fact names the id `fact_id`; the pair used to store None.
        fid = self.store.add_task("Send Dana the deck", source_event_id=None,
                                  source_span="Send Dana the deck",
                                  confidence=0.9, extracted_at=1.0)
        fact = self.store.get_fact(fid)
        pid = learning_store.record_fact_verdict(fact, "accepted",
                                                 store=self.store)
        pair = self.store.get_learning_pair(pid)
        self.assertEqual(pair["source_refs"]["fact_id"], fid)
        d = self.store.list_decisions(subject=f"fact:{fid}")
        self.assertEqual([x["verdict"] for x in d], ["approved"])

    def _packet(self, rec):
        rec.start_run("email Dana the pricing follow-up")
        return rec.record_packet(
            summary="Send email to Dana", goal="email Dana the follow-up",
            fields={"to": "dana@example.com", "subject": "Pricing",
                    "body": "Hi Dana, it is $49 a month."})

    def test_outbound_edit_reaches_the_learning_loop(self):
        from app.services.agent_log import Recorder
        rec = Recorder(store=self.store)
        pkt = self._packet(rec)
        rec.record_decision(pkt, "edit", user_edit="make it shorter please",
                            approved_via="typed")
        d = self.store.list_decisions(subject=f"packet:{pkt}")[0]
        self.assertEqual((d["surface"], d["verdict"]), ("agent.packet", "edited"))
        self.assertEqual(d["edit"], "make it shorter please")
        pair = self.store.get_learning_pair(d["learning_pair_id"])
        self.assertEqual(pair["task_type"], "agent.draft")
        self.assertEqual(pair["verdict"], "edited")

    def test_fleet_offer_is_recorded_without_a_pair(self):
        from app.services.agent_log import Recorder
        rec = Recorder(store=self.store)
        pkt = self._packet(rec)
        rec.record_decision(pkt, "cancel", approved_via="button",
                            surface="fleet.offer")
        d = self.store.list_decisions(subject=f"packet:{pkt}")[0]
        self.assertEqual((d["surface"], d["verdict"]), ("fleet.offer", "rejected"))
        self.assertIsNone(d["learning_pair_id"])

    def test_denied_peer_disclosure_reaches_the_learning_loop(self):
        from app.services import peer_channel
        with mock.patch("app.storage.get_store", return_value=self.store):
            peer_channel._record_disclosure_decision(
                {"id": "a1", "peer_id": "p1", "ask_id": "x1", "kind": "ask",
                 "topic": "personal", "question": "Is Sam job hunting?"},
                {"name": "Marc"}, approve=False)
        d = self.store.list_decisions(subject="peer_ask:a1")[0]
        self.assertEqual((d["surface"], d["verdict"]), ("peer.ask", "rejected"))
        pair = self.store.get_learning_pair(d["learning_pair_id"])
        self.assertEqual((pair["task_type"], pair["verdict"]),
                         ("peer.disclosure", "rejected"))

    def test_task_offer_outcome_is_a_decision(self):
        from app.services import task_offer
        with mock.patch("app.storage.get_store", return_value=self.store):
            task_offer.record_offer_outcome("book the room", True, fact_id=4)
        d = self.store.list_decisions(subject="fact:4")[0]
        self.assertEqual((d["surface"], d["verdict"]), ("task.offer", "approved"))


if __name__ == "__main__":
    unittest.main()
