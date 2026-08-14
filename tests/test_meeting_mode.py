"""Meeting Layer P5 — meeting mode, retention, audio strip."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

NOW = 1_700_000_000.0


def _store(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db", audio_dir=Path(td) / "audio")


class PrefsTests(unittest.TestCase):
    def test_default_retention_roundtrip(self):
        from app.services import meeting_mode as mm
        with tempfile.TemporaryDirectory() as td:
            with patch.object(mm, "_prefs_path",
                              lambda: Path(td) / "meeting_prefs.json"):
                self.assertEqual(mm.default_retention(), "transcript_only")
                mm.set_default_retention("keep_receipts")
                self.assertEqual(mm.default_retention(), "keep_receipts")


class EnterExitTests(unittest.TestCase):
    def test_enter_patches_and_exit_restores(self):
        from app.config import settings
        from app.services import meeting_mode as mm
        with tempfile.TemporaryDirectory() as td:
            with patch.object(mm, "_prefs_path",
                              lambda: Path(td) / "meeting_prefs.json"):
                mm.set_default_retention("keep_receipts")
                before = {
                    "save_audio": bool(settings.storage.save_audio),
                    "min_conf": float(settings.facts.min_conf),
                }
                # Force known baseline
                object.__setattr__(settings.storage, "save_audio", False)
                object.__setattr__(settings.facts, "min_conf", 0.35)
                try:
                    import time as _time
                    out = mm.enter(until=_time.time() + 600, title="Pricing",
                                   calendar_event_id="Work|x", source="test")
                    self.assertTrue(out["ok"])
                    self.assertTrue(mm.status()["active"])
                    self.assertTrue(settings.storage.save_audio)
                    self.assertLess(settings.facts.min_conf, 0.35)
                    mm.exit_mode(reason="test")
                    self.assertFalse(mm.status()["active"])
                finally:
                    object.__setattr__(settings.storage, "save_audio",
                                       before["save_audio"])
                    object.__setattr__(settings.facts, "min_conf",
                                       before["min_conf"])
                    with mm._lock:
                        mm._runtime.update({
                            "active": False, "snapshot": None,
                            "until": None, "title": "",
                            "calendar_event_id": None, "session_id": None,
                        })


class StripAudioTests(unittest.TestCase):
    def test_strip_deletes_wav_marks_facts_keeps_commitment_open(self):
        from app.events import Event, Modality
        from app.services import meeting_mode as mm
        from app.services.sessions import Session

        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            audio_dir = Path(td) / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            wav = audio_dir / "clip.wav"
            wav.write_bytes(b"RIFF...." + b"\x00" * 32)
            try:
                ev = Event(
                    time=NOW - 100, modality=Modality.AUDIO,
                    raw="I'll send the deck", summary="turn",
                    source="audio.whisper",
                    meta={"audio_path": str(wav)},
                )
                eid = store.insert(ev)
                # Ensure column/meta both see the path
                with store._lock:
                    store._conn.execute(
                        "UPDATE events SET audio_path = ? WHERE id = ?",
                        (str(wav), eid))
                    store._conn.commit()
                fid = store.add_commitment(
                    "send the deck", source_event_id=eid,
                    source_span="I'll send the deck",
                    confidence=0.9, extracted_at=NOW - 90,
                )
                store.replace_sessions([Session(
                    start=NOW - 120, end=NOW - 40,
                    speakers=["user"], text="deck",
                    turn_ids=[], event_ids=[eid],
                    n_turns=1, n_utterances=1,
                    calendar_event_id="Work|x",
                    meeting_meta={"title": "Pricing"},
                )])
                # Resolve session id
                sess = store.recent_sessions(limit=1)[0]
                out = mm.strip_session_audio(store, session_id=sess["id"])
                self.assertTrue(out["ok"])
                self.assertGreaterEqual(out["n_files"], 1)
                self.assertFalse(wav.exists())
                fact = store.get_fact(fid)
                self.assertEqual(fact.get("state"), "evidence_removed")
                # Commitment row still open (not cancelled)
                with store._lock:
                    crow = store._conn.execute(
                        "SELECT status, state FROM commitments WHERE fact_id=?",
                        (fid,)).fetchone()
                self.assertEqual(crow["status"], "open")
                # Event kept, audio cleared
                emap = store.by_ids_map([eid])
                self.assertIn(eid, emap)
                meta = emap[eid].meta or {}
                self.assertTrue(meta.get("audio_stripped"))
                self.assertFalse(meta.get("audio_path"))
            finally:
                store.close()


class ConsiderOfferTests(unittest.TestCase):
    def test_consider_offers_starting_event(self):
        from app.services import meeting_mode as mm
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            try:
                store.upsert_calendar_event(
                    event_id="Work|meet1", calendar="Work", uid="u1",
                    title="Standup", start=NOW - 10, end=NOW + 1800,
                    attendees=[], updated_at=NOW,
                )
                worker = MagicMock()
                worker.propose_meeting_record.return_value = True
                from app.services import meeting_session as _ms
                _ms.reset()
                with patch.object(mm, "_prefs_path",
                                  lambda: Path(td) / "meeting_prefs.json"), \
                     patch("app.services.agent_bridge.worker", worker), \
                     patch.object(mm, "enabled", return_value=True):
                    # Ensure not already active
                    mm.exit_mode(reason="reset")
                    out = mm.consider_offer(store, now=NOW)
                self.assertTrue(out.get("offered") or out.get("ok"))
                worker.propose_meeting_record.assert_called_once()
                args = worker.propose_meeting_record.call_args[0][0]
                self.assertEqual(args["calendar_event_id"], "Work|meet1")
            finally:
                store.close()


class RetentionApplyTests(unittest.TestCase):
    def test_set_session_retention_transcript_strips(self):
        from app.events import Event, Modality
        from app.services import meeting_mode as mm
        from app.services.sessions import Session

        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            audio_dir = Path(td) / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            wav = audio_dir / "a.wav"
            wav.write_bytes(b"x" * 40)
            try:
                ev = Event(
                    time=NOW - 50, modality=Modality.AUDIO,
                    raw="hi", summary="t", source="audio.whisper",
                    meta={"audio_path": str(wav)},
                )
                eid = store.insert(ev)
                store.replace_sessions([Session(
                    start=NOW - 60, end=NOW - 20,
                    speakers=["user"], text="hi",
                    turn_ids=[], event_ids=[eid],
                    n_turns=1, n_utterances=1,
                )])
                sid = store.recent_sessions(limit=1)[0]["id"]
                with patch.object(mm, "_prefs_path",
                                  lambda: Path(td) / "meeting_prefs.json"):
                    out = mm.set_session_retention(
                        "transcript_only", session_id=sid, store=store)
                    self.assertTrue(out["ok"])
                    self.assertFalse(wav.exists())
                    ret = mm.retention_for(sid)
                    self.assertEqual(ret["retention"], "transcript_only")
                    self.assertTrue(ret["stripped"])
            finally:
                store.close()


class NotePrivacyTests(unittest.TestCase):
    def test_hydrate_includes_privacy(self):
        from app.services import meeting_enhance as me
        from app.services import meeting_mode as mm
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            try:
                with patch.object(mm, "_prefs_path",
                                  lambda: Path(td) / "meeting_prefs.json"):
                    rid = store.add_reflection(
                        scope="meeting",
                        summary="Call · Work\nBody",
                        period_start=NOW - 100, period_end=NOW - 40,
                        subject_type="session", subject_id=3,
                        model="t", confidence=0.8, created_at=NOW - 30,
                    )
                    note = me.hydrate_meeting_note(
                        store, store.get_reflection(rid))
                self.assertIn("privacy", note)
                self.assertIn("retention", note["privacy"])
                self.assertIn("consent", note["privacy"])
            finally:
                store.close()


class ProposeMeetingModeTests(unittest.TestCase):
    def test_propose_adds_offer(self):
        from app.services.agent_bridge import AgentWorker
        w = AgentWorker()
        shown = w.propose_meeting_mode({
            "title": "Pricing",
            "calendar_event_id": "Work|x",
            "start": NOW, "end": NOW + 600,
            "default_retention": "transcript_only",
        })
        self.assertTrue(shown)
        pend = w.pending_todo
        self.assertEqual(pend["kind"], "meeting_record")
        self.assertIn("Meeting starting", pend["message"])
        self.assertTrue(pend.get("choices"))


if __name__ == "__main__":
    unittest.main()
