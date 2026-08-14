"""Meeting Layer P1 — calendar ↔ session join + attendee priors."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

NOW = 1_700_000_000.0  # fixed epoch for deterministic windows


class OverlapUnitTests(unittest.TestCase):
    def test_overlap_seconds_and_qualify(self):
        from app.services.meeting_join import (
            overlap_qualifies, overlap_seconds,
        )
        self.assertEqual(overlap_seconds(0, 60, 30, 90), 30)
        self.assertEqual(overlap_seconds(0, 10, 20, 30), 0)
        # 12 min overlap → qualifies via min_s
        self.assertTrue(overlap_qualifies(12 * 60, 30 * 60, 30 * 60))
        # 8 min of a 10-min session (=80%) → qualifies via frac
        self.assertTrue(overlap_qualifies(8 * 60, 10 * 60, 60 * 60))
        # 4 min of a 60-min session → no
        self.assertFalse(overlap_qualifies(4 * 60, 60 * 60, 60 * 60))

    def test_back_to_back_meetings_pick_closer_start(self):
        from app.services.meeting_join import best_event_for_session
        # Two 30-min meetings back-to-back; session starts 2 min into the second.
        e1 = {"id": "cal|a", "title": "A", "start": NOW,
              "end": NOW + 30 * 60, "all_day": False, "attendees": []}
        e2 = {"id": "cal|b", "title": "B", "start": NOW + 30 * 60,
              "end": NOW + 60 * 60, "all_day": False, "attendees": []}
        sess_start = NOW + 32 * 60
        sess_end = NOW + 55 * 60
        best = best_event_for_session(sess_start, sess_end, [e1, e2])
        self.assertIsNotNone(best)
        self.assertEqual(best["id"], "cal|b")

    def test_early_join_still_links(self):
        from app.services.meeting_join import best_event_for_session
        # User joins 5 min early; session starts before event.
        ev = {"id": "cal|m", "title": "Sync", "start": NOW + 5 * 60,
              "end": NOW + 35 * 60, "all_day": False}
        best = best_event_for_session(NOW, NOW + 40 * 60, [ev])
        self.assertIsNotNone(best)
        self.assertEqual(best["id"], "cal|m")

    def test_no_calendar_adhoc_unlinked(self):
        from app.services.meeting_join import attach_calendar
        from app.services.sessions import Session
        sess = Session(start=NOW, end=NOW + 20 * 60, text="adhoc chat")
        n = attach_calendar([sess], [])
        self.assertEqual(n, 0)
        self.assertIsNone(sess.calendar_event_id)

    def test_one_event_claims_one_session(self):
        from app.services.meeting_join import attach_calendar
        from app.services.sessions import Session
        ev = {"id": "cal|x", "title": "1:1", "start": NOW,
              "end": NOW + 30 * 60, "all_day": False,
              "attendees": [{"name": "Sarah Chen", "email": "sarah@acme.com"}]}
        s1 = Session(start=NOW + 60, end=NOW + 25 * 60, text="first")
        s2 = Session(start=NOW + 60, end=NOW + 20 * 60, text="second")
        n = attach_calendar([s1, s2], [ev])
        self.assertEqual(n, 1)
        linked = [s for s in (s1, s2) if s.calendar_event_id]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0].meeting_meta["title"], "1:1")
        self.assertEqual(len(linked[0].meeting_meta["attendees"]), 1)


class CalendarIndexStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_mj_"))
        from app.storage import Store
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def tearDown(self):
        self.store.close()

    def test_upsert_and_list_window(self):
        self.store.upsert_calendar_event(
            event_id="Work|uid1", calendar="Work", uid="uid1",
            title="Diligence", start=NOW, end=NOW + 3600,
            attendees=[{"name": "Sarah Chen", "email": "sarah@acme.com"}],
            organizer={"name": "Me", "email": "me@acme.com"},
        )
        rows = self.store.list_calendar_events(
            start_min=NOW - 10, start_max=NOW + 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Diligence")
        self.assertEqual(rows[0]["attendees"][0]["email"], "sarah@acme.com")

    def test_sessions_rebuild_joins_calendar(self):
        from app.services import sessions as sess_mod
        from app.services.consolidation import Turn
        # Seed a calendar event + one turn that becomes a session.
        self.store.upsert_calendar_event(
            event_id="Home|meet1", calendar="Home", uid="meet1",
            title="Pricing call", start=NOW, end=NOW + 30 * 60,
            attendees=[{"name": "Sarah Chen", "email": "sarah@acme.com"}],
        )
        turn = Turn(
            start=NOW + 60, end=NOW + 20 * 60, speaker="user",
            text="Sarah said pricing is tight",
            event_ids=[1], audio_paths=[], n_utterances=1,
        )
        self.store.replace_turns([turn])

        n = sess_mod.rebuild(self.store)
        self.assertGreaterEqual(n, 1)
        rows = self.store.recent_sessions(limit=10)
        self.assertTrue(any(r.get("calendar_event_id") == "Home|meet1"
                            for r in rows), rows)
        linked = next(r for r in rows if r.get("calendar_event_id"))
        self.assertEqual(linked["meeting_meta"]["title"], "Pricing call")


class AttendeePriorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_att_"))
        from app.storage import Store
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.env = patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.store.close()

    def test_first_name_resolves_to_attendee_not_new_node(self):
        from app.services.people_pipeline import resolve_person_mention
        # Existing person with invite email.
        pid = self.store.insert_person("Sarah Chen", ts=NOW,
                                       promotion_state="active")
        self.store.upsert_contact_point(
            person_id=pid, type_="email",
            value_display="sarah@acme.com",
            value_normalized="sarah@acme.com",
            confidence=0.9, attribution_method="user",
            verification_status="verified", source_event_id=None,
            evidence_quote=None, discourse_role=None, ts=NOW,
            created_by="test", pipeline_version="test",
        )
        # A different Sarah also exists — without priors, first-name is ambiguous.
        self.store.insert_person("Sarah Jones", ts=NOW,
                                 promotion_state="active")
        priors = [{"name": "Sarah Chen", "email": "sarah@acme.com"}]
        res = resolve_person_mention(
            "Sarah", store=self.store, event_source="audio.whisper",
            text="Sarah will send pricing", now=NOW,
            attendee_priors=priors,
        )
        self.assertEqual(res.decision, "auto_resolve")
        self.assertEqual(res.person_id, pid)

    def test_matching_attendees_helper(self):
        from app.services.people_pipeline import matching_attendees
        atts = [
            {"name": "Sarah Chen", "email": "sarah@acme.com"},
            {"name": "Bob", "email": "bob@acme.com"},
        ]
        m = matching_attendees("Sarah", atts)
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["email"], "sarah@acme.com")


class ParseAttendeeTests(unittest.TestCase):
    def test_parse_vevent_attendees(self):
        from app.services import icloud_calendar as cal
        block = (
            "BEGIN:VEVENT\n"
            "UID:ABC-ATT\n"
            "SUMMARY:Pricing\n"
            "DTSTART:20260721T140000Z\n"
            "DTEND:20260721T150000Z\n"
            "ORGANIZER;CN=Pat Owner:mailto:pat@acme.com\n"
            "ATTENDEE;CN=Sarah Chen;ROLE=REQ-PARTICIPANT:mailto:sarah@acme.com\n"
            "ATTENDEE;CN=Bob:mailto:bob@acme.com\n"
            "END:VEVENT"
        )
        ev = cal.parse_vevent(block)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["organizer"]["email"], "pat@acme.com")
        self.assertEqual(len(ev["attendees"]), 2)
        emails = {a["email"] for a in ev["attendees"]}
        self.assertEqual(emails, {"sarah@acme.com", "bob@acme.com"})

    def test_parse_vevent_zoom_url_in_description(self):
        from app.services import icloud_calendar as cal
        block = (
            "BEGIN:VEVENT\n"
            "UID:ZOOM-1\n"
            "SUMMARY:Standup\n"
            "DTSTART:20260721T140000Z\n"
            "DTEND:20260721T150000Z\n"
            "LOCATION:https://acme.zoom.us/j/123456789\n"
            "DESCRIPTION:Join Zoom Meeting\\nhttps://acme.zoom.us/j/123456789\n"
            "END:VEVENT"
        )
        ev = cal.parse_vevent(block)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["provider"], "zoom")
        self.assertIn("zoom.us", ev["join_url"])


if __name__ == "__main__":
    unittest.main()
