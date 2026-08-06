"""Local clock + due-date helpers for commitments / tasks."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import patch

from app.services import clock


class ClockTests(unittest.TestCase):
    def test_clock_line_includes_weekday_and_year(self) -> None:
        fixed = dt.datetime(2026, 7, 23, 14, 30, 0)  # Thursday
        line = clock.clock_line(fixed)
        self.assertIn("Thursday", line)
        self.assertIn("2026", line)
        self.assertIn("RIGHT NOW", line)

    def test_clock_instruction_asks_for_iso(self) -> None:
        text = clock.clock_instruction(dt.datetime(2026, 7, 23, 9, 0, 0))
        self.assertIn("YYYY-MM-DD", text)
        self.assertIn("tomorrow", text)

    def test_coerce_due_keeps_iso_date(self) -> None:
        self.assertEqual(clock.coerce_due("2026-07-25"), "2026-07-25")
        self.assertEqual(
            clock.coerce_due("2026-07-25T15:00:00"), "2026-07-25T15:00:00")

    def test_coerce_due_drops_free_text(self) -> None:
        self.assertIsNone(clock.coerce_due("Friday"))
        self.assertIsNone(clock.coerce_due("immediate"))
        self.assertIsNone(clock.coerce_due("Jul 7"))
        self.assertIsNone(clock.coerce_due("overdue by 112d"))
        self.assertIsNone(clock.coerce_due(""))
        self.assertIsNone(clock.coerce_due(None))

    def test_coerce_due_normalizes_us_slash(self) -> None:
        self.assertEqual(clock.coerce_due("3/27/2026"), "2026-03-27")
        self.assertEqual(
            clock.coerce_due("7/18/2026, 3:01:41 PM"), "2026-07-18T15:01:41")

    def test_format_due_relative(self) -> None:
        now = dt.datetime(2026, 7, 23, 12, 0, 0)
        self.assertIn("today", clock.format_due_for_prompt("2026-07-23", now))
        self.assertIn("tomorrow", clock.format_due_for_prompt("2026-07-24", now))
        self.assertIn("overdue", clock.format_due_for_prompt("2026-07-20", now))
        # Legacy free-text still displays as-is when already on disk.
        self.assertEqual(clock.format_due_for_prompt("Friday", now), "Friday")

    def test_graph_due_days_understands_iso(self) -> None:
        from app.services.graph import _due_days
        now = dt.datetime(2026, 7, 23, 12, 0, 0).timestamp()
        days = _due_days("2026-07-24", now)
        self.assertIsNotNone(days)
        assert days is not None
        self.assertAlmostEqual(days, 1.0, delta=1.0)
        self.assertIsNone(_due_days("next Friday", now))


class GroundingClockTests(unittest.TestCase):
    def test_tasks_section_shows_due(self) -> None:
        from app.services.grounding import _tasks_section

        class FakeStore:
            def list_facts(self, **kwargs):
                if kwargs.get("kind") == "task":
                    return [{
                        "fact_id": 1, "kind": "task",
                        "text": "Send pricing follow-up",
                        "due": "2026-07-24", "extracted_at": 1.0,
                    }]
                return []

        with patch("app.services.clock.now_local",
                   return_value=dt.datetime(2026, 7, 23, 10, 0, 0)):
            lines, ids = _tasks_section(FakeStore())
        self.assertEqual(ids, [1])
        joined = "\n".join(lines)
        self.assertIn("Send pricing follow-up", joined)
        self.assertIn("due", joined.lower())
        self.assertIn("2026-07-24", joined)


if __name__ == "__main__":
    unittest.main()
