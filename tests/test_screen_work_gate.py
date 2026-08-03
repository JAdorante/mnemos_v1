"""Screen-mined "work" needs a human verdict before it counts as real work.

Live failure (July 20 2026): a screen-extraction backlog drain minted 34 open
tasks/commitments from email subject lines ("Re: TMZ"), slideware, OCR garble,
and the app's own chat UI — and chat answers, the Tasks board, readiness,
anticipation offers, and the constellation all presented them as the user's
real obligations. Weak-attribution sources now sit in the review queue until
approved; the Console still lists them (that's where they get pruned)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import self_profile
from app.storage import Store

NOW = 1_000_000_000.0


class ActionableFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")
        self.screen_ev = self.store.insert(Event(
            time=NOW, modality=Modality.VISION, raw="[Draft] Re: TMZ",
            source="desktop.screen"))
        self.audio_ev = self.store.insert(Event(
            time=NOW, modality=Modality.AUDIO, raw="call the vet tomorrow",
            source="audio.whisper"))

    def test_unreviewed_screen_task_is_quarantined(self):
        junk = self.store.add_task("Re: TMZ", source_event_id=self.screen_ev,
                                   extracted_at=NOW)
        real = self.store.add_task("call the vet", source_event_id=self.audio_ev,
                                   extracted_at=NOW)
        board = self.store.list_facts(kind="task", status="open",
                                      actionable=True)
        self.assertEqual([f["fact_id"] for f in board], [real])
        # The Console (no `actionable`) still sees it — the review surface.
        console = self.store.list_facts(kind="task", status="open")
        self.assertEqual({f["fact_id"] for f in console}, {junk, real})

    def test_approval_lifts_screen_task_onto_the_board(self):
        fid = self.store.add_task("update Visitor Queue properties",
                                  source_event_id=self.screen_ev,
                                  extracted_at=NOW)
        self.assertEqual(self.store.list_facts(kind="task", actionable=True), [])
        self.store.review_fact(fid, "approved")
        board = self.store.list_facts(kind="task", actionable=True)
        self.assertEqual([f["fact_id"] for f in board], [fid])

    def test_screen_commitments_gated_screen_claims_are_not(self):
        c = self.store.add_commitment("Re: TMZ", source_event_id=self.screen_ev,
                                      extracted_at=NOW)
        cl = self.store.add_claim("Julia Beech works at Dell",
                                  source_event_id=self.screen_ev,
                                  extracted_at=NOW)
        rows = self.store.list_facts(actionable=True)
        ids = {f["fact_id"] for f in rows}
        self.assertNotIn(c, ids)     # unreviewed screen commitment: quarantined
        self.assertIn(cl, ids)       # claims are context, not work — untouched


class WorkListEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_board_quarantines_and_counts_screen_work(self):
        ev = self.store.insert(Event(
            time=NOW, modality=Modality.VISION, raw="x",
            source="desktop.screen"))
        junk = self.store.add_task("Re: TMZ", source_event_id=ev,
                                   extracted_at=NOW)
        d = self.client.get("/work/list").json()
        self.assertEqual(d["open"], [])
        self.assertEqual(d["screen_pending"], 1)
        self.store.review_fact(junk, "approved")
        d = self.client.get("/work/list").json()
        self.assertEqual(len(d["open"]), 1)
        self.assertEqual(d["screen_pending"], 0)


if __name__ == "__main__":
    unittest.main()
