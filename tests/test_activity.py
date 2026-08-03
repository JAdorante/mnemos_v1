"""Unit tests for app.services.activity — folding desktop.screen/desktop.click
events into "what was I doing?" activity blocks (app, focus trail, click trail).

Grouping is a pure function, so most tests feed synthetic (id, Event) rows and
assert the blocks; the last tests round-trip through a temp SQLite store and
run the real rebuild() path.
"""
from __future__ import annotations

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


def _click(t: float, window: str = "", eid: int = 0) -> tuple[int, Event]:
    meta: dict = {"kind": "click", "x": 10, "y": 10, "button": "left",
                  "surface": "desktop"}
    if window:
        meta["window"] = window
    s = f"click left at (10,10) on {window or 'desktop'}"
    return eid, Event(time=t, modality=Modality.INPUT, raw=s, summary=s,
                      source="desktop.click", meta=meta)


class AppOfTests(unittest.TestCase):
    def test_last_dash_segment_is_the_app(self) -> None:
        self.assertEqual(activity.app_of("storage.py - nexus_v1 - Cursor"), "Cursor")
        self.assertEqual(activity.app_of("Inbox — Outlook"), "Outlook")
        self.assertEqual(activity.app_of("doc – Word"), "Word")

    def test_plain_title_is_itself(self) -> None:
        self.assertEqual(activity.app_of("Notepad"), "Notepad")

    def test_empty_falls_back_to_desktop(self) -> None:
        self.assertEqual(activity.app_of(""), "desktop")
        self.assertEqual(activity.app_of("   "), "desktop")


class GroupActivitiesTests(unittest.TestCase):
    def test_same_app_folds_into_one_activity(self) -> None:
        rows = [
            _screen(100, "a.py - Cursor", "editing a.py", eid=1),
            _click(110, "a.py - Cursor", eid=2),
            _screen(130, "b.py - Cursor", "editing b.py", eid=3),
        ]
        acts = activity.group_activities(rows, max_gap_s=300)
        self.assertEqual(len(acts), 1)
        a = acts[0]
        self.assertEqual(a.app, "Cursor")
        self.assertEqual(a.event_ids, [1, 2, 3])
        self.assertEqual(a.n_screens, 2)
        self.assertEqual(a.n_clicks, 1)
        # Focus trail keeps both window titles, first-seen order.
        self.assertEqual(a.windows, ["a.py - Cursor", "b.py - Cursor"])
        self.assertEqual((a.start, a.end), (100, 130))

    def test_app_change_starts_a_new_activity(self) -> None:
        rows = [
            _screen(100, "a.py - Cursor", "code", eid=1),
            _screen(120, "Inbox - Outlook", "reading mail", eid=2),
        ]
        acts = activity.group_activities(rows, max_gap_s=300)
        self.assertEqual([a.app for a in acts], ["Cursor", "Outlook"])

    def test_long_gap_starts_a_new_activity(self) -> None:
        rows = [
            _screen(100, "a.py - Cursor", "code", eid=1),
            _screen(100 + 301, "a.py - Cursor", "code", eid=2),
        ]
        acts = activity.group_activities(rows, max_gap_s=300)
        self.assertEqual(len(acts), 2)

    def test_unsorted_input_is_normalized(self) -> None:
        rows = [
            _screen(130, "a.py - Cursor", "later", eid=2),
            _screen(100, "a.py - Cursor", "earlier", eid=1),
        ]
        acts = activity.group_activities(rows, max_gap_s=300)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0].event_ids, [1, 2])

    def test_summary_carries_app_focus_notes_and_click_trail(self) -> None:
        rows = [
            _screen(100, "report.docx - Word", "drafting the quarterly report", eid=1),
            _click(105, "report.docx - Word", eid=2),
            _click(106, "report.docx - Word", eid=3),
        ]
        a = activity.group_activities(rows, max_gap_s=300)[0]
        self.assertIn("Word", a.summary)
        self.assertIn("report.docx - Word", a.summary)
        self.assertIn("drafting the quarterly report", a.summary)
        self.assertIn("2 clicks", a.summary)
        self.assertIn("2 in report.docx - Word", a.summary)

    def test_adjacent_duplicate_screen_notes_dedup(self) -> None:
        rows = [
            _screen(100, "a - App", "same view", eid=1),
            _screen(110, "a - App", "same view", eid=2),
            _screen(120, "a - App", "new view", eid=3),
        ]
        a = activity.group_activities(rows, max_gap_s=300)[0]
        self.assertEqual(a.summary.count("same view"), 1)
        self.assertIn("new view", a.summary)

    def test_screen_note_prefers_vision_description_over_prefixed_summary(self) -> None:
        eid, ev = _screen(100, "a - App", "")
        ev.summary = "[a - App] fallback text"
        ev.meta.pop("vision", None)
        a = activity.group_activities([(1, ev)], max_gap_s=300)[0]
        self.assertIn("fallback text", a.summary)
        self.assertNotIn("[a - App]", a.summary)

    def test_summary_is_capped(self) -> None:
        rows = [_screen(100 + i, "a - App", f"note {i} " + "x" * 400, eid=i)
                for i in range(6)]
        a = activity.group_activities(rows, max_gap_s=300)[0]
        self.assertLessEqual(len(a.summary), 700)

    def test_clicks_with_no_window_count_as_desktop(self) -> None:
        a = activity.group_activities([_click(100, "", eid=1)], max_gap_s=300)[0]
        self.assertEqual(a.app, "desktop")
        self.assertIn("1 click", a.summary)


class ActivityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="quill_act_")
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_replace_and_recent_roundtrip(self) -> None:
        acts = activity.group_activities(
            [
                _screen(100, "a.py - Cursor", "code", eid=1),
                _screen(1000, "Inbox - Outlook", "mail", eid=2),
            ],
            max_gap_s=300,
        )
        self.store.replace_activities(acts)
        self.assertEqual(self.store.activity_count(), 2)
        rows = self.store.recent_activities(10)
        self.assertEqual([r["app"] for r in rows], ["Outlook", "Cursor"])  # newest first
        self.assertEqual(rows[1]["event_ids"], [1])
        self.assertEqual(rows[1]["windows"], ["a.py - Cursor"])
        # Idempotent swap: replacing again doesn't accumulate rows.
        self.store.replace_activities(acts)
        self.assertEqual(self.store.activity_count(), 2)

    def test_rebuild_reads_only_desktop_events(self) -> None:
        for _, ev in (
            _screen(100, "a.py - Cursor", "code"),
            _click(110, "a.py - Cursor"),
        ):
            self.store.insert(ev)
        self.store.insert(Event(time=105, modality=Modality.AUDIO,
                                raw="hello", summary="hello", source="audio.capture"))
        n = activity.rebuild(self.store)
        self.assertEqual(n, 1)
        row = self.store.recent_activities(10)[0]
        self.assertEqual(row["app"], "Cursor")
        self.assertEqual(row["n_screens"], 1)
        self.assertEqual(row["n_clicks"], 1)

    def test_describe_recent_lines_are_time_anchored(self) -> None:
        self.store.insert(_screen(1_752_660_000.0, "a.py - Cursor", "editing tests")[1])
        activity.rebuild(self.store)
        lines = activity.describe_recent(self.store, limit=3)
        self.assertEqual(len(lines), 1)
        self.assertIn("editing tests", lines[0])
        self.assertIn("min]", lines[0])


if __name__ == "__main__":
    unittest.main()
