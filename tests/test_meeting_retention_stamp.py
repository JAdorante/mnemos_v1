"""Retention follows the stamp, not the clock.

On the first pilot night a keep-receipts meeting lost all of its clips: consent
lived on the MeetingSession, retention was keyed by the derived speech session,
nothing bridged them for a manual meeting, and the settle-time default stripped
every event in the speech session. These pin the bridge.
"""
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


class _Base(unittest.TestCase):
    def setUp(self):
        from app.services import meeting_mode as mm, meeting_session as ms
        ms.reset()
        self.tmp = tempfile.mkdtemp(prefix="quill_stamp_")
        self.store = _store(self.tmp)
        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp})
        self.env.start()
        self._prefs = patch.object(
            mm, "_prefs_path", lambda: Path(self.tmp) / "meeting_prefs.json")
        self._prefs.start()
        self.audio_dir = Path(self.tmp) / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        from app.services import meeting_session as ms
        ms.reset()
        self._prefs.stop()
        self.env.stop()
        try:
            self.store.close()
        except Exception:
            pass

    def _wav(self, name: str) -> Path:
        p = self.audio_dir / f"{name}.wav"
        p.write_bytes(b"RIFF...." + b"\x00" * 32)
        return p

    def _event(self, t: float, wav: Path | None, msid: int | None = None,
               raw: str = "words") -> int:
        from app.events import Event, Modality
        meta = {}
        if wav is not None:
            meta["audio_path"] = str(wav)
        if msid is not None:
            meta["meeting_session_id"] = int(msid)
        return self.store.insert(Event(
            time=t, modality=Modality.AUDIO, raw=raw, summary=raw,
            source="audio.web_mic", meta=meta))

    def _meeting(self, consent: str, start: float, end: float, title="M") -> int:
        row = self.store.insert_meeting_session(
            title=title, source="manual", consent=consent, status="ended",
            t_start=start, t_end=end + 3600, entered_at=start, ended_at=end,
            created_at=start)
        return int(row["id"])

    def _speech_session(self, start: float, end: float, event_ids: list[int]):
        from app.services.sessions import Session
        self.store.replace_sessions([Session(
            start=start, end=end, speakers=["user"], text="t",
            turn_ids=[], event_ids=list(event_ids),
            n_turns=1, n_utterances=len(event_ids))])
        return self.store.recent_sessions(limit=1)[0]


class ApplyDefaultTests(_Base):
    def test_each_event_follows_its_own_meetings_consent(self):
        from app.services import meeting_mode as mm
        ms_t = self._meeting("transcript_only", NOW - 900, NOW - 700)
        ms_k = self._meeting("keep_receipts", NOW - 600, NOW - 400)
        w1, w2, w3 = self._wav("t"), self._wav("k"), self._wav("gap")
        e1 = self._event(NOW - 800, w1, ms_t)
        e2 = self._event(NOW - 500, w2, ms_k)
        e3 = self._event(NOW - 650, w3)           # between meetings, unstamped
        sess = self._speech_session(NOW - 900, NOW - 400, [e1, e2, e3])

        out = mm.apply_default_for_session(self.store, sess)
        self.assertTrue(out["ok"])
        self.assertFalse(w1.exists(), "transcript-only meeting: stripped")
        self.assertTrue(w2.exists(), "keep-receipts meeting: KEPT")
        self.assertFalse(w3.exists(), "unstamped: the default (transcript-only)")
        self.assertEqual(out["kept_events"], 1)
        self.assertEqual(out["stripped_events"], 2)
        self.assertEqual(out["by_meeting_session"],
                         {str(ms_t): "transcript_only", str(ms_k): "keep_receipts"})
        self.assertIsNone(mm.apply_default_for_session(self.store, sess),
                          "applied once per session")

    def test_window_and_session_strips_leave_keep_receipts_alone(self):
        from app.services import meeting_mode as mm
        ms_k = self._meeting("keep_receipts", NOW - 600, NOW - 400)
        wk, wu = self._wav("k"), self._wav("u")
        ek = self._event(NOW - 500, wk, ms_k)
        eu = self._event(NOW - 450, wu)
        out = mm.strip_session_audio(self.store, t0=NOW - 700, t1=NOW - 300)
        self.assertEqual(out["n_events"], 1)
        self.assertTrue(wk.exists())
        self.assertFalse(wu.exists())
        sess = self._speech_session(NOW - 700, NOW - 300, [ek, eu])
        self._wav("u")                                   # bring the file back
        out = mm.strip_session_audio(self.store, session_id=sess["id"])
        self.assertTrue(wk.exists(), "a derived-session strip cannot override consent")

    def test_strip_by_meeting_touches_only_its_stamped_events(self):
        from app.services import meeting_mode as mm
        ms_t = self._meeting("transcript_only", NOW - 900, NOW - 700)
        w1, wb = self._wav("mine"), self._wav("bystander")
        self._event(NOW - 800, w1, ms_t)
        self._event(NOW - 790, wb)                       # same minute, not stamped
        out = mm.strip_session_audio(self.store, meeting_session_id=ms_t)
        self.assertEqual(out["n_events"], 1)
        self.assertFalse(w1.exists())
        self.assertTrue(wb.exists())
        self.assertEqual(
            mm.strip_session_audio(self.store, meeting_session_id=ms_t + 99)
            .get("skipped"), "no_events")


class RetentionRecordTests(_Base):
    def test_retention_for_reads_the_meetings_consent(self):
        from app.services import meeting_mode as mm
        ms_k = self._meeting("keep_receipts", NOW - 600, NOW - 400)
        ret = mm.retention_for(meeting_session_id=ms_k, store=self.store)
        self.assertEqual(ret["retention"], "keep_receipts")
        self.assertEqual(ret["source"], "meeting_session")
        self.assertFalse(ret["is_default"])
        self.assertEqual(ret["key"], f"meeting:{ms_k}")

    def test_set_retention_by_meeting_writes_the_row_and_prefs(self):
        from app.services import meeting_mode as mm
        ms_t = self._meeting("transcript_only", NOW - 600, NOW - 400)
        out = mm.set_session_retention(
            "keep_receipts", meeting_session_id=ms_t, store=self.store, apply=True)
        self.assertTrue(out["ok"])
        self.assertEqual(self.store.get_meeting_session(ms_t)["consent"],
                         "keep_receipts")
        self.assertIn(f"meeting:{ms_t}", mm.load_prefs()["sessions"])
        self.assertEqual(mm.session_key(session_id=5, meeting_session_id=7),
                         "meeting:7", "the meeting's id wins")

    def test_decide_records_under_the_meetings_own_key(self):
        from app.services import meeting_mode as mm, meeting_session as ms
        with patch("app.services.meeting_session.get_store", return_value=self.store), \
             patch("app.services.meeting_mode.enter", return_value={"ok": True}):
            out = ms.start_manual(title="Class", consent="keep_receipts",
                                  store=self.store)
        self.assertTrue(out["ok"])
        sid = int(out["session"]["id"])
        rows = mm.load_prefs()["sessions"]
        self.assertIn(f"meeting:{sid}", rows, "manual meeting: no calendar id, still recorded")
        self.assertEqual(rows[f"meeting:{sid}"]["retention"], "keep_receipts")
        self.assertEqual(self.store.get_meeting_session(sid)["consent"], "keep_receipts")


class EndTests(_Base):
    def _start(self, consent: str) -> int:
        from app.services import meeting_session as ms
        with patch("app.services.meeting_session.get_store", return_value=self.store), \
             patch("app.services.meeting_mode.enter", return_value={"ok": True}):
            out = ms.start_manual(title="Class", consent=consent, store=self.store)
        return int(out["session"]["id"])

    def test_end_strips_only_what_it_stamped(self):
        from app.events import Event, Modality
        from app.services import meeting_session as ms
        import time as _t
        sid = self._start("transcript_only")
        now = _t.time()
        w_mine, w_by = self._wav("mine"), self._wav("bystander")
        ev = ms.stamp_event(Event(
            time=now, modality=Modality.AUDIO, raw="hi", summary="hi",
            source="audio.web_mic", meta={"audio_path": str(w_mine)}))
        self.assertEqual(ev.meta["meeting_session_id"], sid)
        self.store.insert(ev)
        self._event(now + 1, w_by)                       # same window, unstamped
        with patch("app.services.meeting_mode.exit_mode", return_value={"ok": True}):
            out = ms.end(store=self.store)
        self.assertTrue(out["ok"])
        self.assertFalse(w_mine.exists())
        self.assertTrue(w_by.exists(), "a wall-clock window would have taken this too")

    def test_end_kicks_the_note_pipeline_only_with_a_live_worker(self):
        from app.services import meeting_session as ms
        sid = self._start("keep_receipts")
        calls: list = []

        class _Thread:
            def is_alive(self):
                return True

        class _Worker:
            _thread = _Thread()

            def enqueue(self, kind, payload=None, *, unique=False):
                calls.append((kind, unique))
                return 1

        timers: list = []

        class _Timer:
            def __init__(self, delay, fn):
                timers.append(delay)
                self.daemon = False
                self.name = ""

            def start(self):
                pass

        with patch("app.services.worker.worker", _Worker()), \
             patch("app.services.meeting_session.threading.Timer", _Timer), \
             patch("app.services.meeting_mode.exit_mode", return_value={"ok": True}):
            out = ms.end(store=self.store)
        self.assertTrue(out["ok"])
        self.assertEqual(calls, [("consolidate", True)], "fold the turns now")
        self.assertEqual(len(timers), 1, "and again once the last turn settles")
        self.assertGreater(timers[0], 60)
        # Without a running worker the kick is a no-op, not a stray job row.
        got = ms.kick_note_pipeline(sid)
        self.assertEqual(got.get("skipped"), "worker not running")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
