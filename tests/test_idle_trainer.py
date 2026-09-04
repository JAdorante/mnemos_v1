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


class ColdStartBootstrapTests(unittest.TestCase):
    """Automatic-at-signup: the FIRST run may fire on real+synthetic volume,
    bypassing the organic-growth and saturation gates exactly once."""

    def test_first_run_fires_on_synthetic_volume(self) -> None:
        p = probes(pairs=20, synth_pairs=80, bootstrap_min=100,
                   min_new_pairs=150, lora_saturated=False)
        go, reason = it.should_run({}, p)
        self.assertTrue(go, reason)
        self.assertIn("cold-start bootstrap", reason)

    def test_bootstrap_needs_the_green_light_total(self) -> None:
        go, reason = it.should_run(
            {}, probes(pairs=20, synth_pairs=30, bootstrap_min=100,
                       min_new_pairs=150))
        self.assertFalse(go)
        self.assertIn("new labeled pairs", reason)

    def test_second_run_needs_organic_growth_again(self) -> None:
        state = {"last_run_ts": NOW - 30 * DAY, "pairs_at_last_run": 20}
        go, reason = it.should_run(
            state, probes(pairs=25, synth_pairs=80, bootstrap_min=100,
                          min_new_pairs=150))
        self.assertFalse(go)
        self.assertIn("new labeled pairs", reason)

    def test_bootstrap_still_respects_idle_and_disk(self) -> None:
        p = probes(pairs=20, synth_pairs=80, bootstrap_min=100,
                   min_new_pairs=150, idle_s=10)
        go, _ = it.should_run({}, p)
        self.assertFalse(go)

    def test_synth_due_when_green(self) -> None:
        p = probes(pairs=19, synth_pairs=0, bootstrap_min=100,
                   facts_n=50, min_facts=10, synth_enabled=True)
        due, reason = it.synth_bootstrap_due({}, p)
        self.assertTrue(due, reason)

    def test_synth_not_due_after_done_or_backoff_or_thin_graph(self) -> None:
        base = dict(pairs=19, synth_pairs=0, bootstrap_min=100,
                    facts_n=50, min_facts=10, synth_enabled=True)
        self.assertFalse(it.synth_bootstrap_due(
            {"synth_done": True}, probes(**base))[0])
        self.assertFalse(it.synth_bootstrap_due(
            {"synth_last_ts": NOW - 3600}, probes(**base))[0])
        self.assertFalse(it.synth_bootstrap_due(
            {}, probes(**{**base, "facts_n": 3}))[0])
        self.assertFalse(it.synth_bootstrap_due(
            {}, probes(**{**base, "pairs": 150}))[0])
        self.assertFalse(it.synth_bootstrap_due(
            {}, probes(**{**base, "synth_pairs": 90}))[0])
        self.assertFalse(it.synth_bootstrap_due(
            {}, probes(**{**base, "idle_s": 10}))[0])
        self.assertFalse(it.synth_bootstrap_due(
            {}, probes(**{**base, "synth_enabled": False}))[0])


class HostedProbeTests(unittest.TestCase):
    """QUILL_HEADLESS=1 (hosted container): idle = capture-quiet, AC = moot."""

    def test_hosted_mode_swaps_probes(self) -> None:
        from unittest import mock
        with mock.patch.dict("os.environ", {"QUILL_HEADLESS": "1"}), \
                mock.patch.object(it, "capture_idle_seconds",
                                  return_value=2400.0) as cap, \
                mock.patch.object(it, "idle_seconds") as kb, \
                mock.patch.object(it, "on_ac_power") as ac, \
                mock.patch.object(it, "pair_count", return_value=0), \
                mock.patch.object(it, "lora_saturated_probe",
                                  return_value=True):
            p = it.IdleTrainer()._probes()
        self.assertEqual(p["idle_s"], 2400.0)
        self.assertTrue(p["on_ac"])
        cap.assert_called_once()
        kb.assert_not_called()      # keyboard probe never consulted
        ac.assert_not_called()      # battery probe never consulted

    def test_capture_idle_reads_newest_event(self) -> None:
        from unittest import mock

        class _FakeStore:
            def recent_events(self, *, limit):
                import time as _t
                return [{"time": _t.time() - 900}]

        with mock.patch("app.storage.get_store", return_value=_FakeStore()):
            idle = it.capture_idle_seconds()
        self.assertGreaterEqual(idle, 890)
        self.assertLess(idle, 1000)

    def test_capture_idle_fails_safe_to_active(self) -> None:
        from unittest import mock

        class _EmptyStore:
            def recent_events(self, *, limit):
                return []

        with mock.patch("app.storage.get_store", return_value=_EmptyStore()):
            self.assertEqual(it.capture_idle_seconds(), 0.0)
        with mock.patch("app.storage.get_store",
                        side_effect=RuntimeError("db gone")):
            self.assertEqual(it.capture_idle_seconds(), 0.0)


if __name__ == "__main__":
    unittest.main()
