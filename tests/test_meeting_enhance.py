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
        self.env = patch.dict(os.environ, {"QUILL_MEETING_ENHANCE": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
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


if __name__ == "__main__":
    unittest.main()
