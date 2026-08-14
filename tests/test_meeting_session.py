"""First-class MeetingSession — calendar spawn, consent gate, channel split."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from app.events import Event, Modality
from app.services import mint_recurrence as mr


def _store(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db", audio_dir=Path(td) / "audio")


def _flag(enabled=True, min_sessions=2, ttl_days=30.0):
    return SimpleNamespace(mint_recurrence=SimpleNamespace(
        enabled=enabled, min_sessions=min_sessions, ttl_days=ttl_days))


class ConferenceLinkTests(unittest.TestCase):
    def test_extract_zoom_from_description(self):
        from app.services.meeting_session import extract_conference_link
        url, provider = extract_conference_link(
            "Join: https://acme.zoom.us/j/555?pwd=abc extra")
        self.assertEqual(provider, "zoom")
        self.assertIn("zoom.us/j/555", url)

    def test_extract_meet_and_teams(self):
        from app.services.meeting_session import extract_conference_link
        _, p = extract_conference_link("https://meet.google.com/abc-defg-hij")
        self.assertEqual(p, "meet")
        _, p2 = extract_conference_link(
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting")
        self.assertEqual(p2, "teams")

    def test_provider_from_window(self):
        from app.services.meeting_session import provider_from_window
        self.assertEqual(provider_from_window("Standup - Zoom Meeting"), "zoom")
        self.assertEqual(provider_from_window("Microsoft Teams"), "teams")
        self.assertEqual(
            provider_from_window("EOW Team Call - Google Chrome"), "meet")
        self.assertEqual(provider_from_window("EOW Team Call"), "meet")
        self.assertIsNone(provider_from_window("Cursor — nexus_v1"))
        self.assertIsNone(provider_from_window("CRM — VenturePulse - Google Chrome"))

    def test_calendar_event_matching_window(self):
        from app.services.meeting_session import calendar_event_matching_window
        events = [
            {"title": "Focus time", "all_day": False},
            {"title": "EOW Team Call", "all_day": False, "id": "eow"},
        ]
        hit = calendar_event_matching_window(
            events, "EOW Team Call - Google Chrome")
        self.assertEqual(hit["id"], "eow")


class SpawnConsentTests(unittest.TestCase):
    def setUp(self):
        from app.services import meeting_session as ms
        ms.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(self.tmp.name)
        self.now = time.time()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        from app.services import meeting_session as ms
        ms.reset()
        self.store.close()
        self.tmp.cleanup()

    def test_calendar_zoom_offers_no_remote_until_accept(self):
        from app.services import meeting_session as ms
        now = self.now
        self.store.upsert_calendar_event(
            event_id="Work|z1", calendar="Work", uid="z1",
            title="Standup", start=now - 10, end=now + 1800,
            attendees=[{"name": "Sarah Chen", "email": "sarah@acme.com"}],
            join_url="https://acme.zoom.us/j/1", provider="zoom",
            updated_at=now,
        )
        offered = []
        out = ms.consider(self.store, now=now, propose=lambda p: offered.append(p) or True)
        self.assertTrue(out.get("offered"))
        self.assertEqual(offered[0]["calendar_event_id"], "Work|z1")
        self.assertFalse(ms.should_ingest("audio.system"))
        self.assertTrue(ms.should_ingest("audio.whisper"))
        ev = Event(time=now, modality=Modality.AUDIO, raw="hi",
                   source="audio.system", meta={})
        ms.stamp_event(ev)
        self.assertIsNone(ev.meta.get("meeting_session_id"))

    def test_accept_transcript_stamps_channels_and_live_attendees(self):
        from app.services import meeting_session as ms
        from app.services.meeting_join import attendees_for_time
        now = self.now
        sess = ms.spawn(
            self.store, calendar_event_id="Work|p",
            title="Pricing",
            attendees=[{"name": "Sarah Chen", "email": "sarah@acme.com"}],
            t_start=now, t_end=now + 1800, source="calendar",
        )
        with patch("app.services.meeting_mode.enter", return_value={"ok": True}):
            dec = ms.decide("transcript_only", store=self.store,
                            session_id=sess["id"])
        self.assertTrue(dec["ok"])
        self.assertEqual(ms.status()["active"], True)
        self.assertTrue(ms.should_ingest("audio.system"))
        mic = Event(time=now, modality=Modality.AUDIO, raw="I will send it",
                    source="audio.whisper", meta={})
        remote = Event(time=now, modality=Modality.AUDIO, raw="sounds good",
                       source="audio.system", meta={})
        ms.stamp_event(mic)
        ms.stamp_event(remote)
        self.assertEqual(mic.meta["meeting_session_id"], sess["id"])
        self.assertEqual(mic.meta["audio_channel"], "mic")
        self.assertEqual(remote.meta["audio_channel"], "remote")
        priors = attendees_for_time(self.store, now, now + 10)
        self.assertEqual(priors[0]["email"], "sarah@acme.com")
        self.assertEqual(ms.speaker_space("audio.whisper"), "self")
        self.assertEqual(ms.speaker_space("audio.system"), "remote")
        self.assertIn("Sarah Chen", ms.asr_extra_terms())

    def test_skip_blocks_mic_and_system(self):
        from app.services import meeting_session as ms
        now = self.now
        sess = ms.spawn(
            self.store, calendar_event_id="Work|s",
            title="Investor", t_start=now, t_end=now + 1800)
        with patch("app.services.meeting_mode.enter", return_value={"ok": True}):
            ms.decide("skip", store=self.store, session_id=sess["id"])
        self.assertFalse(ms.should_ingest("audio.whisper"))
        self.assertFalse(ms.should_ingest("audio.system"))
        self.assertTrue(ms.should_ingest("desktop.screen"))

    def test_recurring_reuses_last_consent(self):
        from app.services import meeting_session as ms
        now = self.now
        first = ms.spawn(
            self.store, calendar_event_id="Work|series",
            title="Standup", t_start=now - 86400, t_end=now - 86400 + 1800)
        self.store.update_meeting_session(first["id"], consent="transcript_only",
                                          status="ended")
        self.store.upsert_calendar_event(
            event_id="Work|series#2", calendar="Work", uid="series#2",
            title="Standup", start=now - 10, end=now + 1800, updated_at=now)
        ms.reset()
        with patch("app.services.meeting_mode.enter", return_value={"ok": True}):
            out = ms.consider(self.store, now=now, propose=lambda p: True)
        self.assertTrue(out.get("auto"))
        self.assertEqual(out.get("consent"), "transcript_only")

    def test_window_fallback_still_prompts(self):
        from app.services import meeting_session as ms
        offered = []
        out = ms.consider(
            self.store, now=self.now, window_title="Weekly - Zoom Meeting",
            propose=lambda p: offered.append(p) or True)
        self.assertTrue(out.get("offered"))
        self.assertEqual(out.get("source"), "window_fallback")
        self.assertEqual(offered[0]["provider"], "zoom")
        self.assertEqual(ms.current().get("consent"), "pending")

    def test_chrome_team_call_window_fallback(self):
        from app.services import meeting_session as ms
        offered = []
        out = ms.consider(
            self.store, now=self.now,
            window_title="EOW Team Call - Google Chrome",
            propose=lambda p: offered.append(p) or True)
        self.assertTrue(out.get("offered"))
        self.assertEqual(out.get("source"), "window_fallback")
        self.assertEqual(out.get("title"), "EOW Team Call")
        self.assertEqual(offered[0]["provider"], "meet")

    def test_prefers_calendar_event_matching_foreground(self):
        from app.services import meeting_session as ms
        now = self.now
        self.store.upsert_calendar_event(
            event_id="Work|focus", calendar="Work", uid="focus",
            title="Focus time", start=now - 10, end=now + 1800,
            updated_at=now)
        self.store.upsert_calendar_event(
            event_id="Work|eow", calendar="Work", uid="eow",
            title="EOW Team Call", start=now - 10, end=now + 1800,
            attendees=[{"name": "Savannah", "email": "savannah@acme.com"}],
            updated_at=now)
        offered = []
        out = ms.consider(
            self.store, now=now,
            window_title="EOW Team Call - Google Chrome",
            propose=lambda p: offered.append(p) or True)
        self.assertTrue(out.get("offered"))
        self.assertEqual(out.get("calendar_event_id"), "Work|eow")
        self.assertEqual(offered[0]["attendees"][0]["email"], "savannah@acme.com")


class InviteeMintTests(unittest.TestCase):
    def setUp(self):
        from app.services import meeting_session as ms
        ms.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(self.tmp.name)
        self.env = patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"})
        self.env.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        from app.services import meeting_session as ms
        ms.reset()
        self.env.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_invitee_bypasses_mint_recurrence_via_live_roster(self):
        from app.services import meeting_session as ms
        from app.services.meeting_join import attendees_for_time
        from app.services.people_pipeline import resolve_person_mention
        now = time.time()
        ms.spawn(
            self.store, calendar_event_id="Work|m",
            title="Pricing",
            attendees=[{"name": "Sarah Chen", "email": "sarah@acme.com"}],
            t_start=now, t_end=now + 1800)
        priors = attendees_for_time(self.store, now, now + 5)
        self.assertTrue(priors)
        pid = self.store.insert_person("Sarah Chen", ts=now,
                                       promotion_state="active")
        self.store.upsert_contact_point(
            person_id=pid, type_="email",
            value_display="sarah@acme.com",
            value_normalized="sarah@acme.com",
            confidence=0.9, attribution_method="user",
            verification_status="verified", source_event_id=None,
            evidence_quote=None, discourse_role=None, ts=now,
            created_by="test", pipeline_version="test",
        )
        with patch.object(mr, "settings", _flag()):
            res = resolve_person_mention(
                "Sarah", store=self.store, event_source="audio.whisper",
                text="Sarah will send pricing", now=now,
                attendee_priors=priors, relationship_boost=0.9)
        self.assertEqual(res.decision, "auto_resolve")
        self.assertEqual(res.person_id, pid)

    def test_ambient_name_still_pools_without_invite(self):
        from app.services.people_pipeline import resolve_person_mention
        now = time.time()
        with patch.object(mr, "settings", _flag()):
            res = resolve_person_mention(
                "Kevin Doyle", store=self.store, event_source="audio.whisper",
                text="Kevin Doyle from the vendor call said hi", now=now,
                attendee_priors=None, relationship_boost=0.9)
        self.assertIn(res.decision, ("pending_mint", "leave_open", "create_new"))
        self.assertNotEqual(res.decision, "auto_resolve")


class SpeakerSpaceTests(unittest.TestCase):
    def test_remote_clusters_isolated_from_default(self):
        from app.services.speakers import SpeakerIdentifier
        spk = SpeakerIdentifier()
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        r1 = spk.identify_embedding(a, space="remote")
        r2 = spk.identify_embedding(a, space="remote")
        d1 = spk.identify_embedding(a, space="default")
        self.assertEqual(r1["decision"], "new")
        self.assertEqual(r2["decision"], "clustered")
        self.assertEqual(r1["label"], r2["label"])
        self.assertTrue(r1["label"].startswith("Remote"))
        self.assertEqual(d1["decision"], "new")
        self.assertTrue(d1["label"].startswith("Speaker"))
        self.assertEqual(spk.identify_embedding(a, space="self")["decision"],
                         "self_channel")


class ParseChoiceTests(unittest.TestCase):
    def test_parse_choice(self):
        from app.services.meeting_session import parse_choice
        self.assertEqual(parse_choice("skip"), "skip")
        self.assertEqual(parse_choice("no"), "skip")
        self.assertEqual(parse_choice("yes"), "transcript_only")
        self.assertEqual(parse_choice("audio + transcript"), "keep_receipts")
        self.assertIsNone(parse_choice("what is the weather"))


if __name__ == "__main__":
    unittest.main()
