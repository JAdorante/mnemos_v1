"""People v3 P3 (WS-A) — voice-track escrow + retroactive rebind.

Flag OFF (default): behavior is byte-identical to the pre-escrow pipeline —
the unbound-'me' paths still return None, no speaker_tracks rows exist, and
nothing is ever marked escrowed.

Flag ON (QUILL_PEOPLE_ESCROW=1): facts whose subject is an unbound diarization
track ("Speaker N") are kept but escrowed against a durable track id, excluded
from every default surface (grounding/retrieval/boards/people scoring inputs/
constellation edges), and rewritten onto the person by a durable, idempotent
rebind job once the track is labeled (or follows a person merge).
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import people_escrow, self_profile
from app.services.consolidation import Turn
from app.services.extractor import Extractor
from app.storage import Store


def _mk_store():
    tmp = Path(tempfile.mkdtemp())
    return Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")


_TASK = {
    "text": "send the pricing deck",
    "owner": "me",
    "due": "2026-08-14",
    "confidence": 0.9,
    "source_span": "I'll send the pricing deck",
    "assertion": "stated_by_user",
}
_COMMITMENT = {
    "text": "follow up with Eve",
    "from_person": "me",
    "to_person": "Eve",
    "due": "2026-08-15",
    "confidence": 0.9,
    "source_span": "I'll follow up with Eve",
    "assertion": "stated_by_user",
}
_CLAIM_FIRST_PERSON = {
    "text": "I prefer morning meetings",
    "confidence": 0.9,
    "source_span": "I prefer morning meetings",
    "assertion": "stated_by_user",
}
_CLAIM_NEUTRAL = {
    "text": "the demo is on Friday",
    "confidence": 0.9,
    "source_span": "the demo is on Friday",
    "assertion": "stated_by_user",
}


def _facts(tasks=(), commitments=(), claims=()):
    return {"tasks": [dict(t) for t in tasks],
            "commitments": [dict(c) for c in commitments],
            "claims": [dict(c) for c in claims],
            "entities": [], "relations": []}


class _Base(unittest.TestCase):
    """Shared fixture: temp store + extractor + deterministic patches."""

    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        self.store = _mk_store()
        self.addCleanup(self._close)
        self.ex = Extractor(store=self.store)
        self.now = time.time()
        self.indexed: list[tuple] = []
        self.offered: list[tuple] = []

    def _close(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _user(self, name="Hugh"):
        return patch("app.services.identity.user_identity",
                     return_value={"name": name, "source": "profile"})

    # Turn text carries every fixture span verbatim so the hygiene gate's
    # span-faithfulness check passes (same trick as the 2.1 fixtures).
    _TURN_TEXT = ("I'll send the pricing deck. I'll follow up with Eve. "
                  "I prefer morning meetings. the demo is on Friday.")

    def _persist(self, speaker: str, facts: dict, start: float = 1000.0):
        turn = Turn(start=start, end=start + 1, speaker=speaker,
                    text=self._TURN_TEXT, event_ids=[], n_utterances=1)
        with patch.object(self.ex, "_persist_entities", return_value=(0, 0)), \
             patch.object(self.ex, "_record_faithfulness", return_value=None), \
             patch.object(self.ex, "_persist_claim_belief", return_value=None), \
             patch("app.services.extractor._index_fact",
                   lambda st, fid, kind, text, ts:
                       self.indexed.append((fid, kind, text))), \
             patch("app.services.task_offer.offer_task",
                   lambda text, conf, fid: self.offered.append((fid, text))), \
             patch("app.services.fact_gate._similar_active", return_value=[]), \
             patch("app.services.people_pipeline.enabled", return_value=False):
            self.ex._persist(turn, facts, self.now)

    # -- row helpers ------------------------------------------------------
    def _fact_rows(self):
        return [dict(r) for r in self.store._conn.execute(
            "SELECT id, kind, state, speaker_track_id, review, confidence "
            "FROM facts ORDER BY id").fetchall()]

    def _task_row(self):
        return dict(self.store._conn.execute(
            "SELECT t.fact_id, t.text, t.status, t.owner_person_id, "
            "t.owner_track_id, f.state FROM tasks t "
            "JOIN facts f ON f.id = t.fact_id").fetchone())

    def _commitment_row(self):
        return dict(self.store._conn.execute(
            "SELECT c.fact_id, c.text, c.status, c.from_person_id, "
            "c.to_person_id, c.from_track_id, f.state FROM commitments c "
            "JOIN facts f ON f.id = c.fact_id").fetchone())

    def _tracks(self):
        return [dict(r) for r in self.store._conn.execute(
            "SELECT * FROM speaker_tracks ORDER BY id").fetchall()]

    def _jobs(self, kind=people_escrow.JOB_KIND):
        return [dict(r) for r in self.store._conn.execute(
            "SELECT * FROM jobs WHERE kind = ? ORDER BY id",
            (kind,)).fetchall()]


class FlagOffTests(_Base):
    """Default posture: byte-identical to today, no escrow schema activity."""

    def test_flag_defaults_off(self):
        self.assertFalse(people_escrow.enabled())

    def test_unbound_me_still_returns_none(self):
        with self._user("Hugh"):
            self.assertIsNone(self.ex._resolve_person_id(
                "me", self.now, turn_speaker="", text="I'll send it"))
            self.assertIsNone(self.ex._resolve_person_id(
                "me", self.now, turn_speaker="unknown speaker",
                text="I'll send it"))

    def test_no_escrow_no_tracks_task_stays_visible(self):
        with self._user("Hugh"):
            self._persist("Speaker 7", _facts(
                tasks=[_TASK], commitments=[_COMMITMENT],
                claims=[_CLAIM_FIRST_PERSON]))
        self.assertEqual(self._tracks(), [])
        self.assertEqual(self._jobs(), [])
        for r in self._fact_rows():
            self.assertEqual(r["state"], "active")
            self.assertIsNone(r["speaker_track_id"])
        # The task surfaces exactly as before (open board + list_facts).
        self.assertEqual(len(self.store.open_tasks()), 1)
        self.assertEqual(
            len(self.store.list_facts(kind="task", status="open")), 1)


class EscrowWriteTests(_Base):
    """Flag ON: unbound-track facts are kept, marked, and kept out of surfaces."""

    def setUp(self):
        super().setUp()
        p = patch("app.services.people_escrow.enabled", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_task_escrowed_with_track_id_and_no_minted_person(self):
        with self._user("Hugh"):
            self._persist("Speaker 7", _facts(tasks=[_TASK]))
        tracks = self._tracks()
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["label"], "Speaker 7")
        self.assertEqual(tracks[0]["status"], "open")
        tid = tracks[0]["id"]
        row = self._task_row()
        self.assertEqual(row["state"], "escrowed")
        self.assertIsNone(row["owner_person_id"])
        self.assertEqual(row["owner_track_id"], tid)
        self.assertEqual(self._fact_rows()[0]["speaker_track_id"], tid)
        # No junk person minted for the provisional label.
        names = [r["canonical_name"] for r in self.store._conn.execute(
            "SELECT canonical_name FROM people").fetchall()]
        self.assertNotIn("Speaker 7", names)

    def test_commitment_escrowed_from_track(self):
        with self._user("Hugh"):
            eve = self.store.resolve_person("Eve", ts=self.now)
            self._persist("Speaker 7", _facts(commitments=[_COMMITMENT]))
        row = self._commitment_row()
        self.assertEqual(row["state"], "escrowed")
        self.assertIsNone(row["from_person_id"])
        self.assertEqual(row["from_track_id"], self._tracks()[0]["id"])
        self.assertEqual(row["to_person_id"], eve)  # named party unaffected

    def test_first_person_claim_escrowed_neutral_claim_not(self):
        with self._user("Hugh"):
            self._persist("Speaker 7", _facts(
                claims=[_CLAIM_FIRST_PERSON, _CLAIM_NEUTRAL]))
        rows = self._fact_rows()
        by_state = {}
        for r in rows:
            by_state.setdefault(r["state"], []).append(r)
        self.assertEqual(len(by_state.get("escrowed", [])), 1)
        self.assertEqual(len(by_state.get("active", [])), 1)
        self.assertIsNone(by_state["active"][0]["speaker_track_id"])
        # Only the non-escrowed claim was semantically indexed.
        self.assertEqual([k for _, k, _ in self.indexed], ["claim"])

    def test_escrowed_rows_out_of_every_default_surface(self):
        with self._user("Hugh"):
            self._persist("Speaker 7", _facts(
                tasks=[_TASK], commitments=[_COMMITMENT],
                claims=[_CLAIM_FIRST_PERSON]))
        # Boards / grounding / home scoring input (list_facts) / reflection.
        self.assertEqual(self.store.open_tasks(), [])
        self.assertEqual(self.store.list_facts(), [])
        self.assertEqual(
            self.store.list_facts(kind="task", status="open",
                                  actionable=True), [])
        self.assertEqual(self.store.search_facts_like("pricing deck"), [])
        self.assertEqual(self.store.facts_since(0.0), [])
        # People-scoring inputs: no person links derive from escrowed rows.
        self.assertEqual(self.store.fact_person_links(), [])
        # Not vector-indexed, no proactive task offer while identity-less.
        self.assertEqual(self.indexed, [])
        self.assertEqual(self.offered, [])
        # But NOT lost: the explicit escrow views still see them.
        self.assertEqual(len(self.store.list_facts(include_escrowed=True)), 3)
        tid = self._tracks()[0]["id"]
        self.assertEqual(len(self.store.escrowed_facts_for_track(tid)), 3)
        status = people_escrow.escrow_status(self.store)
        self.assertEqual(status["tracks"][0]["escrowed_facts"], 3)

    def test_track_stable_across_turns_and_stamped_on_turns(self):
        with self._user("Hugh"):
            self._persist("Speaker 7", _facts(tasks=[_TASK]), start=1000)
            self._persist("Speaker 7", _facts(
                claims=[_CLAIM_FIRST_PERSON]), start=2000)
        tracks = self._tracks()
        self.assertEqual(len(tracks), 1)  # same session, same durable track
        turns = [Turn(start=1000, end=1001, speaker="Speaker 7",
                      text="fixture turn", event_ids=[1], audio_paths=[],
                      n_utterances=1),
                 Turn(start=2000, end=2001, speaker="Hugh",
                      text="hello", event_ids=[2], audio_paths=[],
                      n_utterances=1)]
        self.store.replace_turns(turns)
        rows = [dict(r) for r in self.store._conn.execute(
            "SELECT speaker, speaker_track_id FROM turns "
            "ORDER BY start").fetchall()]
        self.assertEqual(rows[0]["speaker_track_id"], tracks[0]["id"])
        self.assertIsNone(rows[1]["speaker_track_id"])


class RebindTests(_Base):
    def setUp(self):
        super().setUp()
        p = patch("app.services.people_escrow.enabled", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        with self._user("Hugh"):
            self.store.resolve_person("Eve", ts=self.now)
            self._persist("Speaker 7", _facts(
                tasks=[_TASK], commitments=[_COMMITMENT],
                claims=[_CLAIM_FIRST_PERSON]))
        self.track_id = self._tracks()[0]["id"]

    def test_labeling_binds_track_and_enqueues_durable_job(self):
        res = people_escrow.label_speaker(
            self.store, "Speaker 7", "Sarah Chen")
        self.assertTrue(res["ok"])
        track = self.store.get_speaker_track(self.track_id)
        self.assertEqual(track["status"], "bound")
        self.assertEqual(track["bound_person_id"], res["person_id"])
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "pending")
        self.assertEqual(json.loads(jobs[0]["payload"])["track_id"],
                         self.track_id)
        # The bound track is closed: the label's next appearance starts fresh.
        self.assertIsNone(self.store.open_speaker_track("Speaker 7"))

    def test_label_unknown_track_refused(self):
        res = people_escrow.label_speaker(self.store, "Speaker 99", "Sarah")
        self.assertFalse(res["ok"])
        res2 = people_escrow.label_speaker(self.store, "Hugh", "Sarah")
        self.assertFalse(res2["ok"])  # not a provisional label at all

    def test_rebind_rewrites_all_rows_and_is_idempotent(self):
        res = people_escrow.label_speaker(
            self.store, "Speaker 7", "Sarah Chen")
        pid = res["person_id"]
        before_conf = {r["id"]: r["confidence"] for r in self._fact_rows()}
        out = people_escrow.run_rebind_job(
            {"track_id": self.track_id}, store=self.store)
        self.assertEqual((out["facts"], out["tasks"], out["commitments"]),
                         (3, 1, 1))
        task = self._task_row()
        self.assertEqual(task["owner_person_id"], pid)
        self.assertEqual(task["state"], "active")
        cmt = self._commitment_row()
        self.assertEqual(cmt["from_person_id"], pid)
        self.assertEqual(cmt["state"], "active")
        # No tier promotion: review still NULL (same queue as any ASR fact),
        # confidence untouched.
        for r in self._fact_rows():
            self.assertEqual(r["state"], "active")
            self.assertIsNone(r["review"])
            self.assertEqual(r["confidence"], before_conf[r["id"]])
        # Rows now surface everywhere a normal fact would.
        self.assertEqual(len(self.store.open_tasks()), 1)
        self.assertEqual(len(self.store.list_facts()), 3)
        links = self.store.fact_person_links()
        self.assertIn((task["fact_id"], pid, "responsible_for"), links)
        # Audit row recorded what happened.
        log = [dict(r) for r in self.store._conn.execute(
            "SELECT * FROM escrow_rebind_log ORDER BY id").fetchall()]
        self.assertEqual(len(log), 1)
        self.assertEqual((log[0]["track_id"], log[0]["person_id"],
                          log[0]["n_facts"], log[0]["n_tasks"],
                          log[0]["n_commitments"]),
                         (self.track_id, pid, 3, 1, 1))
        # Retry-safety: a re-run (worker retry) changes nothing.
        out2 = people_escrow.run_rebind_job(
            {"track_id": self.track_id}, store=self.store)
        self.assertEqual((out2["facts"], out2["tasks"], out2["commitments"]),
                         (0, 1, 1))  # facts already active; person cols no-op
        self.assertEqual(self._task_row()["owner_person_id"], pid)
        self.assertEqual(len(self.store.list_facts()), 3)

    def test_rebind_indexes_reactivated_facts(self):
        people_escrow.label_speaker(self.store, "Speaker 7", "Sarah Chen")
        indexed = []
        with patch("app.services.extractor._index_fact",
                   lambda st, fid, kind, text, ts:
                       indexed.append((fid, kind))):
            people_escrow.run_rebind_job(
                {"track_id": self.track_id}, store=self.store)
        self.assertEqual(len(indexed), 3)

    def test_rebind_on_unbound_track_is_a_safe_noop(self):
        out = people_escrow.run_rebind_job(
            {"track_id": self.track_id}, store=self.store)
        self.assertIn("skipped", out)
        self.assertEqual(self._task_row()["state"], "escrowed")

    def test_worker_drains_the_durable_job(self):
        from app.services.worker import JobWorker
        people_escrow.label_speaker(self.store, "Speaker 7", "Sarah Chen")
        w = JobWorker(store=self.store, poll_interval_s=0.05, max_attempts=2)
        w.register(people_escrow.JOB_KIND,
                   lambda p: people_escrow.run_rebind_job(p, store=self.store))
        w.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                stats = self.store.job_stats()
                if not stats["pending"] and not stats["running"]:
                    break
                time.sleep(0.05)
        finally:
            w.stop()
        self.assertEqual(self.store.job_stats()["dead"], 0)
        self.assertEqual(self._task_row()["state"], "active")
        self.assertIsNotNone(self._task_row()["owner_person_id"])


class MergeRebindTests(_Base):
    def setUp(self):
        super().setUp()
        p = patch("app.services.people_escrow.enabled", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        with self._user("Hugh"):
            self._persist("Speaker 7", _facts(
                tasks=[_TASK], commitments=[_COMMITMENT]))
        self.track_id = self._tracks()[0]["id"]
        res = people_escrow.label_speaker(
            self.store, "Speaker 7", "Sarah Chen")
        self.sarah = res["person_id"]
        people_escrow.run_rebind_job(
            {"track_id": self.track_id}, store=self.store)

    def test_merge_repoints_track_and_enqueues_rebind(self):
        bob = self.store.resolve_person("Bob Kane", ts=self.now)
        self.store.soft_merge_people(bob, self.sarah, reason="same person",
                                     actor="user", ts=self.now)
        track = self.store.get_speaker_track(self.track_id)
        self.assertEqual(track["bound_person_id"], bob)
        jobs = self._jobs()
        # one from labeling (already run manually), one from the merge
        self.assertEqual(len(jobs), 2)
        payload = json.loads(jobs[-1]["payload"])
        self.assertEqual(payload["track_id"], self.track_id)
        self.assertEqual(payload["previous_person_id"], self.sarah)
        # Run the merge-triggered rebind: rows follow the merge to Bob.
        people_escrow.run_rebind_job(payload, store=self.store)
        self.assertEqual(self._task_row()["owner_person_id"], bob)
        self.assertEqual(self._commitment_row()["from_person_id"], bob)
        self.assertEqual(self._task_row()["state"], "active")

    def test_merge_hook_is_silent_when_flag_off(self):
        with patch("app.services.people_escrow.enabled", return_value=False):
            bob = self.store.resolve_person("Bob Kane", ts=self.now)
            n_jobs = len(self._jobs())
            self.store.soft_merge_people(bob, self.sarah, reason="x",
                                         actor="user", ts=self.now)
            self.assertEqual(len(self._jobs()), n_jobs)
            track = self.store.get_speaker_track(self.track_id)
            self.assertEqual(track["bound_person_id"], self.sarah)


if __name__ == "__main__":
    unittest.main()
