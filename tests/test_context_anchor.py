"""WS1 — context anchors: activity/meeting context for a time window.

Window overlap math, share computation, the raw-event fallback when no
activity block has been rolled up yet, empty-capture safety, bind-only
entity resolution (unknowns stay candidates, never minted), and the
derived-edge threshold."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import context_anchor as ca
from app.services.activity import Activity
from app.storage import Store

NOW = 1_700_000_000.0


def _block(start, end, app, windows):
    a = Activity(start=start, end=end, app=app)
    a.windows = list(windows)
    return a


class AnchorTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.nexus = self.store.resolve_entity("Nexus V1", "project", ts=NOW)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    # ------------------------- pure helpers -------------------------------
    def test_title_candidates(self):
        self.assertEqual(
            ca.title_candidates("storage.py - nexus_v1 - Cursor"),
            ["nexus_v1"])
        self.assertEqual(ca.title_candidates("Inbox - Outlook"), ["Inbox"])
        self.assertEqual(ca.title_candidates(""), [])
        # Browser suffix stripped → single segment left → no candidates.
        self.assertEqual(
            ca.title_candidates("Quarterly plan - Google Chrome"), [])

    # ------------------------- share math ---------------------------------
    def test_share_from_activity_blocks(self):
        self.store.replace_activities([
            _block(NOW, NOW + 72, "Cursor",
                   ["storage.py - nexus_v1 - Cursor"]),
            _block(NOW + 72, NOW + 100, "Slack", ["general - Slack"]),
        ])
        anchors = ca.anchors_for_window(self.store, NOW, NOW + 100)
        apps = {a["app"]: a["share"] for a in anchors["apps"]}
        self.assertAlmostEqual(apps["Cursor"], 0.72, places=2)
        self.assertAlmostEqual(apps["Slack"], 0.28, places=2)
        self.assertEqual(anchors["apps"][0]["app"], "Cursor")

    def test_min_share_filters_slivers(self):
        self.store.replace_activities([
            _block(NOW, NOW + 95, "Cursor", ["a - nexus_v1 - Cursor"]),
            _block(NOW + 95, NOW + 100, "Slack", ["general - Slack"]),
        ])
        anchors = ca.anchors_for_window(self.store, NOW, NOW + 100)
        self.assertEqual([a["app"] for a in anchors["apps"]], ["Cursor"])

    def test_fallback_to_raw_events_when_no_block(self):
        for i in range(3):
            self.store.insert(Event(
                time=NOW + i * 10, modality=Modality.VISION, raw="",
                source="desktop.screen",
                meta={"window": "storage.py - nexus_v1 - Cursor"}))
        self.store.insert(Event(
            time=NOW + 30, modality=Modality.VISION, raw="",
            source="desktop.screen", meta={"window": "general - Slack"}))
        anchors = ca.anchors_for_window(self.store, NOW, NOW + 40)
        self.assertTrue(anchors["apps"])
        self.assertEqual(anchors["apps"][0]["app"], "Cursor")
        self.assertAlmostEqual(anchors["apps"][0]["share"], 0.75, places=2)

    def test_empty_capture_returns_empty_anchors(self):
        anchors = ca.anchors_for_window(self.store, NOW, NOW + 60)
        self.assertEqual(anchors, {"apps": [], "meeting": None,
                                   "entities": []})

    def test_kill_switch(self):
        import os
        with patch.dict(os.environ, {"QUILL_CONTEXT_ANCHOR": "0"}):
            anchors = ca.anchors_for_window(self.store, NOW, NOW + 60)
        self.assertEqual(anchors["apps"], [])

    # ------------------------- entities -----------------------------------
    def test_window_entity_resolves_bind_only(self):
        self.store.replace_activities([
            _block(NOW, NOW + 100, "Cursor",
                   ["storage.py - nexus_v1 - Cursor"]),
        ])
        before = len(self.store.all_entities())
        anchors = ca.anchors_for_window(self.store, NOW, NOW + 100)
        ents = anchors["entities"]
        hit = next(e for e in ents if e.get("entity_id"))
        self.assertEqual(hit["entity_id"], self.nexus)
        self.assertEqual(hit["name"], "Nexus V1")
        self.assertEqual(hit["source"], "window_title")
        self.assertEqual(len(self.store.all_entities()), before)

    def test_unknown_candidate_never_minted(self):
        self.store.replace_activities([
            _block(NOW, NOW + 100, "Cursor",
                   ["main.rs - zephyr-engine - Cursor"]),
        ])
        before = len(self.store.all_entities())
        anchors = ca.anchors_for_window(self.store, NOW, NOW + 100)
        cand = next(e for e in anchors["entities"]
                    if e["name"] == "zephyr-engine")
        self.assertIsNone(cand["entity_id"])
        self.assertEqual(len(self.store.all_entities()), before)

    def test_meeting_title_names_entity(self):
        with patch("app.services.meeting_session.current",
                   return_value={"title": "Nexus V1 weekly", "id": 7,
                                 "status": "active", "attendees": []}):
            anchors = ca.anchors_for_window(self.store, NOW, NOW + 60)
        self.assertEqual(anchors["meeting"]["title"], "Nexus V1 weekly")
        self.assertEqual(anchors["meeting"]["session_id"], 7)
        hit = next(e for e in anchors["entities"] if e.get("entity_id"))
        self.assertEqual(hit["entity_id"], self.nexus)
        self.assertEqual(hit["source"], "meeting_title")

    def test_ocr_identifiers_outrank_titles(self):
        self.store.replace_activities([
            _block(NOW, NOW + 100, "Cursor", ["x - nexus_v1 - Cursor"]),
        ])
        self.store.insert(Event(
            time=NOW + 5, modality=Modality.VISION, raw="",
            source="desktop.screen",
            meta={"window": "x - nexus_v1 - Cursor",
                  "identifiers": [{"kind": "repo",
                                   "value": "JAdorante/nexus_v1",
                                   "norm": "nexus_v1"}]}))
        anchors = ca.anchors_for_window(self.store, NOW, NOW + 100)
        hit = next(e for e in anchors["entities"]
                   if e.get("entity_id") == self.nexus)
        self.assertEqual(hit["source"], "identifier")
        self.assertEqual(hit["score"], 1.0)

    # ------------------------- derived edges ------------------------------
    def test_derived_entities_respect_share_floor(self):
        anchors = {"entities": [
            {"name": "Nexus V1", "entity_id": self.nexus,
             "source": "window_title", "score": 0.72},
            {"name": "Weak", "entity_id": 99, "source": "window_title",
             "score": 0.3},
            {"name": "zephyr", "entity_id": None, "source": "window_title",
             "score": 0.9},
        ]}
        hits = ca.derived_context_entities(anchors)
        self.assertEqual([h["id"] for h in hits], [self.nexus])

    # ------------------------- prompt block --------------------------------
    def test_prompt_block_bounded_and_hint_only(self):
        anchors = {
            "apps": [{"app": "Cursor",
                      "window": "storage.py - nexus_v1 - Cursor",
                      "share": 0.72}],
            "meeting": {"title": "VenturePulse sync", "session_id": 1,
                        "attendees": []},
            "entities": [{"name": "Nexus V1", "entity_id": self.nexus,
                          "source": "window_title", "score": 0.72},
                         {"name": "junk", "entity_id": None,
                          "source": "window_title", "score": 0.9}],
        }
        block = ca.prompt_block(anchors)
        self.assertIn("may be irrelevant", block)
        self.assertIn('active meeting: "VenturePulse sync"', block)
        self.assertIn("Cursor", block)
        self.assertIn("Nexus V1", block)
        self.assertNotIn("junk", block, "unresolved candidates stay out")

    def test_prompt_block_empty_for_empty_anchors(self):
        self.assertEqual(
            ca.prompt_block({"apps": [], "meeting": None, "entities": []}),
            "")


if __name__ == "__main__":
    unittest.main()
