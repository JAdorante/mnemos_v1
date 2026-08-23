"""WS-A tier 3 — the cohort table the Oct 1 decision reads.

Three synthetic installs, one of each shape: a retained weekly-active user, a
week-1-only churner, and a fresh install still inside week 2. The gates must
count them correctly, and the still-in-week-2 install must not be counted as
churn — that is the difference between a 50% and a 33% retention number.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "pilot_metrics", Path(__file__).resolve().parent.parent
    / "scripts" / "pilot_metrics.py")
pm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pm)


def day_plus(base: str, n: int) -> str:
    d = datetime.strptime(base, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (d + timedelta(days=n)).strftime("%Y-%m-%d")


def report(install_id: str, install_day: str, active_offsets: list[int], *,
           generated: str, os_name: str = "Windows",
           version: str = "0.4.0") -> dict:
    return {
        "schema": "mnemos.usage/1",
        "install_id": install_id,
        "installed_at_day": install_day,
        "generated_at_day": generated,
        "version": version,
        "os": os_name,
        "timezone": "UTC",
        "days": [{"day": day_plus(install_day, o), "install_id": install_id,
                  "active_minutes": 12, "searches": 3, "chat_turns": 1,
                  "app_starts": 1, "meetings_captured": 1, "meeting_minutes": 30,
                  "facts_reviewed": 2, "facts_created": 5, "agent_tasks": 0,
                  "approvals": 0, "capture_audio_minutes": 30,
                  "capture_desktop_minutes": 0, "version": version,
                  "os": os_name}
                 for o in active_offsets],
        "metrics": {},
    }


class ThreeInstallCohortTests(unittest.TestCase):
    AS_OF = "2025-10-01"

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="quill_pilot_"))
        # A: installed 3 weeks before the checkpoint, active in weeks 1, 2 and
        #    the trailing 7 days -> WAU and retained.
        self.a = report("aaaaaaaa-0000-0000-0000-000000000001", "2025-09-08",
                        [0, 2, 9, 20, 22], generated=self.AS_OF)
        # B: same install date, active only in week 1 -> not WAU, not retained.
        self.b = report("bbbbbbbb-0000-0000-0000-000000000002", "2025-09-08",
                        [0, 1, 3], generated="2025-09-15", os_name="Darwin")
        # C: installed 4 days before the checkpoint -> WAU, week 2 not reached.
        self.c = report("cccccccc-0000-0000-0000-000000000003", "2025-09-27",
                        [0, 1, 3], generated=self.AS_OF)
        for i, rep in enumerate((self.a, self.b, self.c)):
            (self.dir / f"usage-{i}.json").write_text(json.dumps(rep),
                                                      encoding="utf-8")
        self.as_of = datetime.strptime(self.AS_OF, "%Y-%m-%d").replace(
            hour=23, minute=59, tzinfo=timezone.utc).timestamp()

    def _run(self):
        installs = pm.load_reports(self.dir)
        summaries = pm.summarize(installs, as_of=self.as_of)
        return summaries, pm.cohort(summaries)

    def test_per_install_rows(self) -> None:
        summaries, _ = self._run()
        by_id = {s["install_id"][:1]: s for s in summaries}
        self.assertEqual(len(summaries), 3)

        a = by_id["a"]
        self.assertTrue(a["is_wau"])
        self.assertTrue(a["retained_wk2"])
        self.assertEqual(a["wk1_active_days"], 2)     # offsets 0, 2
        self.assertEqual(a["wk2_active_days"], 1)     # offset 9
        self.assertEqual(a["wau_weeks"], [1, 2, 3, 4])

        b = by_id["b"]
        self.assertFalse(b["is_wau"])                 # nothing in the last 7 days
        self.assertFalse(b["retained_wk2"])
        self.assertTrue(b["wk2_complete"])            # week 2 has elapsed
        self.assertEqual(b["os"], "Darwin")

        c = by_id["c"]
        self.assertTrue(c["is_wau"])
        self.assertFalse(c["wk2_complete"])           # only day 5 for them

    def test_cohort_gates(self) -> None:
        _, totals = self._run()
        self.assertEqual(totals["installs"], 3)
        self.assertEqual(totals["wau"], 2)
        self.assertFalse(totals["wau_pass"])          # gate is 8
        # C is excluded from the denominator, not counted as churn.
        self.assertEqual(totals["wk2_eligible"], 2)
        self.assertEqual(totals["wk2_retained"], 1)
        self.assertEqual(totals["wk2_rate"], 0.5)
        self.assertTrue(totals["retention_pass"])
        self.assertEqual(totals["wk2_pending"], 1)

    def test_eight_wau_installs_pass_the_gate(self) -> None:
        for i in range(8):
            rep = report(f"dddddddd-0000-0000-0000-00000000000{i}", "2025-09-08",
                         [0, 9, 22], generated=self.AS_OF)
            (self.dir / f"extra-{i}.json").write_text(json.dumps(rep),
                                                      encoding="utf-8")
        _, totals = self._run()
        self.assertEqual(totals["wau"], 10)
        self.assertTrue(totals["wau_pass"])
        self.assertTrue(totals["retention_pass"])


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="quill_pilot_m_"))

    def test_repeat_reports_from_one_install_collapse(self) -> None:
        early = report("same-install", "2025-09-08", [0, 1], generated="2025-09-10")
        late = report("same-install", "2025-09-08", [0, 1, 2, 3],
                      generated="2025-09-12")
        (self.dir / "a.json").write_text(json.dumps(early), encoding="utf-8")
        (self.dir / "b.json").write_text(json.dumps(late), encoding="utf-8")
        installs = pm.load_reports(self.dir)
        self.assertEqual(len(installs), 1)
        self.assertEqual(len(installs["same-install"]["days"]), 4)
        self.assertEqual(installs["same-install"]["reports"], 2)

    def test_history_from_a_stale_report_is_not_lost(self) -> None:
        """A day present only in the older report still counts."""
        old = report("i", "2025-09-08", [0, 1, 2], generated="2025-09-11")
        new = report("i", "2025-09-08", [5], generated="2025-09-14")
        (self.dir / "a.json").write_text(json.dumps(old), encoding="utf-8")
        (self.dir / "b.json").write_text(json.dumps(new), encoding="utf-8")
        installs = pm.load_reports(self.dir)
        self.assertEqual(len(installs["i"]["days"]), 4)

    def test_junk_and_foreign_files_are_skipped_not_fatal(self) -> None:
        (self.dir / "broken.json").write_text("{not json", encoding="utf-8")
        (self.dir / "other.json").write_text('{"schema": "something/else"}',
                                             encoding="utf-8")
        (self.dir / "ok.json").write_text(
            json.dumps(report("i", "2025-09-08", [0], generated="2025-09-08")),
            encoding="utf-8")
        self.assertEqual(list(pm.load_reports(self.dir)), ["i"])

    def test_empty_folder_reports_unmeasured_not_zero(self) -> None:
        totals = pm.cohort(pm.summarize({}, as_of=1_759_363_140.0))
        self.assertEqual(totals["installs"], 0)
        self.assertEqual(totals["wk2_eligible"], 0)
        self.assertFalse(totals["retention_pass"])


class CliTests(unittest.TestCase):
    def test_main_prints_the_table(self) -> None:
        import io
        from contextlib import redirect_stdout
        d = Path(tempfile.mkdtemp(prefix="quill_pilot_cli_"))
        (d / "x.json").write_text(
            json.dumps(report("i", "2025-09-08", [0, 9], generated="2025-10-01")),
            encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pm.main([str(d), "--as-of", "2025-10-01"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("weekly active", out)
        self.assertIn("week-2 retention", out)
        self.assertIn("1/1 = 100%", out)

    def test_json_output_is_machine_readable(self) -> None:
        import io
        from contextlib import redirect_stdout
        d = Path(tempfile.mkdtemp(prefix="quill_pilot_json_"))
        (d / "x.json").write_text(
            json.dumps(report("i", "2025-09-08", [0, 9], generated="2025-10-01")),
            encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            pm.main([str(d), "--as-of", "2025-10-01", "--json"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["as_of"], "2025-10-01")
        self.assertEqual(payload["cohort"]["installs"], 1)

    def test_missing_folder_exits_nonzero(self) -> None:
        self.assertEqual(pm.main(["/nonexistent/pilot/dir"]), 2)


if __name__ == "__main__":
    unittest.main()
