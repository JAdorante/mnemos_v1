"""Connector capture & task fulfillment spec — Feature 2: tasks and
commitments that close themselves.

Unit coverage of every transition in the state machine (including the two
illegal ones: done without evidence, and re-proposal from a declined
thread), the additive migration, evidence-based completion from a calendar
read-back, weak evidence yielding a question, user-created tasks, and the
honest fulfillment metric.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.events import Event, Modality  # noqa: E402
from app.services import commitment_state as cs  # noqa: E402
from app.services import task_completion as tc  # noqa: E402

NOW = 1_726_600_000.0  # 2024-09-17-ish; the tests pass `now` explicitly


def _mk(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db")


class StateMachineTests(unittest.TestCase):
    def test_every_state_has_a_status(self):
        for st in cs.STATES:
            self.assertIn(cs.status_for(st), cs.STATUSES)

    def test_new_statuses_round_trip(self):
        for status in ("awaiting_data", "uncertain", "declined"):
            self.assertEqual(cs.status_for(cs.state_for_status(status)), status)

    def test_spec_diagram_transitions_are_legal(self):
        legal = [
            ("detected", "active"),          # proposed → open (user confirms)
            ("detected", "declined"),        # proposed → declined
            ("active", "awaiting_data"),     # open → awaiting_data
            ("awaiting_data", "active"),     # fill found + confirmed
            ("awaiting_data", "completed"),  # delivered fill closes it
            ("active", "completed"),         # verified evidence
            ("active", "uncertain"),         # evidence weak
            ("uncertain", "completed"),      # user confirms
            ("uncertain", "active"),         # user says not yet
            ("active", "declined"),          # user cancels
            ("awaiting_data", "cancelled"),  # sibling resolved elsewhere
            ("awaiting_data", "declined"),   # Drop on the horizon strip
        ]
        for a, b in legal:
            self.assertTrue(cs.is_legal(a, b), (a, b))
            cs.require_legal(a, b)

    def test_terminal_states(self):
        self.assertFalse(cs.is_legal("declined", "active"))
        self.assertFalse(cs.is_legal("declined", "completed"))
        with self.assertRaises(cs.TransitionError):
            cs.require_legal("declined", "active")
        # completed / cancelled may still reopen (plan 4.1 undo).
        self.assertTrue(cs.is_legal("completed", "active"))

    def test_done_is_attributable_only_with_evidence_or_user(self):
        self.assertTrue(cs.done_is_attributable(
            {"to_state": "completed", "actor": "capture", "evidence_id": 7}))
        self.assertTrue(cs.done_is_attributable(
            {"to_state": "completed", "actor": "user", "evidence_id": None}))
        self.assertTrue(cs.done_is_attributable(
            {"to_state": "completed", "actor": "agent",
             "evidence": {"evidence_event_id": 3}}))
        self.assertFalse(cs.done_is_attributable(
            {"to_state": "completed", "actor": "agent", "evidence": {"note": "x"}}))
        self.assertFalse(cs.done_is_attributable(
            {"to_state": "cancelled", "actor": "user"}))
        self.assertFalse(cs.done_is_attributable(None))


class StoreTransitionTests(unittest.TestCase):
    def test_migration_adds_columns_and_tables(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                cols = {r["name"] for r in store._conn.execute(
                    "PRAGMA table_info(commitments)").fetchall()}
                for c in ("slot_json", "review_after", "requester_kind",
                          "requester_id", "task_kind", "counterparty_name",
                          "question", "thread_key", "linked_declined_id"):
                    self.assertIn(c, cols)
                tcols = {r["name"] for r in store._conn.execute(
                    "PRAGMA table_info(commitment_transitions)").fetchall()}
                self.assertIn("evidence_id", tcols)
                self.assertIn("actor", tcols)
                names = {r["name"] for r in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                self.assertIn("slot_candidates", names)
                self.assertIn("connector_sync", names)
                # Idempotent: a second migrate pass is a no-op.
                store._migrate_task_slots()
            finally:
                store.close()

    def test_illegal_done_without_evidence_or_user(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment("Send Dana the deck", extracted_at=NOW)
                store.transition_commitment(fid, "active", reason="confirm")
                for actor in ("capture", "agent", "peer", "user"):
                    with self.assertRaises(cs.TransitionError):
                        store.transition_commitment(fid, "completed", actor=actor)
                self.assertEqual(store.get_task(fid)["status"], "open")
                # With a cite it closes and the transition carries evidence_id.
                out = store.transition_commitment(
                    fid, "completed", actor="capture",
                    evidence={"source": "sent_folder", "evidence_event_id": 42})
                self.assertEqual(out["evidence_id"], 42)
                tx = store.last_transition(fid)
                self.assertEqual(tx["evidence_id"], 42)
                self.assertEqual(tx["actor"], "capture")
                self.assertTrue(cs.done_is_attributable(tx))
            finally:
                store.close()

    def test_uncertain_keeps_question_and_answer_paths(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment("Have a call with Marc", extracted_at=NOW,
                                           task_kind="call", counterparty_name="Marc")
                store.transition_commitment(fid, "active", actor="user")
                store.transition_commitment(
                    fid, "uncertain", actor="capture",
                    evidence={"source": "audio_session", "evidence_event_id": 9},
                    question="Did the call with Marc happen?")
                row = store.get_task(fid)
                self.assertEqual(row["status"], "uncertain")
                self.assertEqual(row["question"], "Did the call with Marc happen?")
                # Not yet → back to open, question cleared.
                tc.answer(store, fid, False)
                self.assertEqual(store.get_task(fid)["status"], "open")
                self.assertIsNone(store.get_task(fid)["question"])
                # Ask again, then yes → done with the user's cite.
                store.transition_commitment(
                    fid, "uncertain", actor="capture",
                    evidence={"source": "audio_session", "evidence_event_id": 9},
                    question="Did the call with Marc happen?")
                out = tc.answer(store, fid, True)
                self.assertEqual(out["to_state"], "completed")
                self.assertEqual(store.get_task(fid)["status"], "done")
                self.assertEqual(store.last_transition(fid)["actor"], "user")
            finally:
                store.close()

    def test_declined_thread_never_reproposes_but_new_mention_links(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                eid = store.insert(Event(time=NOW, modality=Modality.SYSTEM,
                                         raw="Re: deck", source="google.mail",
                                         meta={"thread_id": "T-9"}))
                fid = store.add_commitment("Send Dana the deck", extracted_at=NOW,
                                           source_event_id=eid)
                self.assertEqual(store.get_task(fid)["thread_key"],
                                 "google:thread_id:T-9")
                store.transition_commitment(fid, "declined", actor="user")
                # Same thread → not re-proposed (0).
                eid2 = store.insert(Event(time=NOW + 1, modality=Modality.SYSTEM,
                                          raw="Re: deck again", source="google.mail",
                                          meta={"thread_id": "T-9"}))
                self.assertEqual(store.add_commitment("Send Dana the deck",
                                                      extracted_at=NOW + 1,
                                                      source_event_id=eid2), 0)
                # A new mention elsewhere → a new proposed task linked to it.
                fid3 = store.add_commitment("Send Dana the deck", extracted_at=NOW + 2)
                self.assertTrue(fid3)
                self.assertEqual(store.get_task(fid3)["linked_declined_id"], fid)
                self.assertEqual(store.get_task(fid3)["status"], "open")
            finally:
                store.close()


class CompletionDetectorTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = _mk(self._td.name)
        self.notices = []
        self.offers = []
        self._p1 = mock.patch.object(tc, "_notify",
                                     side_effect=lambda text, stream=None:
                                     self.notices.append((text, stream)))
        self._p2 = mock.patch.object(tc, "ask_user",
                                     side_effect=lambda store, task, q, eid:
                                     self.offers.append(q) or True)
        self._p1.start(); self._p2.start()

    def tearDown(self):
        self._p1.stop(); self._p2.stop()
        self.store.close()
        self._td.cleanup()

    def _call_task(self) -> int:
        fid = tc.create_user_task(self.store, "Have a call with Marc",
                                  counterparty="Marc", kind="call", now=NOW)
        self.assertTrue(fid)
        row = self.store.get_task(fid)
        self.assertEqual((row["task_kind"], row["counterparty_name"],
                          row["status"]), ("call", "Marc", "open"))
        return fid

    def test_calendar_read_back_closes_the_call(self):
        fid = self._call_task()
        now = NOW + 3600
        ev = Event(time=now - 60, modality=Modality.SYSTEM,
                   raw="Call with Marc", summary="[calendar] Call with Marc",
                   source="phone.calendar", people=["Marc"],
                   meta={"summary": "Call with Marc", "start": now - 1500,
                         "end": now - 60, "attendees": [{"name": "Marc"}]})
        eid = self.store.insert(ev)
        res = tc.detect(self.store, eid, ev, now=now)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["verdict"], "completes")
        self.assertEqual(res[0]["applied"], "completed")
        row = self.store.get_task(fid)
        self.assertEqual(row["status"], "done")
        tx = self.store.last_transition(fid)
        self.assertEqual(tx["evidence_id"], eid)
        self.assertEqual(tx["actor"], "capture")
        self.assertTrue(self.notices)
        text, stream = self.notices[-1]
        self.assertIn("Marked done", text)
        self.assertIn("24-min", text)
        self.assertEqual(stream["type"], "task.completed")
        self.assertEqual(stream["task_id"], fid)

    def test_weak_evidence_yields_one_question_never_a_close(self):
        fid = self._call_task()
        ev = Event(time=NOW + 100, modality=Modality.AUDIO,
                   raw="Marc: yeah let's do it", source="audio.whisper",
                   confidence=0.9, people=["Marc"])
        eid = self.store.insert(ev)
        res = tc.detect(self.store, eid, ev, now=NOW + 200)
        self.assertEqual(res[0]["verdict"], "completes")
        self.assertEqual(res[0]["applied"], "uncertain")
        row = self.store.get_task(fid)
        self.assertEqual(row["status"], "uncertain")
        self.assertIn("Did the call with Marc happen?", row["question"])
        self.assertEqual(self.offers, [row["question"]])
        # A second weak sighting does not ask twice.
        ev2 = Event(time=NOW + 300, modality=Modality.AUDIO, raw="Marc again",
                    source="audio.whisper", confidence=0.9, people=["Marc"])
        eid2 = self.store.insert(ev2)
        res2 = tc.detect(self.store, eid2, ev2, now=NOW + 400)
        self.assertEqual(res2[0]["applied"], "already_uncertain")
        self.assertEqual(len(self.offers), 1)
        # It never auto-closes: after 48 h it is on the horizon strip.
        items = tc.horizon_items(self.store, now=NOW + 400 + 49 * 3600)
        self.assertEqual(items[0]["kind"], "pending_ask")
        self.assertEqual(items[0]["fact_id"], fid)
        self.assertEqual(self.store.get_task(fid)["status"], "uncertain")

    def test_future_calendar_block_only_progresses(self):
        fid = self._call_task()
        ev = Event(time=NOW, modality=Modality.SYSTEM, raw="Call with Marc",
                   source="phone.calendar", people=["Marc"],
                   meta={"summary": "Call with Marc", "start": NOW + 3600,
                         "end": NOW + 5400})
        eid = self.store.insert(ev)
        res = tc.detect(self.store, eid, ev, now=NOW)
        self.assertEqual(res[0]["verdict"], "progresses")
        self.assertEqual(self.store.get_task(fid)["status"], "open")

    def test_phone_call_toast_variants(self):
        fid = self._call_task()
        missed = Event(time=NOW, modality=Modality.NOTIFICATION,
                       raw="Missed call from Marc", source="notifications.phone_link",
                       meta={"app": "Phone Link"})
        eid = self.store.insert(missed)
        self.assertEqual(tc.detect(self.store, eid, missed, now=NOW)[0]["verdict"],
                         "progresses")
        ended = Event(time=NOW + 10, modality=Modality.NOTIFICATION,
                      raw="Call ended · Marc · 24 min",
                      source="notifications.phone_link", meta={"app": "Phone Link"})
        eid2 = self.store.insert(ended)
        res = tc.detect(self.store, eid2, ended, now=NOW + 10)
        self.assertEqual(res[0]["applied"], "completed")
        self.assertEqual(self.store.get_task(fid)["status"], "done")

    def test_sent_mail_read_back_closes_a_send_task(self):
        fid = tc.create_user_task(self.store, "Send Dana the deck",
                                  counterparty="Dana", kind="send", now=NOW)
        ev = Event(time=NOW + 5, modality=Modality.NOTIFICATION,
                   raw="Message sent to Dana Whitfield", source="desktop.screen")
        eid = self.store.insert(ev)
        res = tc.detect(self.store, eid, ev, now=NOW + 5)
        self.assertEqual(res[0]["applied"], "completed")
        self.assertEqual(self.store.get_task(fid)["status"], "done")
        self.assertEqual(self.store.last_transition(fid)["evidence_id"], eid)

    def test_unrelated_event_leaves_tasks_alone(self):
        fid = self._call_task()
        ev = Event(time=NOW, modality=Modality.SYSTEM, raw="Lunch with Priya",
                   source="phone.calendar", people=["Priya"],
                   meta={"summary": "Lunch with Priya", "start": NOW - 4000,
                         "end": NOW - 400})
        eid = self.store.insert(ev)
        self.assertEqual(tc.detect(self.store, eid, ev, now=NOW), [])
        self.assertEqual(self.store.get_task(fid)["status"], "open")

    def test_insert_hook_runs_detector_in_sync_mode(self):
        fid = self._call_task()
        with mock.patch.dict(os.environ, {"QUILL_TASK_COMPLETION_SYNC": "1"}):
            tc.attach()
            try:
                now = NOW + 3600
                self.store.insert(Event(
                    time=now - 60, modality=Modality.SYSTEM, raw="Call with Marc",
                    source="phone.calendar", people=["Marc"],
                    meta={"summary": "Call with Marc", "start": now - 1500,
                          "end": now - 60}))
            finally:
                tc.detach()
        self.assertEqual(self.store.get_task(fid)["status"], "done")


class IntentAndMetricTests(unittest.TestCase):
    def test_parse_task_intent(self):
        p = tc.parse_task_intent("Have a call with Marc")
        self.assertEqual((p["kind"], p["counterparty"]), ("call", "Marc"))
        p = tc.parse_task_intent("email Dana Whitfield about the deck")
        self.assertEqual((p["kind"], p["counterparty"]), ("send", "Dana Whitfield"))
        p = tc.parse_task_intent("get me the Boston quote")
        self.assertEqual((p["kind"], p["need"]), ("slot", "the Boston quote"))
        p = tc.parse_task_intent("keep an eye out for the Acme invoice")
        self.assertEqual(p["kind"], "slot")
        self.assertTrue(p["watch"])
        self.assertIsNone(tc.parse_task_intent("what did Marc say yesterday?"))
        self.assertIsNone(tc.parse_task_intent("call me maybe"))

    def test_fulfillment_counts_done_only_when_attributable(self):
        from app.services import fulfillment
        facts = [
            {"kind": "commitment", "status": "done", "done_verified": True,
             "extracted_at": NOW - 86400, "updated_at": NOW},
            {"kind": "commitment", "status": "done", "done_verified": False,
             "extracted_at": NOW - 86400, "updated_at": NOW},
            {"kind": "commitment", "status": "cancelled", "extracted_at": NOW - 86400},
            {"kind": "commitment", "status": "declined", "extracted_at": NOW - 86400},
            {"kind": "commitment", "status": "awaiting_data", "extracted_at": NOW - 100},
            {"kind": "commitment", "status": "uncertain", "extracted_at": NOW - 100},
        ]
        s = fulfillment.summarize(facts, now=NOW)
        self.assertEqual(s["counts"], {"open": 2, "done": 1, "cancelled": 1})
        self.assertEqual(s["fulfillment_rate"], 0.5)

    def test_annotate_done_verified_reads_the_transition_log(self):
        from app.services import fulfillment
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment("x", extracted_at=NOW)
                store.transition_commitment(fid, "active", actor="user")
                store.transition_commitment(
                    fid, "completed", actor="capture",
                    evidence={"source": "calendar_get", "evidence_event_id": 5})
                rows = fulfillment.annotate_done_verified(
                    store, store.list_facts(kind="commitment"))
                self.assertTrue(rows[0]["done_verified"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
