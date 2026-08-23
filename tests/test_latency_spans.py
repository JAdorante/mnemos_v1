"""Latency program, Phase 0 — the span facility and the stage aggregator.

Two properties matter more than the numbers: the instrumentation must never
break the path it measures, and the cold-start census must actually
discriminate cold from warm. The second one is not theoretical — the first
draft used a 100 ms threshold and classified every warm call as cold, because
Ollama reports a non-zero `load_duration` on resident models too.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import latency


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_lat_"))
        self.env = patch.dict(os.environ, {
            "QUILL_DATA_DIR": str(self.tmp),
            "QUILL_LATENCY_SPANS": "1",
        }, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def rows(self) -> list[dict]:
        return latency.read_rows()


class TraceTests(_Base):
    def test_a_trace_writes_one_row_with_its_stages(self) -> None:
        with latency.trace("chat", task="chat") as tr:
            with tr.stage("retrieval"):
                pass
            tr.add("generation", 120.0)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "chat")
        self.assertEqual(rows[0]["stages"]["generation"], 120.0)
        self.assertIn("retrieval", rows[0]["stages"])

    def test_repeated_stages_sum_rather_than_overwrite(self) -> None:
        """A request that calls the model twice spent both lots of time."""
        with latency.trace("chat") as tr:
            tr.add("generation", 100.0)
            tr.add("generation", 50.0)
        self.assertEqual(self.rows()[0]["stages"]["generation"], 150.0)

    def test_unaccounted_time_is_reported(self) -> None:
        """The gap between total and the sum of stages is the blind spot; if it
        is not surfaced, optimization becomes guesswork."""
        with latency.trace("chat") as tr:
            tr.add("generation", 1.0)
        row = self.rows()[0]
        self.assertIn("unaccounted_ms", row)
        self.assertGreaterEqual(row["unaccounted_ms"], 0.0)

    def test_nested_traces_do_not_fragment_a_request(self) -> None:
        with latency.trace("chat", task="chat") as outer:
            with latency.trace("chat", task="chat") as inner:
                self.assertIs(inner, outer)
                inner.add("generation", 5.0)
        self.assertEqual(len(self.rows()), 1)

    def test_module_level_stage_attaches_to_the_active_trace(self) -> None:
        with latency.trace("chat"):
            with latency.stage("retrieval"):
                pass
        self.assertIn("retrieval", self.rows()[0]["stages"])

    def test_stage_outside_a_trace_is_a_no_op(self) -> None:
        """Instrumentation is left in code that runs both in and out of a
        request; outside one it must do nothing, not raise."""
        with latency.stage("retrieval"):
            pass
        latency.add("generation", 5.0)
        latency.mark("cold_load", True)
        self.assertEqual(self.rows(), [])

    def test_disabled_writes_nothing_and_still_yields(self) -> None:
        with patch.dict(os.environ, {"QUILL_LATENCY_SPANS": "0"}, clear=False):
            with latency.trace("chat") as tr:
                with tr.stage("retrieval"):
                    pass
                tr.add("generation", 10.0)
                tr.mark("x", 1)
        self.assertEqual(self.rows(), [])

    def test_an_exception_still_closes_and_writes_the_trace(self) -> None:
        with self.assertRaises(ValueError):
            with latency.trace("chat") as tr:
                tr.add("generation", 1.0)
                raise ValueError("boom")
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(latency.current())

    def test_a_broken_writer_never_breaks_the_caller(self) -> None:
        """Rule 1: a failed measurement costs a measurement, never a request."""
        with patch.object(latency, "_path",
                          return_value=Path("/nonexistent/dir/x.jsonl")):
            with latency.trace("chat") as tr:
                tr.add("generation", 1.0)   # must not raise


class OllamaTimingTests(_Base):
    """Stage timings are read from the reply, never probed for separately."""

    PAYLOAD = {
        "load_duration": 3_571_000_000,      # ns -> 3571 ms (a real cold load)
        "prompt_eval_duration": 32_000_000,  # 32 ms
        "eval_duration": 1_058_000_000,      # 1058 ms
        "prompt_eval_count": 210,
        "eval_count": 64,
    }

    def test_nanosecond_fields_become_stages(self) -> None:
        with latency.trace("chat", task="chat"):
            latency.record_ollama_timings(self.PAYLOAD)
        stages = self.rows()[0]["stages"]
        self.assertAlmostEqual(stages["model_load"], 3571.0, places=1)
        self.assertAlmostEqual(stages["prefill"], 32.0, places=1)
        self.assertAlmostEqual(stages["generation"], 1058.0, places=1)

    def test_marks_carry_tokens_and_throughput(self) -> None:
        with latency.trace("chat"):
            latency.record_ollama_timings(self.PAYLOAD)
        marks = self.rows()[0]["marks"]
        self.assertEqual(marks["input_tokens"], 210)
        self.assertEqual(marks["output_tokens"], 64)
        self.assertAlmostEqual(marks["tok_per_s"], 60.5, places=0)

    def test_the_raw_load_ms_is_always_kept(self) -> None:
        """So a wrong threshold can be corrected over collected data."""
        with latency.trace("chat"):
            latency.record_ollama_timings(self.PAYLOAD)
        self.assertAlmostEqual(self.rows()[0]["marks"]["load_ms"], 3571.0,
                               places=1)

    def test_a_partial_payload_does_not_raise(self) -> None:
        with latency.trace("chat"):
            latency.record_ollama_timings({"eval_duration": 5_000_000})
            latency.record_ollama_timings({})
            latency.record_ollama_timings({"load_duration": "nonsense"})
        self.assertEqual(len(self.rows()), 1)


class ColdStartCensusTests(_Base):
    """The measurement that was wrong the first time.

    Measured on the reference machine: cold ~3,571 ms, warm ~163 ms. Any
    threshold between them must call these three warm and one cold.
    """

    WARM = [138.6, 155.2, 165.0]
    COLD = [3571.1]

    def _rows(self, loads: list[float]) -> list[dict]:
        return [{"kind": "model", "task": "chat", "total_ms": 1000.0,
                 "stages": {"model_load": v}, "marks": {"load_ms": v}}
                for v in loads]

    def test_warm_calls_are_not_counted_as_cold(self) -> None:
        out = latency.percentiles(self._rows(self.WARM))
        self.assertEqual(out["cold_start"]["cold"], 0)
        self.assertEqual(out["cold_start"]["pct"], 0.0)

    def test_a_real_cold_load_is_caught(self) -> None:
        out = latency.percentiles(self._rows(self.WARM + self.COLD))
        self.assertEqual(out["cold_start"]["cold"], 1)
        self.assertEqual(out["cold_start"]["calls"], 4)
        self.assertEqual(out["cold_start"]["pct"], 25.0)

    def test_the_default_threshold_separates_the_measured_populations(self) -> None:
        self.assertGreater(latency.COLD_LOAD_MS, max(self.WARM) * 2)
        self.assertLess(latency.COLD_LOAD_MS, min(self.COLD) / 2)

    def test_the_census_is_recomputable_at_another_threshold(self) -> None:
        """A wrong threshold must never be baked into collected data."""
        rows = self._rows(self.WARM + self.COLD)
        self.assertEqual(
            latency.percentiles(rows, cold_load_ms=100.0)["cold_start"]["cold"], 4)
        self.assertEqual(
            latency.percentiles(rows, cold_load_ms=5000.0)["cold_start"]["cold"], 0)


class PercentileTests(_Base):
    def test_stages_are_ranked_by_share_of_total_time(self) -> None:
        """The top row of a group is where to optimize — that is the whole
        point of the table."""
        rows = [{"kind": "chat", "task": "chat", "total_ms": 1000.0,
                 "stages": {"generation": 800.0, "prefill": 30.0,
                            "retrieval": 100.0}, "marks": {}}]
        out = latency.percentiles(rows)
        names = [s["stage"] for s in out["rows"][0]["stages"]]
        self.assertEqual(names[0], "generation")
        self.assertGreater(out["rows"][0]["stages"][0]["share_pct"], 50.0)

    def test_groups_split_by_kind_and_task(self) -> None:
        rows = [{"kind": "chat", "task": "chat", "total_ms": 10.0,
                 "stages": {}, "marks": {}},
                {"kind": "capture", "task": "utterance", "total_ms": 20.0,
                 "stages": {}, "marks": {}}]
        out = latency.percentiles(rows)
        self.assertEqual({(r["kind"], r["task"]) for r in out["rows"]},
                         {("chat", "chat"), ("capture", "utterance")})

    def test_percentiles_on_a_known_series(self) -> None:
        rows = [{"kind": "k", "task": "t", "total_ms": float(v),
                 "stages": {}, "marks": {}} for v in range(1, 101)]
        total = latency.percentiles(rows)["rows"][0]["total"]
        self.assertEqual(total["p50"], 50.0)
        self.assertEqual(total["p90"], 90.0)
        self.assertEqual(total["p99"], 99.0)

    def test_a_torn_trail_line_is_skipped_not_fatal(self) -> None:
        with latency.trace("chat") as tr:
            tr.add("generation", 1.0)
        with latency._path().open("a", encoding="utf-8") as f:
            f.write('{"kind": "chat", "total_ms"\n')      # truncated write
        self.assertEqual(len(latency.read_rows()), 1)

    def test_empty_trail_aggregates_cleanly(self) -> None:
        out = latency.percentiles([])
        self.assertEqual(out["rows"], [])
        self.assertIsNone(out["cold_start"]["pct"])


class CaptureBridgeTests(_Base):
    """The audio path reports from audio_telemetry — no second set of timers."""

    def setUp(self) -> None:
        super().setUp()
        from app.storage import Store
        self.store = Store(db_path=self.tmp / "quill.db",
                           audio_dir=self.tmp / "audio")

    def test_stages_are_derived_from_recorded_utterances(self) -> None:
        for _ in range(5):
            self.store.record_audio_telemetry(
                outcome="kept", queue_wait_ms=300.0, asr_latency_ms=600.0,
                total_latency_ms=1000.0)
        out = latency.capture_stages(store=self.store)
        self.assertEqual(out["n"], 5)
        by = {s["stage"]: s for s in out["stages"]}
        self.assertEqual(by["asr"]["p50"], 600.0)
        self.assertEqual(by["queue_wait"]["p50"], 300.0)
        # total - queue - asr, the residual Phase 3.1 would pipeline away.
        self.assertEqual(by["post_asr"]["p50"], 100.0)
        self.assertEqual(out["capture_to_published"]["p50"], 1000.0)

    def test_shares_sum_to_the_whole(self) -> None:
        self.store.record_audio_telemetry(
            outcome="kept", queue_wait_ms=200.0, asr_latency_ms=700.0,
            total_latency_ms=1000.0)
        out = latency.capture_stages(store=self.store)
        self.assertAlmostEqual(sum(s["share_pct"] for s in out["stages"]),
                               100.0, places=1)

    def test_dropped_utterances_are_excluded(self) -> None:
        self.store.record_audio_telemetry(outcome="dropped",
                                          drop_reason="empty",
                                          total_latency_ms=50.0)
        self.assertEqual(latency.capture_stages(store=self.store)["n"], 0)

    def test_no_rows_is_not_an_error(self) -> None:
        out = latency.capture_stages(store=self.store)
        self.assertEqual(out["n"], 0)
        self.assertEqual(out["stages"], [])


class ConsoleRouteTests(_Base):
    def test_console_latency_returns_the_table(self) -> None:
        with latency.trace("chat", task="chat") as tr:
            tr.add("generation", 100.0)
            latency.record_ollama_timings(OllamaTimingTests.PAYLOAD)
        from app.api.adoption import console_latency
        out = console_latency()
        self.assertTrue(out["ok"])
        self.assertEqual(out["traces"], 1)
        self.assertIn("capture", out)
        self.assertIn("cold_start", out)


if __name__ == "__main__":
    unittest.main()
