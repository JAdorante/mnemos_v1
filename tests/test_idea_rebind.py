"""WS3 rebind — naming an anonymous voice cluster retro-binds its ideas:
the escrow track path (rebind_speaker_track_rows), the label-only path
(rebind_ideas_by_label), and the durable rebind job end-to-end."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import people_escrow
from app.storage import Store

NOW = 1_700_000_000.0


class IdeaRebindTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.sarah = self.store.resolve_person("Sarah Chen", ts=NOW)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _idea_row(self, fid):
        return dict(self.store._conn.execute(
            "SELECT f.state, i.* FROM facts f JOIN ideas i "
            "ON i.fact_id = f.id WHERE f.id = ?", (fid,)).fetchone())

    def test_escrowed_idea_rebinds_via_track(self):
        track = self.store.get_or_create_speaker_track("Speaker 3", ts=NOW)
        fid = self.store.add_idea(
            "try the hosted cloud path",
            source_span="what if we tried the hosted cloud path",
            confidence=0.8, originator_label="Speaker 3",
            originator_track_id=track, extracted_at=NOW)
        self.store.escrow_fact(fid, track, idea_originator=True)
        self.assertEqual(self._idea_row(fid)["state"], "escrowed")

        self.assertTrue(self.store.bind_speaker_track(track, self.sarah,
                                                      ts=NOW))
        res = self.store.rebind_speaker_track_rows(track, self.sarah, ts=NOW)
        self.assertEqual(res["ideas"], 1)
        row = self._idea_row(fid)
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["originator_person_id"], self.sarah)

    def test_label_only_idea_rebinds_by_label(self):
        fid = self.store.add_idea(
            "switch the pilot pricing",
            source_span="maybe we switch the pilot pricing",
            confidence=0.7, originator_label="Speaker 4", extracted_at=NOW)
        n = self.store.rebind_ideas_by_label("Speaker 4", self.sarah, ts=NOW)
        self.assertEqual(n, 1)
        self.assertEqual(self._idea_row(fid)["originator_person_id"],
                         self.sarah)
        # Idempotent — already-bound rows are never touched again.
        self.assertEqual(
            self.store.rebind_ideas_by_label("Speaker 4", self.sarah,
                                             ts=NOW), 0)

    def test_rebind_job_covers_both_paths(self):
        track = self.store.get_or_create_speaker_track("Speaker 5", ts=NOW)
        fid_escrow = self.store.add_idea(
            "escrowed idea", source_span="escrowed idea", confidence=0.8,
            originator_label="Speaker 5", originator_track_id=track,
            extracted_at=NOW)
        self.store.escrow_fact(fid_escrow, track, idea_originator=True)
        fid_label = self.store.add_idea(
            "label-only idea", source_span="label-only idea", confidence=0.8,
            originator_label="Speaker 5", extracted_at=NOW)
        self.store.bind_speaker_track(track, self.sarah, ts=NOW)
        with patch("app.services.memory.memory.index_fact"):
            out = people_escrow.run_rebind_job({"track_id": track},
                                               store=self.store)
        self.assertEqual(out["ideas"], 2)
        for fid in (fid_escrow, fid_label):
            self.assertEqual(self._idea_row(fid)["originator_person_id"],
                             self.sarah)
        self.assertEqual(self._idea_row(fid_escrow)["state"], "active")


if __name__ == "__main__":
    unittest.main()
