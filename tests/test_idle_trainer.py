"""Idle LoRA trainer — the pure go/no-go decision and state handling.

Every environmental fact reaches `should_run` via the probes dict, so these
tests need no GPU, WSL, clock, or Windows APIs. The subprocess/report paths
are exercised only up to their seams.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services import idle_trainer as it

NOW = 1_800_000_000.0
DAY = 86400.0


def probes(**over) -> dict:
    """A fully-green probe set; tests knock out one condition at a time."""
    base = {
        "enabled": True, "now": NOW, "pairs": 400,
        "idle_s": 3600, "on_ac": True, "free_gb": 100,
        "min_new_pairs": 150, "min_idle_s": 1200,
        "min_free_gb": 25, "min_days": 7, "max_fails": 3,
    }
    base.update(over)
    return base


class ShouldRunTests(unittest.TestCase):
    def test_all_green_runs(self) -> None:
        go, reason = it.should_run({}, probes())
        self.assertTrue(go, reason)
        self.assertIn("new pairs", reason)

    def test_disabled_by_default(self) -> None:
        go, reason = it.should_run({}, probes(enabled=False))
        self.assertFalse(go)
        self.assertIn("QUILL_IDLE_TRAIN", reason)

    def test_needs_new_pairs_not_total_pairs(self) -> None:
        # 400 total but 300 already trained on -> only 100 new -> no run.
        state = {"pairs_at_last_run": 300}
        go, reason = it.should_run(state, probes())
        self.assertFalse(go)
        self.assertIn("100 new labeled pairs", reason)

    def test_rate_cap_blocks_recent_run(self) -> None:
        state = {"last_run_ts": NOW - 3 * DAY}
        go, reason = it.should_run(state, probes())
        self.assertFalse(go)
        self.assertIn("rate cap", reason)

    def test_failure_backoff_doubles_wait(self) -> None:
        # 1 failure -> 14d wait: 10 days ago is not enough…
        state = {"last_run_ts": NOW - 10 * DAY, "consecutive_failures": 1}
        go, _ = it.should_run(state, probes())
        self.assertFalse(go)
        # …but 15 days ago is.
        state["last_run_ts"] = NOW - 15 * DAY
        go, _ = it.should_run(state, probes())
        self.assertTrue(go)

    def test_failure_streak_pauses_entirely(self) -> None:
        state = {"consecutive_failures": 3}
        go, reason = it.should_run(state, probes())
        self.assertFalse(go)
        self.assertIn("paused", reason)

    def test_active_user_blocks(self) -> None:
        go, reason = it.should_run({}, probes(idle_s=30))
        self.assertFalse(go)
        self.assertIn("active", reason)

    def test_battery_blocks(self) -> None:
        go, reason = it.should_run({}, probes(on_ac=False))
        self.assertFalse(go)
        self.assertIn("battery", reason)

    def test_low_disk_blocks(self) -> None:
        go, reason = it.should_run({}, probes(free_gb=12))
        self.assertFalse(go)
        self.assertIn("12 GB free", reason)


class StateTests(unittest.TestCase):
    def test_state_roundtrip_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / "trainer_state.json"
            self.assertEqual(it.load_state(p), {})   # missing -> empty
            it.save_state({"last_run_ts": 5, "pairs_at_last_run": 40}, p)
            self.assertEqual(it.load_state(p)["pairs_at_last_run"], 40)

    def test_corrupt_state_is_empty_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trainer_state.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(it.load_state(p), {})


class ProbeSafetyTests(unittest.TestCase):
    def test_probe_failures_fail_safe(self) -> None:
        # Whatever the OS does, probes must return values that BLOCK a run
        # (or, for AC, allow it on desktops) — never raise.
        self.assertGreaterEqual(it.idle_seconds(), 0.0)
        self.assertIsInstance(it.on_ac_power(), bool)
        self.assertGreaterEqual(it.free_gb(), 0.0)
        self.assertGreaterEqual(it.pair_count(), 0)


if __name__ == "__main__":
    unittest.main()
