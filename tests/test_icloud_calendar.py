"""Unit tests for app.services.icloud_calendar — parser + sync dedupe.

The network layer is exercised live (it was built against the real iCloud
CalDAV chain); these tests pin the pure parts: RFC 5545 unfolding, VEVENT
parsing (TZID / UTC / all-day / recurrence-id / cancelled), event text, and
the hash-dedupe contract (same event once, edited event re-lands once).

Run with either:
    python -m unittest discover -s tests
    pytest tests/
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import icloud_calendar as cal  # noqa: E402

VEVENT_TZ = (
    "BEGIN:VEVENT\n"
    "UID:ABC-123\n"
    "SUMMARY:Mix review with\n"
    "  Abby\n"
    "LOCATION:Studio B\n"
    "DTSTART;TZID=US/Pacific:20260720T090000\n"
    "DTEND;TZID=US/Pacific:20260720T100000\n"
    "END:VEVENT"
)


class ParserTests(unittest.TestCase):
    def test_unfold_and_parse_tzid(self) -> None:
        blocks = cal._ics_blocks("BEGIN:VCALENDAR\n" + VEVENT_TZ + "\nEND:VCALENDAR")
        self.assertEqual(len(blocks), 1)
        ev = cal.parse_vevent(blocks[0])
        self.assertEqual(ev["uid"], "ABC-123")
        self.assertEqual(ev["summary"], "Mix review with Abby")  # folded line joined
        self.assertEqual(ev["location"], "Studio B")
        self.assertFalse(ev["all_day"])
        self.assertEqual(ev["start"].hour, 9)
        self.assertEqual(str(ev["start"].tzinfo), "US/Pacific")

    def test_utc_and_allday(self) -> None:
        ev = cal.parse_vevent("BEGIN:VEVENT\nUID:U1\nSUMMARY:Call\n"
                              "DTSTART:20260721T140000Z\nEND:VEVENT")
        self.assertEqual(ev["start"].tzinfo, dt.timezone.utc)
        ev2 = cal.parse_vevent("BEGIN:VEVENT\nUID:U2\nSUMMARY:Trip\n"
                               "DTSTART;VALUE=DATE:20260801\nEND:VEVENT")
        self.assertTrue(ev2["all_day"])
        self.assertEqual(ev2["start"], dt.date(2026, 8, 1))

    def test_recurrence_instance_gets_distinct_uid(self) -> None:
        ev = cal.parse_vevent("BEGIN:VEVENT\nUID:U1\nSUMMARY:Standup\n"
                              "RECURRENCE-ID;TZID=US/Eastern:20260722T100000\n"
                              "DTSTART;TZID=US/Eastern:20260722T100000\nEND:VEVENT")
        self.assertIn("#", ev["uid"])

    def test_cancelled_and_broken_events_dropped(self) -> None:
        self.assertIsNone(cal.parse_vevent(
            "BEGIN:VEVENT\nUID:U1\nSTATUS:CANCELLED\n"
            "DTSTART:20260721T140000Z\nEND:VEVENT"))
        self.assertIsNone(cal.parse_vevent("BEGIN:VEVENT\nUID:U1\nEND:VEVENT"))

    def test_when_text(self) -> None:
        ev = cal.parse_vevent(VEVENT_TZ)
        ev["calendar"] = "Work"
        self.assertIn("09:00-10:00", cal._when_text(ev))


class SyncDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ical_")
        os.environ["QUILL_ICLOUD_STATE"] = str(Path(self._tmp) / "state.json")
        self.published: list = []

    def tearDown(self) -> None:
        os.environ.pop("QUILL_ICLOUD_STATE", None)

    def _run_sync(self, summary: str) -> dict:
        ev = cal.parse_vevent(VEVENT_TZ.replace("Mix review with\n  Abby", summary))
        with mock.patch.object(cal.icloud_account, "_read_saved",
                               return_value=("a@b.com", "pw")), \
             mock.patch.object(cal, "discover",
                               return_value=("https://p1.example",
                                             [{"href": "/c/home/", "name": "Home"}])), \
             mock.patch.object(cal, "query_events",
                               return_value=[{**ev, "calendar": "Home"}]):
            return cal.sync(publish=self.published.append)

    def test_new_then_unchanged_then_edited(self) -> None:
        r1 = self._run_sync("Mix review")
        self.assertTrue(r1["ok"], r1)
        self.assertEqual(r1["new"], 1)
        ev = self.published[-1]
        self.assertEqual(ev.source, "phone.calendar")
        self.assertEqual(ev.epistemic, "observed")
        self.assertIn("Mix review", ev.raw)
        # Same content again: nothing new lands.
        r2 = self._run_sync("Mix review")
        self.assertEqual(r2["new"], 0)
        self.assertEqual(len(self.published), 1)
        # Edited event (new summary): re-lands exactly once.
        r3 = self._run_sync("Mix review (moved)")
        self.assertEqual(r3["new"], 1)
        self.assertEqual(len(self.published), 2)

    def test_not_connected_is_clean_error(self) -> None:
        with mock.patch.object(cal.icloud_account, "_read_saved",
                               return_value=("", "")):
            res = cal.sync(publish=self.published.append)
        self.assertFalse(res["ok"])
        self.assertIn("not connected", res["error"])


class WriteBuilderTests(unittest.TestCase):
    """The ICS builder is pure — pin its contract (no attendees ever, escaping,
    timed vs all-day) without touching the network."""

    def _stamp(self):
        return dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.timezone.utc)

    def test_timed_event_has_dtstart_dtend_no_attendee(self) -> None:
        ics = cal.build_ics("uid1", "Dentist", "2026-07-20T15:00:00-04:00",
                            None, 60, "Office", False, self._stamp())
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertIn("SUMMARY:Dentist", ics)
        self.assertIn("DTSTART:20260720T190000Z", ics)   # 15:00 EDT -> 19:00 UTC
        self.assertIn("DTEND:20260720T200000Z", ics)     # +60 min
        self.assertIn("LOCATION:Office", ics)
        self.assertNotIn("ATTENDEE", ics)                # never invites anyone

    def test_all_day_uses_value_date(self) -> None:
        ics = cal.build_ics("uid2", "Trip", "2026-08-01", None, 60, "", True,
                            self._stamp())
        self.assertIn("DTSTART;VALUE=DATE:20260801", ics)
        self.assertIn("DTEND;VALUE=DATE:20260802", ics)  # exclusive next day

    def test_special_chars_escaped(self) -> None:
        ics = cal.build_ics("uid3", "Lunch; then, review", "2026-07-20T12:00:00Z",
                            None, 30, "", False, self._stamp())
        self.assertIn(r"SUMMARY:Lunch\; then\, review", ics)


if __name__ == "__main__":
    unittest.main()
