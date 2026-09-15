"""Memory Console Episodes view — CAL timeline in the product."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.events import Event, Modality
from app.main import app
from app.storage import Store
import app.api.routes as routes_mod


class ConsoleEpisodesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_ep_api_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "a")
        self.eid = self.store.resolve_entity("Boostrun", "org", ts=time.time())
        self.t0 = time.time() - 3600
        for i in range(12):
            self.store.insert(Event(
                time=self.t0 + i * 30, modality=Modality.INPUT,
                raw="click", summary="click", source="desktop.click",
                meta={"window": "Boostrun plan - Google Docs - Chromium",
                      "identifiers": []}))
        self.client = TestClient(app)
        self._patch = mock.patch.object(routes_mod.memory, "_ensure_store",
                                        return_value=self.store)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def test_episodes_names_known_work(self) -> None:
        r = self.client.get("/console/episodes",
                            params={"since": self.t0 - 1})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertGreaterEqual(body["count"], 1)
        titles = [e["title"] for e in body["episodes"] if e.get("node_type")]
        self.assertIn("Boostrun", titles)

    def test_blank_stays_blank_without_anchors(self) -> None:
        empty = Store(db_path=self.tmp / "e.db", audio_dir=self.tmp / "ea")
        for i in range(8):
            empty.insert(Event(
                time=self.t0 + i * 30, modality=Modality.VISION,
                raw="x", summary="x", source="desktop.screen",
                meta={"window": "", "identifiers": []}))
        with mock.patch.object(routes_mod.memory, "_ensure_store",
                               return_value=empty):
            r = self.client.get("/console/episodes",
                                params={"since": self.t0 - 1})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["named"], 0)
        self.assertTrue(all(e["node_type"] is None for e in body["episodes"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
