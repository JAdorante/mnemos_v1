"""CAL §14 — the labelling loop: sheet with predictions, "=" acceptance, CSV
round-trip, confusions, and the two commands that close the loop."""
from __future__ import annotations

import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path

from app.events import Event, Modality
from app.services.context import evaluate as ev
from app.storage import Store


def _episodes():
    return [{"started_at": 100.0, "ended_at": 200.0, "node_type": "entity",
             "title": "Ravenry", "_event_ids": [(1, False), (2, True)]},
            {"started_at": 200.0, "ended_at": 300.0, "node_type": None,
             "title": "Firefox", "_event_ids": [(3, True), (4, True)]}]


class PredictionTests(unittest.TestCase):
    def test_predictions_from_replay(self) -> None:
        predicted, starts = ev.predictions_from(_episodes())
        self.assertEqual(predicted, {1: "Ravenry", 2: "Ravenry", 3: None, 4: None})
        self.assertEqual(starts, {1, 3}, "first event of each episode opens it")

    def test_attribution_ignores_case_and_whitespace(self) -> None:
        got = ev.attribution({1: "Ravenry", 2: "mnemos"},
                             {1: "ravenry ", 2: "Mnemos"})
        self.assertEqual(got["n_correct"], 2)

    def test_confusions_group_the_wrong_ones(self) -> None:
        got = ev.confusions({1: "Justin", 2: "Justin", 3: None, 4: "A"},
                            {1: "mnemos", 2: "mnemos", 3: "mnemos", 4: "A"})
        self.assertEqual(got[0], ("Justin", "mnemos", 2))
        self.assertIn(("—", "mnemos", 1), got)
        self.assertEqual(sum(n for _p, _t, n in got), 3)

    def test_score_run_carries_confusions(self) -> None:
        got = ev.score_run(_episodes(), {1: "Ravenry", 2: "X", 3: None, 4: None},
                           [100.0, 200.0])
        self.assertEqual(got["confusions"], [("Ravenry", "X", 1)])


class SheetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_eval_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "a")
        self.t0 = 1_700_000_000.0
        for i in range(3):
            self.store.insert(Event(
                time=self.t0 + i, modality=Modality.INPUT, raw="click",
                summary="click", source="desktop.click",
                meta={"window": "Docs - Chromium"}))
        self.ids = [r["event_id"] for r in ev.labelling_sheet(
            self.store, t0=self.t0 - 1, t1=self.t0 + 100)]

    def _eps(self):
        a, b, c = self.ids
        return [{"started_at": self.t0, "ended_at": self.t0 + 1,
                 "node_type": "entity", "title": "Ravenry",
                 "_event_ids": [(a, False), (b, True)]},
                {"started_at": self.t0 + 2, "ended_at": self.t0 + 3,
                 "node_type": None, "title": "Firefox",
                 "_event_ids": [(c, True)]}]

    def test_sheet_carries_predictions_beside_blank_labels(self) -> None:
        sheet = ev.labelling_sheet(self.store, t0=self.t0 - 1, t1=self.t0 + 100,
                                   episodes=self._eps())
        self.assertEqual([r["predicted_project"] for r in sheet],
                         ["Ravenry", "Ravenry", ""])
        self.assertEqual([r["predicted_boundary"] for r in sheet], ["1", "", "1"])
        self.assertTrue(all(r["label_project"] == "" for r in sheet))
        self.assertTrue(all(r["label_boundary"] == "" for r in sheet))
        self.assertRegex(sheet[0]["hhmm"], r"^\d\d:\d\d:\d\d$")

    def test_blind_sheet_has_the_columns_but_no_claims(self) -> None:
        sheet = ev.labelling_sheet(self.store, t0=self.t0 - 1, t1=self.t0 + 100)
        self.assertTrue(all(r["predicted_project"] == "" and
                            r["predicted_boundary"] == "" for r in sheet))

    def test_accept_marker_copies_the_prediction(self) -> None:
        sheet = ev.labelling_sheet(self.store, t0=self.t0 - 1, t1=self.t0 + 100,
                                   episodes=self._eps())
        a, b, c = self.ids
        truth, bounds = ev.load_labels([
            {**sheet[0], "label_project": "=", "label_boundary": "="},
            {**sheet[1], "label_project": "mnemos", "label_boundary": ""},
            {**sheet[2], "label_project": "=", "label_boundary": "1"}])
        self.assertEqual(truth, {a: "Ravenry", b: "mnemos", c: None},
                         "'=' on a blank prediction is an honest blank")
        self.assertEqual(bounds, [self.t0, self.t0 + 2])

    def test_csv_round_trip_keeps_types(self) -> None:
        sheet = ev.labelling_sheet(self.store, t0=self.t0 - 1, t1=self.t0 + 100,
                                   episodes=self._eps())
        path = ev.write_sheet(sheet, self.tmp / "sub" / "day.csv")
        self.assertTrue(path.exists())
        back = ev.read_sheet(path)
        self.assertEqual(len(back), 3)
        self.assertIsInstance(back[0]["event_id"], int)
        self.assertIsInstance(back[0]["time"], float)
        self.assertEqual(back[0]["predicted_project"], "Ravenry")
        self.assertEqual(list(back[0].keys()), list(ev.SHEET_COLUMNS))


class LoopTests(unittest.TestCase):
    """One real store, one day: sheet -> accept everything -> score == perfect.

    A sheet that accepts the system's own claims must score 1.0 on both axes;
    anything less means the sheet and the replay disagree about the day."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_loop_"))
        self.db = self.tmp / "t.db"
        store = Store(db_path=self.db, audio_dir=self.tmp / "a")
        store.resolve_entity("Boostrun", "org", ts=time.time())
        import datetime as dt
        self.day = "2026-08-26"
        self.t0 = dt.datetime.strptime(self.day, "%Y-%m-%d").timestamp() + 3600
        for i in range(20):
            store.insert(Event(
                time=self.t0 + i * 20, modality=Modality.INPUT, raw="click",
                summary="click", source="desktop.click",
                meta={"window": "Boostrun plan - Google Docs - Chromium"}))
        self.store = store

    def test_sheet_then_score_round_trip(self) -> None:
        rows = ev.sheet_for_day(self.store, self.day)
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]["predicted_project"], "Boostrun")
        self.assertEqual(rows[0]["predicted_boundary"], "1")
        for r in rows:
            r["label_project"] = "="
            r["label_boundary"] = "="
        got = ev.score_labels(self.store, rows)
        self.assertEqual(got["day"], self.day, "day read off the sheet")
        self.assertEqual(got["boundaries"]["f1"], 1.0)
        self.assertEqual(got["attribution"]["precision"], 1.0)
        self.assertEqual(got["attribution"]["recall"], 1.0)
        self.assertEqual(got["n_blank"], 0)
        self.assertIn("boundaries   P 1.00", ev.report(got))

    def test_cli_writes_and_scores_a_sheet(self) -> None:
        out = self.tmp / "sheet.csv"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ev.main(["sheet", "--day", self.day, "--db", str(self.db),
                          "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertIn("20 rows", buf.getvalue())
        rows = ev.read_sheet(out)
        for r in rows:
            r["label_project"] = "Boostrun"       # typed, not "=": case-free match
            r["label_boundary"] = "="
        ev.write_sheet(rows, out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ev.main(["score", "--labels", str(out), "--db", str(self.db)])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn(self.day, text)
        self.assertIn("F1 1.00", text)
        self.assertIn("unbound 0%", text)
        self.assertIn("outside 5–15%", text, "zero unbound is flagged, not praised")

    def test_untouched_sheet_refuses_to_print_zeros(self) -> None:
        rows = ev.sheet_for_day(self.store, self.day)
        got = ev.score_labels(self.store, rows)
        self.assertTrue(ev.unlabelled(got))
        text = ev.report(got)
        self.assertIn("none labelled yet", text)
        self.assertNotIn("healthy", text)
        out = self.tmp / "blank.csv"
        ev.write_sheet(rows, out)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ev.main(["score", "--labels", str(out), "--db", str(self.db)])
        self.assertEqual(rc, 2, "an unlabelled sheet is not a passing score")

    def test_fill_labels_a_stretch_by_clock_time(self) -> None:
        out = self.tmp / "sheet.csv"
        ev.write_sheet(ev.sheet_for_day(self.store, self.day), out)
        first = ev.read_sheet(out)[0]["hhmm"][:5]           # e.g. "01:00"
        h, m = int(first[:2]), int(first[3:])
        mid = f"{h:02d}:{m + 3:02d}"                        # 20 rows × 20 s = 6m40s
        with contextlib.redirect_stdout(io.StringIO()):
            ev.main(["fill", "--labels", str(out), "--from", first, "--to", mid,
                     "--project", "Boostrun"])
            ev.main(["fill", "--labels", str(out), "--from", mid, "--to", "23:59",
                     "--accept", "--no-boundary"])
        rows = ev.read_sheet(out)
        typed = [r for r in rows if r["label_project"] == "Boostrun"]
        accepted = [r for r in rows if r["label_project"] == "="]
        self.assertEqual(len(typed) + len(accepted), 20)
        self.assertEqual(len(typed), 9, "rows at 0,20,…,160 s fall under 3 min")
        self.assertEqual([r["label_boundary"] for r in rows].count("1"), 1,
                         "one boundary at the first stretch; --no-boundary adds none")
        got = ev.score_labels(self.store, rows)
        self.assertEqual(got["attribution"]["precision"], 1.0)
        self.assertEqual(got["boundaries"]["f1"], 1.0)

    def test_unfinished_sheet_is_reported_not_hidden(self) -> None:
        rows = ev.sheet_for_day(self.store, self.day)
        rows[0]["label_project"] = "="
        got = ev.score_labels(self.store, rows)
        self.assertEqual(got["n_blank"], 19)
        self.assertIn("19 blank project labels", ev.report(got))


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
