"""Unit tests for the multimodal activity enrichment (app.services.activity).

Grouping stays desktop-only; join_context is a pure post-grouping join that
attaches co-timed audio transcripts + webcam vision events to each block and
folds them into the summary as "heard: ..." / "saw: ..." segments. These tests
cover the join window, the never-splits invariant, the summary segments, the
storage round-trip of the new columns, and the schema migration of an
old-shape (desktop-only) activities table.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.events import Event, Modality
from app.services import activity
from app.storage import Store


def _screen(t: float, window: str = "", desc: str = "", eid: int = 0) -> tuple[int, Event]:
    meta: dict = {"surface": "desktop"}
    if window:
        meta["window"] = window
    if desc:
        meta["vision"] = {"description": desc}
    summary = f"[{window}] {desc}" if window else (desc or "[desktop screen captured]")
    return eid, Event(time=t, modality=Modality.VISION, raw=desc or summary,
                      summary=summary, source="desktop.screen", meta=meta)


def _audio(t: float, text: str = "", eid: int = 0,
           source: str = "audio.whisper") -> tuple[int, Event]:
    return eid, Event(time=t, modality=Modality.AUDIO, raw=text, summary=text,
                      source=source, meta={})


def _webcam(t: float, desc: str = "", eid: int = 0) -> tuple[int, Event]:
    meta: dict = {}
    if desc:
        meta["vision"] = {"description": desc}
    return eid, Event(time=t, modality=Modality.VISION,
                      raw=desc or "[frame captured]",
                      summary=desc or "[frame captured]",
                      source="vision.claude", meta=meta)


class JoinContextTests(unittest.TestCase):
    def _one_act(self, start: float = 100, end: float = 200) -> list:
        rows = [_screen(start, "a.py - Cursor", "editing a.py", eid=1),
                _screen(end, "a.py - Cursor", "still editing", eid=2)]
        return activity.group_activities(rows, max_gap_s=300)

    def test_in_window_events_attach_and_out_of_window_do_not(self) -> None:
        acts = self._one_act(100, 200)
        audio = [_audio(99.9, "too early", eid=10),
                 _audio(150, "call the vendor tomorrow", eid=11),
                 _audio(200.0, "boundary is inclusive", eid=12),
                 _audio(200.1, "too late", eid=13)]
        webcam = [_webcam(150, "person at desk writing notes", eid=20),
                  _webcam(500, "empty room", eid=21)]
        activity.join_context(acts, audio, webcam, max_heard=3, max_saw=2)
        a = acts[0]
        self.assertEqual(a.n_audio, 2)
        self.assertEqual(a.n_webcam, 1)
        self.assertEqual(sorted(a.ctx_event_ids), [11, 12, 20])
        # Desktop provenance is untouched and distinguishable.
        self.assertEqual(a.event_ids, [1, 2])

    def test_summary_gains_heard_and_saw_segments(self) -> None:
        acts = self._one_act()
        activity.join_context(
            acts,
            [_audio(150, "call the vendor tomorrow", eid=11)],
            [_webcam(160, "person at desk writing notes", eid=20)],
            max_heard=3, max_saw=2)
        s = acts[0].summary
        self.assertIn("heard: call the vendor tomorrow", s)
        self.assertIn("saw: person at desk writing notes", s)
        # The desktop head is preserved.
        self.assertIn("Cursor", s)
        self.assertIn("editing a.py", s)

    def test_no_context_leaves_summary_without_segments(self) -> None:
        acts = self._one_act()
        before = acts[0].summary
        activity.join_context(acts, [], [], max_heard=3, max_saw=2)
        a = acts[0]
        self.assertEqual(a.summary, before)
        self.assertNotIn("heard:", a.summary)
        self.assertNotIn("saw:", a.summary)
        self.assertEqual((a.n_audio, a.n_webcam, a.ctx_event_ids), (0, 0, []))

    def test_transcriptless_audio_counts_but_adds_no_heard_text(self) -> None:
        acts = self._one_act()
        activity.join_context(
            acts, [_audio(150, "", eid=11, source="audio.skipped")], [],
            max_heard=3, max_saw=2)
        a = acts[0]
        self.assertEqual(a.n_audio, 1)
        self.assertNotIn("heard:", a.summary)

    def test_undescribed_webcam_frame_adds_no_saw_text(self) -> None:
        acts = self._one_act()
        activity.join_context(acts, [], [_webcam(150, "", eid=20)],
                              max_heard=3, max_saw=2)
        a = acts[0]
        self.assertEqual(a.n_webcam, 1)
        self.assertNotIn("saw:", a.summary)

    def test_heard_snippets_dedupe_and_cap(self) -> None:
        acts = self._one_act()
        audio = [_audio(110 + i, "same phrase", eid=30 + i) for i in range(3)]
        audio += [_audio(150 + i, f"unique phrase {i}", eid=40 + i)
                  for i in range(5)]
        activity.join_context(acts, audio, [], max_heard=2, max_saw=2)
        s = acts[0].summary
        self.assertEqual(s.count("same phrase"), 1)
        self.assertIn("unique phrase 0", s)
        self.assertNotIn("unique phrase 1", s)  # capped at max_heard=2

    def test_summary_cap_is_respected_with_context(self) -> None:
        rows = [_screen(100 + i, "a - App", f"note {i} " + "x" * 400, eid=i)
                for i in range(6)]
        acts = activity.group_activities(rows, max_gap_s=300)
        audio = [_audio(101 + i, f"spoken {i} " + "y" * 200, eid=50 + i)
                 for i in range(4)]
        activity.join_context(acts, audio, [], max_heard=3, max_saw=2)
        self.assertLessEqual(len(acts[0].summary), 700)

    def test_join_never_creates_or_splits_activities(self) -> None:
        # A long audio/webcam-only stretch between desktop events must not
        # fracture the block, and context outside any block creates nothing.
        rows = [_screen(100, "a.py - Cursor", "code", eid=1),
                _screen(150, "a.py - Cursor", "code", eid=2)]
        acts = activity.group_activities(rows, max_gap_s=300)
        self.assertEqual(len(acts), 1)
        audio = [_audio(120 + i, f"talk {i}", eid=60 + i) for i in range(10)]
        audio += [_audio(9_000, "lonely audio far away", eid=99)]
        out = activity.join_context(acts, audio, [], max_heard=1, max_saw=1)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].start, out[0].end), (100, 150))


class MultimodalRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="quill_actmm_")
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_audio_only_stretch_produces_no_activity(self) -> None:
        for i in range(5):
            self.store.insert(_audio(100 + i * 10, f"talking {i}")[1])
        self.assertEqual(activity.rebuild(self.store), 0)
        self.assertEqual(self.store.activity_count(), 0)

    def test_rebuild_enriches_and_roundtrips_new_columns(self) -> None:
        self.store.insert(_screen(100, "a.py - Cursor", "editing tests")[1])
        self.store.insert(_screen(160, "a.py - Cursor", "running tests")[1])
        self.store.insert(_audio(120, "remember to push the branch")[1])
        self.store.insert(_webcam(130, "person typing at a laptop")[1])
        self.store.insert(_audio(999_999, "way outside the block")[1])
        n = activity.rebuild(self.store)
        self.assertEqual(n, 1)
        row = self.store.recent_activities(10)[0]
        self.assertEqual(row["n_audio"], 1)
        self.assertEqual(row["n_webcam"], 1)
        self.assertEqual(len(row["ctx_event_ids"]), 2)
        self.assertIn("heard: remember to push the branch", row["summary"])
        self.assertIn("saw: person typing at a laptop", row["summary"])
        # describe_recent carries the enriched summary automatically.
        lines = activity.describe_recent(self.store, limit=3)
        self.assertIn("heard:", lines[0])

    def test_replace_activities_defaults_for_plain_blocks(self) -> None:
        acts = activity.group_activities(
            [_screen(100, "a.py - Cursor", "code", eid=1)], max_gap_s=300)
        self.store.replace_activities(acts)
        row = self.store.recent_activities(1)[0]
        self.assertEqual((row["n_audio"], row["n_webcam"]), (0, 0))
        self.assertEqual(row["ctx_event_ids"], [])


class SchemaMigrationTests(unittest.TestCase):
    def test_old_shape_activities_table_gains_new_columns(self) -> None:
        tmp = tempfile.mkdtemp(prefix="quill_actmig_")
        db_path = Path(tmp) / "old.db"
        # Simulate a live DB created before the multimodal join shipped:
        # the activities table exists with the old (desktop-only) shape.
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE activities (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                start     REAL    NOT NULL,
                end       REAL    NOT NULL,
                app       TEXT,
                windows   TEXT,
                summary   TEXT    NOT NULL,
                event_ids TEXT,
                n_screens INTEGER,
                n_clicks  INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO activities (start, end, app, windows, summary, "
            "event_ids, n_screens, n_clicks) "
            "VALUES (100, 200, 'Cursor', '[]', 'old row', '[1]', 1, 0)")
        conn.commit()
        conn.close()
        store = Store(db_path=db_path, audio_dir=Path(tmp) / "audio")
        cols = {r["name"] for r in
                store._conn.execute("PRAGMA table_info(activities)").fetchall()}
        self.assertLessEqual({"n_audio", "n_webcam", "ctx_event_ids"}, cols)
        # The pre-migration row is readable (NULL new columns -> defaults) ...
        row = store.recent_activities(10)[0]
        self.assertEqual(row["summary"], "old row")
        self.assertEqual((row["n_audio"], row["n_webcam"]), (0, 0))
        self.assertEqual(row["ctx_event_ids"], [])
        # ... and the new write path works against the migrated table.
        acts = activity.group_activities(
            [_screen(300, "b.py - Cursor", "code", eid=2),
             _screen(320, "b.py - Cursor", "code", eid=4)], max_gap_s=300)
        activity.join_context(acts, [_audio(310, "hello there", eid=3)], [],
                              max_heard=3, max_saw=2)
        store.replace_activities(acts)
        row = store.recent_activities(1)[0]
        self.assertEqual(row["n_audio"], 1)
        self.assertEqual(row["ctx_event_ids"], [3])


if __name__ == "__main__":
    unittest.main()
