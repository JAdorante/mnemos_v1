"""Tests for the base-model bake-off harness (scripts/bench_bakeoff.py).

Covers the part that makes the comparison honest — the deferred escalate gate,
the threshold sweep, and matched-risk selection — plus the rollups read off it.
Pure functions only: no Ollama, no embedder.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bench_bakeoff as bo  # noqa: E402


def _r(sim, conf_eff, hard=False, task="chat", lat=1.0):
    """One probe_row-shaped result."""
    return {"id": "x", "task": task, "sim": sim, "pass": sim >= bo.PASS_SIM,
            "hard_escalate": hard, "confidence": conf_eff,
            "conf_effective": conf_eff, "fewshot_n": 0, "latency_s": lat}


class GateTests(unittest.TestCase):
    def test_hard_escalate_ignores_threshold(self) -> None:
        row = _r(0.9, 1.0, hard=True)
        self.assertTrue(bo.escalates(row, 0.0))
        self.assertTrue(bo.escalates(row, 0.95))

    def test_missing_confidence_always_escalates(self) -> None:
        """conf None reads as 'unsure' — production behavior, even at thr 0."""
        self.assertTrue(bo.escalates(_r(0.9, None), 0.0))

    def test_threshold_boundary_is_strict_less_than(self) -> None:
        self.assertFalse(bo.escalates(_r(0.9, 0.6), 0.6))
        self.assertTrue(bo.escalates(_r(0.9, 0.55), 0.6))


class SweepTests(unittest.TestCase):
    def setUp(self) -> None:
        # Two good answers kept cheaply, one confidently-wrong at low threshold.
        self.scored = [_r(0.9, 0.9), _r(0.8, 0.7), _r(0.1, 0.5), _r(0.9, None)]

    def test_rates_are_over_all_rows(self) -> None:
        p = bo.sweep_point(self.scored, 0.4)
        self.assertEqual(p["n_local"], 3)          # the None row escalates
        self.assertEqual(p["stays_local"], 0.75)
        self.assertEqual(p["n_conf_wrong"], 1)     # sim 0.1 stayed local
        self.assertEqual(p["conf_wrong"], 0.25)    # of ALL 4 rows, not of the 3

    def test_raising_threshold_never_raises_risk(self) -> None:
        prev = 1.0
        for thr in bo.SWEEP:
            cw = bo.sweep_point(self.scored, thr)["conf_wrong"]
            self.assertLessEqual(cw, prev)
            prev = cw

    def test_matched_point_picks_cheapest_qualifying(self) -> None:
        p = bo.matched_point(self.scored, 0.0)     # tolerate no wrong answers
        self.assertEqual(p["threshold"], 0.55)     # first thr dropping sim-0.1
        self.assertEqual(p["conf_wrong"], 0.0)
        self.assertEqual(p["n_local"], 2)

    def test_matched_point_spends_the_whole_budget(self) -> None:
        """A budget that already covers the risk buys the loosest threshold."""
        p = bo.matched_point(self.scored, 0.25)
        self.assertEqual(p["threshold"], 0.0)
        self.assertEqual(p["stays_local"], 0.75)

    def test_matched_point_none_when_unreachable(self) -> None:
        """Hard-escalates aside, a model whose wrong answers come with high
        confidence cannot be made as safe as the incumbent at any threshold."""
        self.assertIsNone(bo.matched_point([_r(0.1, 1.0), _r(0.9, 0.9)], 0.0))


class RollTests(unittest.TestCase):
    def test_roll_reports_self_report_not_blended_confidence(self) -> None:
        rows = [_r(0.9, 0.9, lat=1.0), _r(0.5, None, lat=3.0)]
        rows[1]["conf_effective"] = 0.8            # few-shot floor papered over it
        m = bo.roll(rows)
        self.assertEqual(m["conf_missing_rate"], 0.5)
        self.assertEqual(m["n"], 2)
        self.assertEqual(m["mean_sim"], 0.7)
        self.assertEqual(m["pass_rate"], 0.5)
        self.assertEqual(m["p50_latency_s"], 3.0)


if __name__ == "__main__":
    unittest.main()
