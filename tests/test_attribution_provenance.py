"""Attribution provenance — typed rows keep their mention-ledger trail, and
the late re-resolution job flows later identity evidence back onto facts.

A task whose owner resolution was left open used to be a dead end: NULL
owner, no marker that attribution was attempted. Now the typed row records
(owner_mention_id, owner_resolution_confidence), and `reresolve_open_mentions`
re-runs those mentions bind-only after alias rules / merges / new people —
filling still-NULL person columns without ever minting.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.events import Event, Modality
from app.services import people_pipeline as pp
from app.storage import Store

NOW = 1_000_000_000.0


def _audio(text: str, t: float = NOW) -> Event:
    return Event(time=t, modality=Modality.AUDIO, raw=text,
                 source="audio.whisper", confidence=0.9, meta={})


class ProvenanceColumnTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_add_task_persists_mention_and_confidence(self):
        fid = self.store.add_task(
            "call marc back", owner_person_id=None,
            owner_mention_id=42, owner_resolution_confidence=0.41,
            extracted_at=NOW)
        row = self.store._conn.execute(
            "SELECT owner_mention_id, owner_resolution_confidence "
            "FROM tasks WHERE fact_id=?", (fid,)).fetchone()
        self.assertEqual(int(row["owner_mention_id"]), 42)
        self.assertAlmostEqual(row["owner_resolution_confidence"], 0.41)

    def test_add_commitment_persists_party_provenance(self):
        fid = self.store.add_commitment(
            "send the deck", from_person_id=None, to_person_id=None,
            from_mention_id=7, from_resolution_confidence=0.3,
            to_mention_id=8, to_resolution_confidence=0.2,
            extracted_at=NOW)
        row = self.store._conn.execute(
            "SELECT from_mention_id, from_resolution_confidence, "
            "to_mention_id, to_resolution_confidence "
            "FROM commitments WHERE fact_id=?", (fid,)).fetchone()
        self.assertEqual(int(row["from_mention_id"]), 7)
        self.assertEqual(int(row["to_mention_id"]), 8)
        self.assertAlmostEqual(row["from_resolution_confidence"], 0.3)
        self.assertAlmostEqual(row["to_resolution_confidence"], 0.2)

    def test_defaults_stay_null(self):
        fid = self.store.add_task("untracked", extracted_at=NOW)
        row = self.store._conn.execute(
            "SELECT owner_mention_id, owner_resolution_confidence "
            "FROM tasks WHERE fact_id=?", (fid,)).fetchone()
        self.assertIsNone(row["owner_mention_id"])
        self.assertIsNone(row["owner_resolution_confidence"])


class ReresolveOpenMentionsTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _open_mention_task(self, name: str = "Marc",
                           text: str = "marc will send the deck"):
        """Speak a single-token unknown name → leave_open mention + unowned
        task that records the mention (the new persist-path behavior)."""
        eid = self.store.insert(_audio(text))
        res = pp.resolve_person_mention(
            name, store=self.store, event_id=eid,
            event_source="audio.whisper", text=text, now=NOW)
        self.assertIsNone(res.person_id)
        self.assertIsNotNone(res.mention_id)
        fid = self.store.add_task(
            "send the deck", source_event_id=eid,
            owner_person_id=None, owner_mention_id=res.mention_id,
            owner_resolution_confidence=res.confidence, extracted_at=NOW)
        return eid, res.mention_id, fid

    def test_open_mention_rebinds_after_alias_rule(self):
        _, mid, fid = self._open_mention_task()
        # Later: the person exists and a human merge minted a positive alias.
        pid = self.store.insert_person("Marc Ellison", ts=NOW)
        self.store.add_alias_rule(pid, "Marc", "positive", ts=NOW)

        out = pp.reresolve_open_mentions(self.store, now=NOW + 60)
        self.assertTrue(out["ok"])
        self.assertEqual(out["bound"], 1)
        self.assertEqual(out["tasks"], 1)

        row = self.store._conn.execute(
            "SELECT owner_person_id, owner_resolution_confidence "
            "FROM tasks WHERE fact_id=?", (fid,)).fetchone()
        self.assertEqual(int(row["owner_person_id"]), pid)
        self.assertGreater(row["owner_resolution_confidence"], 0.9)
        mrow = self.store._conn.execute(
            "SELECT resolution_status, resolved_person_id "
            "FROM person_mentions WHERE mention_id=?", (mid,)).fetchone()
        self.assertEqual(mrow["resolution_status"], "resolved")
        self.assertEqual(int(mrow["resolved_person_id"]), pid)

    def test_reresolve_is_bind_only_and_idempotent(self):
        self._open_mention_task(name="dana",
                                text="dana said she can review it")
        before = len(self.store.all_people())
        out = pp.reresolve_open_mentions(self.store, now=NOW + 60)
        self.assertTrue(out["ok"])
        self.assertEqual(out["bound"], 0)
        # Bind-only: the sweep never mints a person from a stale name.
        self.assertEqual(len(self.store.all_people()), before)
        # And running again is a no-op, not an error.
        out2 = pp.reresolve_open_mentions(self.store, now=NOW + 120)
        self.assertEqual(out2["bound"], 0)

    def test_human_set_owner_is_never_overwritten(self):
        _, mid, fid = self._open_mention_task()
        other = self.store.insert_person("Someone Else", ts=NOW)
        # A human (or the escrow rebind) set the owner since.
        self.store._conn.execute(
            "UPDATE tasks SET owner_person_id=? WHERE fact_id=?",
            (other, fid))
        self.store._conn.commit()
        pid = self.store.insert_person("Marc Ellison", ts=NOW)
        self.store.add_alias_rule(pid, "Marc", "positive", ts=NOW)
        pp.reresolve_open_mentions(self.store, now=NOW + 60)
        row = self.store._conn.execute(
            "SELECT owner_person_id FROM tasks WHERE fact_id=?",
            (fid,)).fetchone()
        self.assertEqual(int(row["owner_person_id"]), other)


if __name__ == "__main__":
    unittest.main()
