"""Unit tests for the chat calendar-add intent (calendar_intent + worker wiring).

Pins: the gate distinguishes ADD requests from calendar QUESTIONS, the parser
normalizes model output (all-day date trim, when_text), and the worker's
approval flow writes on 'yes' / skips on 'no' without ever adding attendees.
The model + iCloud are stubbed — no network.

Run: python -m unittest discover -s tests
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import calendar_intent as ci  # noqa: E402


class GateTests(unittest.TestCase):
    def test_add_requests_pass(self) -> None:
        for t in ("add coffee with Sam to my calendar tomorrow at 3",
                  "schedule a dentist appointment Friday at 10am",
                  "put mix deadline on my calendar Friday",
                  "remind me to call the studio tomorrow at noon",
                  "book a meeting with Abby next Tuesday at 2pm"):
            self.assertTrue(ci.looks_like_calendar_add(t), t)

    def test_questions_and_chatter_do_not(self) -> None:
        for t in ("what's on my calendar tomorrow?",
                  "when is my dentist appointment?",
                  "do I have anything Friday?",
                  "show my calendar",
                  "how was the meeting",
                  "text Sam about coffee"):
            self.assertFalse(ci.looks_like_calendar_add(t), t)


class ParseTests(unittest.TestCase):
    def _router(self, payload):
        r = mock.Mock()
        r.complete_json.return_value = payload
        return r

    def test_timed_event_normalized(self) -> None:
        r = self._router({"is_event": True, "summary": "Coffee with Sam",
                          "start": "2026-07-19T15:00:00", "end": "2026-07-19T16:00:00",
                          "all_day": False, "location": "Starbucks"})
        ev = ci.parse("add coffee with Sam tomorrow at 3", router=r)
        self.assertEqual(ev["summary"], "Coffee with Sam")
        self.assertEqual(ev["start"], "2026-07-19T15:00:00")
        self.assertEqual(ev["location"], "Starbucks")
        self.assertFalse(ev["all_day"])
        self.assertIn("3:00", ev["when_text"])

    def test_all_day_trims_to_date(self) -> None:
        r = self._router({"is_event": True, "summary": "Mix deadline",
                          "start": "2026-07-24T00:00:00", "all_day": True})
        ev = ci.parse("put mix deadline on my calendar Friday", router=r)
        self.assertEqual(ev["start"], "2026-07-24")
        self.assertIn("all day", ev["when_text"])

    def test_non_event_returns_none(self) -> None:
        r = self._router({"is_event": False, "summary": "", "start": "",
                          "all_day": False})
        self.assertIsNone(ci.parse("what's on my calendar?", router=r))


class WorkerResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.services.agent_bridge import AgentWorker
        self.w = AgentWorker()
        self.event = {"summary": "Coffee with Sam", "start": "2026-07-19T15:00:00",
                      "end": None, "all_day": False, "location": "",
                      "calendar": "Home", "when_text": "Sunday, Jul 19 at 3:00 PM"}

    def test_yes_creates_event(self) -> None:
        with mock.patch("app.services.icloud_calendar.create_event",
                        return_value={"ok": True, "calendar": "Home"}) as ce:
            self.w.propose_calendar(self.event)
            res = self.w.resolve_todo(True)
        self.assertTrue(res["accepted"])
        self.assertTrue(res["created"])
        ce.assert_called_once()
        # never adds attendees: create_event is called positionally with summary
        # + start and keyword-only fields — no attendee argument exists.
        _, kwargs = ce.call_args
        self.assertNotIn("attendees", kwargs)
        kinds = [e["kind"] for e in self.w.events]
        self.assertIn("result", kinds)

    def test_no_skips(self) -> None:
        with mock.patch("app.services.icloud_calendar.create_event") as ce:
            self.w.propose_calendar(self.event)
            res = self.w.resolve_todo(False)
        self.assertFalse(res["accepted"])
        ce.assert_not_called()

    def test_create_failure_surfaces_error(self) -> None:
        with mock.patch("app.services.icloud_calendar.create_event",
                        return_value={"ok": False, "error": "iCloud not connected"}):
            self.w.propose_calendar(self.event)
            res = self.w.resolve_todo(True)
        self.assertFalse(res["created"])
        self.assertTrue(any(e["kind"] == "error" for e in self.w.events))


if __name__ == "__main__":
    unittest.main()
