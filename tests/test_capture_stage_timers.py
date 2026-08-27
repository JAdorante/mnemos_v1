"""Perception Phase 0 — the capture path's stage timers.

The audio pipeline already timed ASR and end-to-end. What was missing is the
breakdown that decides where the next five weeks go: how much of an utterance's
budget is VAD, queue wait, ASR, and the post-ASR tail, split by pipeline. A
faster engine only fixes one of those four, so guessing which one dominates is
how a latency program spends a month on the wrong stage.

These tests hold the instrumentation to its own rules: it must never break
capture, and it must not add a second set of probes for numbers the path
already produced.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.services import audio as audio_mod
from app.services import latency


class _FakeStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record_audio_telemetry(self, **fields):
        self.rows.append(fields)
        return len(self.rows)


class _Pipeline(audio_mod.AudioPipeline):
    """A pipeline with no audio device and no model — only the bookkeeping."""

    def __init__(self, capture: str = "mic") -> None:
        super().__init__(sink=lambda ev: None, capture=capture,
                         source=f"audio.{capture}")


class RecordTeleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _FakeStore()
        self.p = _Pipeline()
        self.patches = [
            patch.object(audio_mod, "get_store", lambda: self.store),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def test_post_ms_is_derived_from_the_asr_done_mark(self) -> None:
        import time
        tele = {"asr_latency_ms": 600.0, "_t_asr_done": time.time() - 0.25,
                "_t_speech_end": time.time() - 1.0}
        self.p._record_tele(tele, "kept")
        row = self.store.rows[0]
        self.assertAlmostEqual(row["post_ms"], 250.0, delta=60.0)

    def test_private_marks_never_reach_the_row(self) -> None:
        """They are scratch state between the worker and this method. A stray
        underscore key would be silently dropped by the column filter today and
        become a mystery column the day someone widens the schema."""
        import time
        tele = {"_t_asr_done": time.time(), "_t_speech_end": time.time()}
        self.p._record_tele(tele, "kept")
        self.assertFalse([k for k in self.store.rows[0] if k.startswith("_")])

    def test_a_dropped_utterance_still_records(self) -> None:
        self.p._record_tele({"queue_wait_ms": 12.0}, "dropped", "empty")
        row = self.store.rows[0]
        self.assertEqual(row["outcome"], "dropped")
        self.assertEqual(row["drop_reason"], "empty")

    def test_telemetry_failure_does_not_reach_the_caller(self) -> None:
        """Rule 1: a broken timer costs a measurement, never an utterance."""
        def boom(**_kw):
            raise RuntimeError("db is gone")

        self.store.record_audio_telemetry = boom
        self.p._record_tele({"asr_latency_ms": 1.0}, "kept")   # must not raise

    def test_telemetry_disabled_still_emits_the_span(self) -> None:
        import time
        from types import SimpleNamespace
        # settings is a frozen dataclass, so swap the module's reference rather
        # than mutating the shared instance every other test also reads.
        off = SimpleNamespace(telemetry=SimpleNamespace(enabled=False))
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"QUILL_DATA_DIR": td,
                                         "QUILL_LATENCY_SPANS": "1"}):
                with patch.object(audio_mod, "settings", off):
                    self.p._record_tele(
                        {"asr_latency_ms": 5.0, "_t_speech_end": time.time()},
                        "kept")
                rows = latency.read_rows()
        self.assertEqual(self.store.rows, [])
        self.assertEqual(len(rows), 1)


class CaptureSpanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp.name,
                                           "QUILL_LATENCY_SPANS": "1"})
        self.env.start()
        self.store = _FakeStore()
        self.gs = patch.object(audio_mod, "get_store", lambda: self.store)
        self.gs.start()

    def tearDown(self) -> None:
        self.gs.stop(); self.env.stop(); self.tmp.cleanup()

    def _record(self, capture="mic", **tele):
        import time
        tele.setdefault("_t_speech_end", time.time() - 1.0)
        _Pipeline(capture)._record_tele(tele, tele.pop("_outcome", "kept"))
        return latency.read_rows()

    def test_one_span_per_utterance_with_every_stage(self) -> None:
        import time
        rows = self._record(vad_ms=40.0, queue_wait_ms=100.0,
                            asr_latency_ms=600.0, channel="mic",
                            engine="whisper:small",
                            _t_asr_done=time.time() - 0.1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["kind"], latency.KIND_CAPTURE)
        self.assertEqual(row["task"], "mic")
        self.assertEqual(row["stages"]["vad"], 40.0)
        self.assertEqual(row["stages"]["queue_wait"], 100.0)
        self.assertEqual(row["stages"]["asr"], 600.0)
        self.assertIn("post", row["stages"])
        self.assertEqual(row["marks"]["engine"], "whisper:small")

    def test_loopback_is_a_separate_task(self) -> None:
        rows = self._record(capture="loopback", asr_latency_ms=1.0)
        self.assertEqual(rows[0]["task"], "loopback")

    def test_dropped_utterances_get_a_span_too(self) -> None:
        """A drop still consumed the budget. Timing only the kept ones hides a
        pipeline that is slow *because* it decides to drop late."""
        import time
        _Pipeline()._record_tele(
            {"asr_latency_ms": 500.0, "_t_speech_end": time.time() - 0.9,
             "_t_asr_done": time.time()}, "dropped", "hallucination")
        rows = latency.read_rows()
        self.assertEqual(rows[0]["marks"]["outcome"], "dropped")
        self.assertEqual(rows[0]["marks"]["drop_reason"], "hallucination")

    def test_no_speech_end_means_no_span_rather_than_a_wrong_one(self) -> None:
        _Pipeline()._record_tele({"asr_latency_ms": 5.0}, "kept")
        self.assertEqual(latency.read_rows(), [])

    def test_spans_off_by_default_writes_nothing(self) -> None:
        import time
        with patch.dict(os.environ, {"QUILL_LATENCY_SPANS": "0"}):
            _Pipeline()._record_tele(
                {"asr_latency_ms": 5.0, "_t_speech_end": time.time()}, "kept")
        self.assertEqual(latency.read_rows(), [])


class VadAccountingTests(unittest.TestCase):
    """`_on_audio` runs on the audio callback thread, so this is the one place
    the VAD cost is observable at all — it runs per 32 ms chunk, not per
    utterance."""

    class _FakeVad:
        """Returns speech-start on the 2nd chunk and speech-end on the 5th."""

        def __init__(self) -> None:
            self.n = 0

        def __call__(self, chunk, return_seconds=False):
            self.n += 1
            if self.n == 2:
                return {"start": 512}
            if self.n == 5:
                return {"end": 2560}
            return None

    def _run(self, p, chunks: int) -> None:
        frame = np.zeros(p.cfg.frame_samples, dtype=np.float32)
        for _ in range(chunks):
            p._on_audio(frame, len(frame), None, None)

    def test_vad_time_travels_with_the_utterance(self) -> None:
        p = _Pipeline()
        p._vad = self._FakeVad()
        self._run(p, 6)
        item = p._utterances.get_nowait()
        self.assertEqual(len(item), 4)
        self.assertIsInstance(item[3], float)
        self.assertGreaterEqual(item[3], 0.0)

    def test_the_counter_resets_between_utterances(self) -> None:
        """Otherwise vad_ms would report the cost of the whole session on every
        row, growing without bound."""
        p = _Pipeline()
        p._vad = self._FakeVad()
        self._run(p, 5)                     # stop ON the speech-end chunk
        p._utterances.get_nowait()
        self.assertEqual(p._vad_ms, 0.0)
        self._run(p, 2)                     # the next utterance starts fresh
        self.assertLess(p._vad_ms, 5.0)

    def test_the_worker_accepts_the_older_three_tuple(self) -> None:
        """A queue drained across a restart must not crash the worker on shape."""
        p = _Pipeline()
        item = (np.zeros(100, dtype=np.float32), 1.0, 2.0)
        audio, _start, end = item[:3]
        vad_ms = item[3] if len(item) > 3 else None
        self.assertIsNone(vad_ms)
        self.assertEqual(len(audio), 100)


class CaptureStagesTests(unittest.TestCase):
    """The aggregator over the new columns."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_stage_"))
        from app.storage import Store
        self.store = Store(db_path=self.tmp / "quill.db",
                           audio_dir=self.tmp / "audio")

    def test_vad_is_reported_beside_the_stages_not_inside_them(self) -> None:
        """VAD runs during speech, before the speech-end the budget starts at.
        Counting it as a stage would make the shares sum to more than 100%."""
        for _ in range(3):
            self.store.record_audio_telemetry(
                outcome="kept", vad_ms=40.0, queue_wait_ms=200.0,
                asr_latency_ms=700.0, total_latency_ms=1000.0, channel="mic")
        out = latency.capture_stages(store=self.store)
        self.assertEqual(out["vad_ms"]["p50"], 40.0)
        self.assertNotIn("vad", {s["stage"] for s in out["stages"]})
        self.assertAlmostEqual(sum(s["share_pct"] for s in out["stages"]),
                               100.0, places=1)

    def test_mic_and_loopback_are_split(self) -> None:
        self.store.record_audio_telemetry(
            outcome="kept", asr_latency_ms=500.0, total_latency_ms=800.0,
            channel="mic", engine="whisper:small")
        self.store.record_audio_telemetry(
            outcome="kept", asr_latency_ms=1500.0, total_latency_ms=2200.0,
            channel="loopback", engine="whisper:small")
        out = latency.capture_stages(store=self.store)
        self.assertEqual(out["by_channel"]["mic"]["total_p90"], 800.0)
        self.assertEqual(out["by_channel"]["loopback"]["total_p90"], 2200.0)
        self.assertEqual(out["engines"], ["whisper:small"])

    def test_rows_without_a_channel_do_not_invent_a_bucket(self) -> None:
        self.store.record_audio_telemetry(
            outcome="kept", asr_latency_ms=1.0, total_latency_ms=2.0)
        self.assertEqual(latency.capture_stages(store=self.store)["by_channel"], {})


class AudioHealthEngineTests(unittest.TestCase):
    """What the console reads: engine provenance, RTF, stages, channel split.

    The support problem these answer: a tester on a flag-flipped build says
    "transcripts got worse". Averaged across engines or channels, the panel
    cannot say which engine or which pipeline they mean."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_health_"))
        from app.storage import Store
        self.store = Store(db_path=self.tmp / "quill.db",
                           audio_dir=self.tmp / "audio")

    def _row(self, **kw):
        base = dict(outcome="kept", audio_duration_ms=2000.0,
                    asr_latency_ms=1000.0, total_latency_ms=1400.0,
                    queue_wait_ms=100.0, post_ms=300.0, vad_ms=40.0,
                    channel="mic", engine="whisper:small")
        base.update(kw)
        self.store.record_audio_telemetry(**base)

    def test_rtf_is_asr_time_over_audio_duration(self) -> None:
        self._row(asr_latency_ms=1000.0, audio_duration_ms=2000.0)
        self._row(asr_latency_ms=3000.0, audio_duration_ms=2000.0)
        self.assertEqual(self.store.audio_health(3600.0)["rtf"], 1.0)

    def test_rtf_ignores_rows_with_no_asr_time(self) -> None:
        """A dropped-before-ASR utterance has duration but no transcribe cost;
        counting its seconds would make the engine look faster than it is."""
        self._row(asr_latency_ms=1000.0, audio_duration_ms=1000.0)
        self._row(outcome="dropped", drop_reason="bad_audio",
                  asr_latency_ms=None, audio_duration_ms=60000.0)
        self.assertEqual(self.store.audio_health(3600.0)["rtf"], 1.0)

    def test_two_engines_in_one_window_are_split_not_averaged(self) -> None:
        self._row(engine="whisper:small", asr_latency_ms=1000.0)
        self._row(engine="parakeet-onnx:tdt-0.6b-v2", asr_latency_ms=200.0)
        by = self.store.audio_health(3600.0)["by_engine"]
        self.assertEqual(set(by), {"whisper:small", "parakeet-onnx:tdt-0.6b-v2"})
        self.assertEqual(by["parakeet-onnx:tdt-0.6b-v2"]["rtf"], 0.1)

    def test_mic_and_loopback_are_split(self) -> None:
        self._row(channel="mic", total_latency_ms=800.0)
        self._row(channel="loopback", total_latency_ms=2400.0)
        by = self.store.audio_health(3600.0)["by_channel"]
        self.assertEqual(by["mic"]["total_latency_ms"]["p50"], 800.0)
        self.assertEqual(by["loopback"]["total_latency_ms"]["p50"], 2400.0)

    def test_the_stage_breakdown_keeps_vad_out_of_the_chain(self) -> None:
        self._row()
        st = self.store.audio_health(3600.0)["stage_ms"]
        self.assertEqual(st["vad"]["p50"], 40.0)
        self.assertEqual(st["queue_wait"]["p50"], 100.0)
        self.assertEqual(st["post"]["p50"], 300.0)
        # queue + asr + post is the end-to-end budget; vad is not in it.
        self.assertAlmostEqual(
            st["queue_wait"]["p50"] + st["asr"]["p50"] + st["post"]["p50"],
            1400.0, delta=1.0)

    def test_a_store_with_no_engine_column_data_omits_the_splits(self) -> None:
        self.store.record_audio_telemetry(outcome="kept", asr_latency_ms=5.0)
        out = self.store.audio_health(3600.0)
        self.assertEqual(out["by_engine"], {})
        self.assertEqual(out["by_channel"], {})


class EvalProvenanceTests(unittest.TestCase):
    """`last_eval_report` — the offline half of the console's engine story."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, **over):
        import json
        report = {"tag": over.pop("tag", "whisper-baseline"),
                  "config": {"engine_id": over.pop("engine_id", "whisper:small")},
                  "overall": {"n_clips": 12, "wer": 0.18, "rtf": 0.42,
                              "raw_hallucination_rate": 0.8,
                              "post_filter_hallucination_rate": 0.0, **over}}
        (self.dir / name).write_text(json.dumps(report), encoding="utf-8")

    def test_it_reads_the_newest_report(self) -> None:
        import os, time
        from app.services import asr
        self._write("report_old.json", tag="old")
        self._write("report_new.json", tag="new")
        os.utime(self.dir / "report_old.json", (time.time() - 900,) * 2)
        out = asr.last_eval_report(str(self.dir))
        self.assertEqual(out["tag"], "new")
        self.assertEqual(out["post_filter_hallucination_rate"], 0.0)

    def test_it_always_reports_when_it_ran(self) -> None:
        """It describes the last run, not the running process. Without a
        timestamp a reader cannot tell a fresh result from a month-old one."""
        from app.services import asr
        self._write("report_a.json")
        self.assertIn("ran_at", asr.last_eval_report(str(self.dir)))

    def test_no_reports_is_empty_not_an_error(self) -> None:
        from app.services import asr
        self.assertEqual(asr.last_eval_report(str(self.dir)), {})

    def test_a_half_written_report_does_not_break_the_panel(self) -> None:
        from app.services import asr
        (self.dir / "report_torn.json").write_text('{"tag": "x", "over',
                                                   encoding="utf-8")
        self.assertEqual(asr.last_eval_report(str(self.dir)), {})


class MigrationTests(unittest.TestCase):
    def test_an_old_store_gains_the_stage_columns(self) -> None:
        import sqlite3

        tmp = Path(tempfile.mkdtemp(prefix="quill_mig_"))
        db = tmp / "quill.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE audio_telemetry ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                     "outcome TEXT NOT NULL, asr_latency_ms REAL)")
        conn.execute("INSERT INTO audio_telemetry (ts, outcome, asr_latency_ms) "
                     "VALUES (1.0, 'kept', 500.0)")
        conn.commit(); conn.close()

        from app.storage import Store
        store = Store(db_path=db, audio_dir=tmp / "audio")
        cols = {r["name"] for r in store._conn.execute(
            "PRAGMA table_info(audio_telemetry)").fetchall()}
        for c in ("queue_wait_ms", "vad_ms", "post_ms", "channel", "engine"):
            self.assertIn(c, cols)
        # and the pre-existing row survived
        n = store._conn.execute("SELECT COUNT(*) FROM audio_telemetry").fetchone()[0]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
