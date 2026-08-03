"""Regression: screen_extract must not starve behind a webcam backlog.

Live bug (July 20 2026): unextracted_events(modality='vision') returned the
80 OLDEST unextracted vision events — all vision.claude webcam frames, which
nothing ever marks extracted — and screen_extract's Python-side source filter
then found zero desktop.screen frames in the window. Result: 1,390 screen
frames (including a meeting invite on screen) were never mined, while the
job ran "successfully" every few seconds forever."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.storage import Store

NOW = 1_000_000_000.0


def _vision(t, source, raw="x" * 200):
    return Event(time=t, modality=Modality.VISION, raw=raw, source=source)


class UnextractedSourceFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_source_filter_beats_older_backlog(self):
        # 50 old webcam frames that nothing will ever mark...
        for i in range(50):
            self.store.insert(_vision(NOW + i, "vision.claude"))
        # ...and 3 newer screen frames behind them.
        screen_ids = [self.store.insert(_vision(NOW + 100 + i, "desktop.screen"))
                      for i in range(3)]
        # The window is smaller than the webcam backlog: without the SQL
        # source filter, screen frames never appear.
        rows = self.store.unextracted_events(limit=10, modality="vision",
                                             source="desktop.screen")
        self.assertEqual([eid for eid, _ in rows], screen_ids)
        # And unfiltered still behaves as before (oldest-first, all sources).
        rows = self.store.unextracted_events(limit=10, modality="vision")
        self.assertTrue(all(ev.source == "vision.claude" for _, ev in rows))


class FreshLaneTests(unittest.TestCase):
    """New information must not wait behind a backlog drain: frames younger
    than FRESH_S are processed first; the backlog keeps draining after."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_fresh_frames_jump_the_backlog_then_backlog_drains(self):
        import time as _time
        from app.services import screen_extract
        now = _time.time()
        for i in range(30):    # stale backlog, hours old
            self.store.insert(_vision(now - 7200 + i, "desktop.screen"))
        fresh = [self.store.insert(_vision(now - 5 + i, "desktop.screen"))
                 for i in range(2)]
        with patch.object(screen_extract, "_extract", return_value={}), \
             patch("app.services.documents._persist_facts", return_value=0), \
             patch("app.storage.get_store", return_value=self.store):
            first = screen_extract.run_once()
            second = screen_extract.run_once()
        self.assertEqual(first["lane"], "fresh")
        self.assertEqual(first["events"], len(fresh))
        self.assertTrue(first["remaining"])          # backlog still there
        self.assertEqual(second["lane"], "backlog")  # ...and drains next
        self.assertGreater(second["events"], 0)
        # The fresh frames really are the ones marked by the first pass.
        left = {eid for eid, _ in self.store.unextracted_events(
            limit=200, modality="vision", source="desktop.screen")}
        self.assertTrue(left.isdisjoint(fresh))


class ScreenExtractProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_run_once_reaches_screen_frames_behind_webcam_backlog(self):
        from app.services import screen_extract
        for i in range(100):   # backlog larger than the BATCH*8 window
            self.store.insert(_vision(NOW + i, "vision.claude"))
        sid = self.store.insert(_vision(
            NOW + 500, "desktop.screen",
            raw="[Outlook] From: Falloon, Chris  Subject: DTC PortDev Weekly"
                "  When: Monday, July 20, 2026 10:00 AM"))
        with patch.object(screen_extract, "_extract", return_value={}), \
             patch("app.services.documents._persist_facts", return_value=0), \
             patch("app.storage.get_store", return_value=self.store):
            res = screen_extract.run_once()
        self.assertEqual(res["events"], 1)   # the screen frame WAS selected
        rows = self.store.unextracted_events(modality="vision",
                                             source="desktop.screen")
        self.assertEqual(rows, [])           # ...and marked, so no re-grind
        # The webcam backlog is untouched — not screen_extract's to consume.
        self.assertEqual(
            len(self.store.unextracted_events(limit=200, modality="vision")), 100)
        _ = sid


if __name__ == "__main__":
    unittest.main()
