"""Replay ingest route: off by default, lands connector-shaped items with
their own timestamps when on, and refuses unknown connector ids."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.replay_routes import router
from app.storage import Store


class ReplayRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "q.db")
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self._prev = os.environ.get("QUILL_REPLAY_INGEST")
        self.patch = mock.patch("app.storage.get_store", return_value=self.store)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        if self._prev is None:
            os.environ.pop("QUILL_REPLAY_INGEST", None)
        else:
            os.environ["QUILL_REPLAY_INGEST"] = self._prev
        self.store.close()
        self.tmp.cleanup()

    def test_off_by_default(self):
        os.environ.pop("QUILL_REPLAY_INGEST", None)
        r = self.client.post("/replay/items", json={"connector_id": "google", "items": []})
        self.assertEqual(r.status_code, 404)

    def test_lands_with_item_timestamp(self):
        os.environ["QUILL_REPLAY_INGEST"] = "1"
        ts = 1_700_000_000.0
        r = self.client.post("/replay/items", json={"connector_id": "google", "items": [
            {"kind": "mail", "external_id": "m1", "title": "Hello", "body": "Ship date is Dec 12.",
             "ts": ts, "from": "a@x.fake", "to": ["b@x.fake"], "thread_id": "t1"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["landed"], 1)
        rows = self.store.recent_events(limit=5)
        self.assertEqual(len(rows), 1)
        ev = rows[0]
        self.assertEqual(ev["source"], "google.mail")
        self.assertAlmostEqual(float(ev["time"]), ts, places=0)
        meta = ev["meta"] if isinstance(ev["meta"], dict) else __import__("json").loads(ev["meta"])
        self.assertEqual(meta.get("thread_id"), "t1")
        # same item again is deduped by the ledger
        r2 = self.client.post("/replay/items", json={"connector_id": "google", "items": [
            {"kind": "mail", "external_id": "m1", "title": "Hello", "body": "Ship date is Dec 12.", "ts": ts}]})
        self.assertEqual(r2.json()["landed"], 0)

    def test_rejects_unknown_connector(self):
        os.environ["QUILL_REPLAY_INGEST"] = "1"
        r = self.client.post("/replay/items", json={"connector_id": "evil", "items": []})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
