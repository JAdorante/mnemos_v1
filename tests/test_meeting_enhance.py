"""Meeting Layer P3 — session enhance, templates, hydrate receipts."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

NOW = 1_700_000_000.0


def _store(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db", audio_dir=Path(td) / "audio")


class EligibilityTests(unittest.TestCase):
    def test_calendar_linked_settled(self):
        from app.services import meeting_enhance as me
        sess = {
            "start": NOW - 3600, "end": NOW - 600,
            "duration_s": 3000, "calendar_event_id": "Home|x",
        }
        self.assertTrue(me.is_eligible(sess, NOW))

    def test_short_adhoc_not_eligible(self):
        from app.services import meeting_enhance as me
        sess = {
            "start": NOW - 200, "end": NOW - 100,
            "duration_s": 100, "calendar_event_id": None,
        }
        self.assertFalse(me.is_eligible(sess, NOW))

    def test_long_adhoc_settled(self):
        from app.services import meeting_enhance as me
        sess = {
            "start": NOW - 900, "end": NOW - 400,
            "duration_s": 500, "calendar_event_id": None,
        }
        self.assertTrue(me.is_eligible(sess, NOW))

    def test_unsettled_not_eligible(self):
        from app.services import meeting_enhance as me
        # session just ended — still inside session_gap
        sess = {
            "start": NOW - 600, "end": NOW - 10,
            "duration_s": 590, "calendar_event_id": "Home|y",
        }
        self.assertFalse(me.is_eligible(sess, NOW))


class TemplateTests(unittest.TestCase):
    def test_pick_diligence(self):
        from app.services import meeting_enhance as me
        sess = {"meeting_meta": {"title": "Series A diligence"}}
        self.assertEqual(me.pick_template(sess), "diligence_pitch")

    def test_pick_internal(self):
        from app.services import meeting_enhance as me
        sess = {"meeting_meta": {"title": "Weekly sync"}}
        self.assertEqual(me.pick_template(sess), "internal_sync")

    def test_seed_templates(self):
        from app.services import meeting_enhance as me
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            try:
                me.ensure_templates(store)
                got = store.get_kg_config("meeting_template:external_call")
                self.assertIsNotNone(got)
                self.assertIn("focus", got[1])
            finally:
                store.close()


class EnhancePersistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_me_")
        self.store = _store(self.tmp)
        self.env = patch.dict(os.environ, {
            "QUILL_MEETING_ENHANCE": "1",
            "QUILL_DATA_DIR": self.tmp,
        })
        self.env.start()
        # enhance_session writes first_run + meeting_prefs; keep them off prod data/.
        from app.services import first_run, meeting_mode as mm
        first_run._cached = None
        self._prefs = patch.object(
            mm, "_prefs_path", lambda: Path(self.tmp) / "meeting_prefs.json")
        self._prefs.start()

    def tearDown(self):
        self._prefs.stop()
        self.env.stop()
        from app.services import first_run
        first_run._cached = None
        self.store.close()

    def _session_with_fact(self):
        from app.events import Event, Modality
        from app.services.consolidation import Turn
        # Audio event + turn + commitment fact in window.
        ev = Event(
            time=NOW - 500, modality=Modality.AUDIO,
            raw="I'll send the revised pricing by Thursday",
            summary="turn", source="audio.whisper",
            meta={"audio_path": str(Path(self.tmp) / "fake.wav")},
        )
        eid = self.store.insert(ev)
        turn = Turn(
            start=NOW - 500, end=NOW - 480, speaker="user",
            text="I'll send the revised pricing by Thursday",
            event_ids=[eid], audio_paths=[], n_utterances=1,
        )
        self.store.replace_turns([turn])
        fid = self.store.add_commitment(
            "send revised pricing by Thursday",
            source_event_id=eid,
            source_span="I'll send the revised pricing by Thursday",
            confidence=0.9, extracted_at=NOW - 470,
        )
        sess = {
            "id": 1,
            "start": NOW - 520, "end": NOW - 400,
            "duration_s": 120,
            "calendar_event_id": "Work|pricing",
            "meeting_meta": {
                "title": "Pricing call",
                "attendees": [{"name": "Sarah Chen", "email": "sarah@acme.com"}],
            },
            "event_ids": [eid],
            "speakers": ["user"],
            "text": turn.text,
        }
        return sess, fid, eid

    def test_enhance_writes_meeting_reflection_with_citations(self):
        from app.services import meeting_enhance as me
        sess, fid, eid = self._session_with_fact()

        class FakeRouter:
            def complete_json(self, task, *, system, messages, schema, **kw):
                self.task = task
                self.packet = messages[0]["content"]
                return {
                    "summary": "Discussed pricing timeline.",
                    "confidence": 0.85,
                    "items": [{
                        "kind": "commitment",
                        "text": "Send revised pricing by Thursday",
                        "detail": "Owner: user",
                        "subject": "pricing",
                        "confidence": 0.9,
                        "source_fact_ids": [fid],
                    }],
                }

        fake = FakeRouter()
        with patch("app.services.model_router.router", fake):
            # force: skip settle/eligibility wall for the fixture window
            res = me.enhance_session(sess, store=self.store, force=True)

        self.assertEqual(fake.task, "enhance")
        self.assertIn("Pricing call", fake.packet)
        self.assertIsNotNone(res.get("reflection_id"))
        header = self.store.get_reflection(res["reflection_id"])
        self.assertEqual(header["scope"], "meeting")
        self.assertEqual(header["subject_type"], "session")
        items = self.store.reflection_items(res["reflection_id"])
        kinds = {it["kind"] for it in items}
        self.assertIn("commitment", kinds)
        cited = next(it for it in items if it["kind"] == "commitment")
        self.assertIn(fid, cited["source_fact_ids"])

        # First-win deep-link must be session-scoped (never a raw reflection id).
        from app.services import first_run
        pend = first_run.load().get("pending_first_win")
        self.assertIsInstance(pend, dict)
        self.assertEqual(pend.get("href"), "/meetings/1")

        # Idempotent
        res2 = me.enhance_session(sess, store=self.store, force=False)
        self.assertEqual(res2.get("skipped"), "already enhanced")

    def test_hydrate_includes_playback_fields(self):
        from app.services import meeting_enhance as me
        sess, fid, eid = self._session_with_fact()

        class FakeRouter:
            def complete_json(self, *a, **k):
                return {
                    "summary": "Pricing.",
                    "confidence": 0.8,
                    "items": [{
                        "kind": "commitment",
                        "text": "Send pricing",
                        "detail": "", "subject": "",
                        "confidence": 0.9,
                        "source_fact_ids": [fid],
                    }],
                }

        with patch("app.services.model_router.router", FakeRouter()):
            res = me.enhance_session(sess, store=self.store, force=True)
        note = me.hydrate_meeting_note(
            self.store, self.store.get_reflection(res["reflection_id"]))
        self.assertEqual(note["title"], "Pricing call")
        commit = next(i for i in note["items"] if i["kind"] == "commitment")
        self.assertTrue(commit["evidence"])
        ev = commit["evidence"][0]
        self.assertEqual(ev["fact_id"], fid)
        self.assertIn("play_path", ev)
        self.assertIn("span_highlight", ev)

    def test_drops_invented_fact_ids(self):
        from app.services import meeting_enhance as me
        sess, fid, eid = self._session_with_fact()

        class FakeRouter:
            def complete_json(self, *a, **k):
                return {
                    "summary": "x",
                    "confidence": 0.5,
                    "items": [{
                        "kind": "decision",
                        "text": "Go ahead",
                        "detail": "", "subject": "",
                        "confidence": 0.5,
                        "source_fact_ids": [fid, 99999],
                    }],
                }

        with patch("app.services.model_router.router", FakeRouter()):
            res = me.enhance_session(sess, store=self.store, force=True)
        items = self.store.reflection_items(res["reflection_id"])
        dec = next(i for i in items if i["kind"] == "decision")
        self.assertEqual(dec["source_fact_ids"], [fid])


class ModelRouterEnhanceTests(unittest.TestCase):
    def test_enhance_maps_to_sonnet(self):
        from app.services.model_router import MODELS, ModelRouter
        self.assertIn("enhance", MODELS)
        self.assertIn("sonnet", MODELS["enhance"].lower())
        r = ModelRouter()
        self.assertEqual(r.model_for("enhance"), MODELS["enhance"])


class MeetingSessionNoteTests(unittest.TestCase):
    """One note per recorded MeetingSession, keyed by its own id."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_msn_")
        self.store = _store(self.tmp)
        self.env = patch.dict(os.environ, {
            "QUILL_MEETING_ENHANCE": "1",
            "QUILL_DATA_DIR": self.tmp,
        })
        self.env.start()
        from app.services import first_run, meeting_mode as mm, meeting_session as ms
        ms.reset()
        first_run._cached = None
        self._prefs = patch.object(
            mm, "_prefs_path", lambda: Path(self.tmp) / "meeting_prefs.json")
        self._prefs.start()
        self.calls = 0

    def tearDown(self):
        self._prefs.stop()
        self.env.stop()
        from app.services import first_run, meeting_session as ms
        ms.reset()
        first_run._cached = None
        self.store.close()

    def _router(self, fid):
        outer = self

        class FakeRouter:
            def complete_json(self_, *a, **k):
                outer.calls += 1
                return {"summary": "What happened.", "confidence": 0.8,
                        "items": [{"kind": "next_step", "text": "Send it",
                                   "detail": "", "subject": "", "confidence": 0.9,
                                   "source_fact_ids": [fid] if fid else []}]}
        return FakeRouter()

    def _meeting(self, *, consent="transcript_only", n=4, title="All In Meeting Test 3",
                 start=NOW - 900, dur=120):
        from app.events import Event, Modality
        from app.services.consolidation import Turn
        row = self.store.insert_meeting_session(
            title=title, source="manual", consent=consent, status="ended",
            t_start=start, t_end=start + 3600, entered_at=start,
            ended_at=start + dur, created_at=start)
        msid = int(row["id"])
        eids = []
        for i in range(n):
            eids.append(self.store.insert(Event(
                time=start + 10 + i * 20, modality=Modality.AUDIO,
                raw=f"we agreed to send the deck {i}", summary="turn",
                source="audio.web_mic",
                meta={"meeting_session_id": msid})))
        self.store.replace_turns([Turn(
            start=start + 10, end=start + 10 + n * 20, speaker="user",
            text="we agreed to send the deck", event_ids=list(eids),
            audio_paths=[], n_utterances=n)])
        fid = self.store.add_commitment(
            "send the deck", source_event_id=eids[0],
            source_span="send the deck", confidence=0.9, extracted_at=start + 30)
        return msid, fid, eids

    def test_run_once_notes_each_recorded_meeting_by_its_own_id(self):
        from app.services import meeting_enhance as me
        from app.services.sessions import Session
        msid, fid, eids = self._meeting()
        # The speech session that contains it would have been a legacy note.
        self.store.replace_sessions([Session(
            start=NOW - 920, end=NOW - 700, speakers=["user"], text="t",
            turn_ids=[], event_ids=list(eids), n_turns=1, n_utterances=4,
            calendar_event_id="Work|x")])
        with patch("app.services.model_router.router", self._router(fid)):
            res = me.run_once(self.store)
        self.assertEqual(res["enhanced"], 1, res)
        self.assertEqual(self.calls, 1, "one note, not one per id space")
        rid = res["results"][0]["reflection_id"]
        r = self.store.get_reflection(rid)
        self.assertEqual(r["subject_type"], "meeting_session")
        self.assertEqual(int(r["subject_id"]), msid)
        self.assertTrue((r["summary"] or "").startswith("All In Meeting Test 3"))
        note = me.hydrate_meeting_note(self.store, r)
        self.assertEqual(note["title"], "All In Meeting Test 3")
        self.assertEqual(note["meeting_session_id"], msid)
        self.assertEqual(note["privacy"]["retention"]["source"], "meeting_session")
        self.assertEqual(me.note_href_for_session(self.store, msid),
                         f"/meeting/note/{rid}")
        with patch("app.services.model_router.router", self._router(fid)):
            again = me.run_once(self.store)
        self.assertEqual(again["enhanced"], 0, "idempotent by meeting id")

    def test_meeting_already_covered_by_a_legacy_note_is_not_renoted(self):
        from app.services import meeting_enhance as me
        msid, fid, _ = self._meeting()
        self.store.add_reflection(
            scope="meeting", subject_type="session", subject_id=69,
            period_start=NOW - 1000, period_end=NOW - 600,
            summary="Meeting\n\nold note", model="x", confidence=0.5,
            created_at=NOW - 500)
        with patch("app.services.model_router.router", self._router(fid)):
            res = me.run_once(self.store)
        self.assertEqual(res["enhanced"], 0)
        self.assertEqual(self.calls, 0)

    def test_a_false_start_is_not_worth_a_model_call(self):
        from app.services import meeting_enhance as me
        msid, fid, _ = self._meeting(n=2, dur=16)
        with patch("app.services.model_router.router", self._router(fid)):
            res = me.run_once(self.store)
        self.assertEqual(res["enhanced"], 0)
        self.assertEqual(self.calls, 0)

    def test_hydrate_tells_removed_audio_from_none(self):
        from app.events import Event, Modality
        from app.services import meeting_enhance as me
        stripped = self.store.insert(Event(
            time=NOW - 100, modality=Modality.AUDIO, raw="send the deck",
            summary="t", source="audio.web_mic",
            meta={"audio_stripped": True, "audio_stripped_at": NOW - 50}))
        silent = self.store.insert(Event(
            time=NOW - 90, modality=Modality.AUDIO, raw="and the invoice",
            summary="t", source="audio.web_mic", meta={}))
        f1 = self.store.add_commitment("send the deck", source_event_id=stripped,
                                       source_span="send the deck",
                                       confidence=0.9, extracted_at=NOW - 80)
        f2 = self.store.add_commitment("send the invoice", source_event_id=silent,
                                       source_span="the invoice",
                                       confidence=0.9, extracted_at=NOW - 80)
        rid = self.store.add_reflection(
            scope="meeting", subject_type="session", subject_id=1,
            period_start=NOW - 120, period_end=NOW - 60, summary="M\n\ns",
            model="x", confidence=0.5, created_at=NOW)
        self.store.add_reflection_item(
            rid, kind="commitment", text="Send both", detail="", subject="",
            confidence=0.9, source_fact_ids=[f1, f2], created_at=NOW)
        note = me.hydrate_meeting_note(self.store, self.store.get_reflection(rid))
        ev = {e["fact_id"]: e for e in note["items"][0]["evidence"]}
        self.assertEqual(ev[f1]["audio_state"], "removed")
        self.assertEqual(ev[f2]["audio_state"], "none")
        self.assertFalse(ev[f1]["playable"])

    def test_hydrate_offsets_land_inside_a_real_clip(self):
        import wave
        from app.events import Event, Modality
        from app.services import meeting_enhance as me
        path = Path(self.tmp) / "audio" / "clip.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000 * 4)          # 4 s of silence
        eid = self.store.insert(Event(
            time=NOW - 100, modality=Modality.AUDIO,
            raw="alpha beta gamma delta epsilon zeta", summary="t",
            source="audio.web_mic", meta={"audio_path": str(path)}))
        fid = self.store.add_commitment("gamma", source_event_id=eid,
                                        source_span="gamma delta",
                                        confidence=0.9, extracted_at=NOW - 80)
        rid = self.store.add_reflection(
            scope="meeting", subject_type="session", subject_id=1,
            period_start=NOW - 120, period_end=NOW - 60, summary="M\n\ns",
            model="x", confidence=0.5, created_at=NOW)
        self.store.add_reflection_item(
            rid, kind="decision", text="Gamma", detail="", subject="",
            confidence=0.9, source_fact_ids=[fid], created_at=NOW)
        note = me.hydrate_meeting_note(self.store, self.store.get_reflection(rid))
        ev = note["items"][0]["evidence"][0]
        self.assertEqual(ev["audio_state"], "playable")
        self.assertIsNotNone(ev["clip_start_s"])
        self.assertGreater(ev["clip_start_s"], 0.0, "the words are mid-clip")
        self.assertLess(ev["clip_start_s"], ev["clip_end_s"])
        self.assertLessEqual(ev["clip_end_s"], 4.0)

    def test_page_has_a_visible_player_and_honest_labels(self):
        from app.api.meeting_page import MEETING_PAGE
        self.assertIn('id="playerBar"', MEETING_PAGE)
        self.assertIn("<audio id=\"player\" controls", MEETING_PAGE)
        self.assertNotIn("#player{display:none}", MEETING_PAGE)
        self.assertIn("audio removed · transcript-only", MEETING_PAGE)
        self.assertIn("no audio captured", MEETING_PAGE)
        self.assertIn("data-start=", MEETING_PAGE)
        self.assertIn("meeting_session_id: msid || null", MEETING_PAGE)


if __name__ == "__main__":
    unittest.main()
