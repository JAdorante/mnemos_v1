"""The weekly distillation number (app/services/distill_metrics.py)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services import distill_metrics as dm
from app.storage import Store

WEEK = "2026-W44"
T0, T1 = dm.week_bounds(WEEK)
DAY = 86400


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.calls = []
        self.store = Store(db_path=self.dir / "quill.db",
                           audio_dir=self.dir / "audio")
        self.n = 0

    def call(self, task, provider, t, cost=0.0, ok=True) -> None:
        self.calls.append({"time": t, "task": task, "provider": provider,
                           "ok": ok, "cost_usd": cost})

    def verdict(self, verdict, t, *, parent=None, source="chat.outcome"):
        self.n += 1
        self.store.add_learning_pair({
            "id": f"p{self.n}", "created_at": t, "task_type": "escalation.text",
            "input_text": f"q{self.n}", "local_output": "a",
            "parent_output": parent, "final_target": "a" * 12,
            "verdict": verdict, "verdict_source": source, "source_refs": {},
            "model_tag": None, "embedding_id": None,
            "content_hash": f"h{self.n}", "shadow_eligible": True,
            "human_confirmed": True, "privacy_class": "internal"})

    def run_report(self) -> dict:
        with (self.dir / "model_calls.jsonl").open("w") as f:
            for c in self.calls:
                f.write(json.dumps(c) + "\n")
        return dm.report(self.dir, WEEK)


class AvoidedTests(Base):
    def test_avoided_is_local_minus_parent_valued_at_mean_parent_cost(self):
        for i in range(10):
            self.call("chat", "ollama", T0 + i)
        self.call("chat", "claude", T0 + 20, cost=0.02)
        self.call("chat", "claude", T0 - 30 * DAY, cost=0.04)   # history
        r = self.run_report()
        self.assertEqual(r["parent_calls_avoided"], 9)
        self.assertEqual(r["parent_calls"], 1)
        self.assertEqual(r["local_share"], 0.9)
        self.assertAlmostEqual(r["avoided_usd_est"], 9 * 0.03, places=4)
        self.assertAlmostEqual(r["parent_spend_usd"], 0.02, places=4)

    def test_only_local_first_tasks_failures_and_this_week_count(self):
        self.call("adjudicate", "ollama", T0 + 1)
        self.call("chat", "ollama", T0 + 2, ok=False)
        self.call("chat", "ollama", T1 + 1)
        self.call("chat", "ollama", T0 + 3)
        r = self.run_report()
        self.assertEqual(r["parent_calls_avoided"], 1)
        self.assertEqual(list(r["by_task"]), ["chat"])

    def test_straight_to_parent_never_goes_negative(self):
        self.call("extract", "claude", T0 + 1, cost=0.01)
        self.call("extract", "claude", T0 + 2, cost=0.01)
        r = self.run_report()
        self.assertEqual(r["parent_calls_avoided"], 0)
        self.assertEqual(r["local_share"], 0.0)

    def test_empty_seat(self):
        r = dm.report(self.dir, WEEK)
        self.assertEqual(r["parent_calls_avoided"], 0)
        self.assertIsNone(r["local_share"])


class QualityTests(Base):
    def test_too_few_verdicts_is_said_not_guessed(self):
        self.verdict("accepted", T0 + 1)
        r = self.run_report()
        self.assertEqual(r["quality"]["held"], "too_few_verdicts")

    def test_held_and_dropped_against_the_four_week_baseline(self):
        for i in range(10):
            self.verdict("accepted", T0 - 10 * DAY + i)
        for i in range(5):
            self.verdict("accepted", T0 + i)
        self.assertEqual(self.run_report()["quality"]["held"], "held")
        for i in range(5):
            self.verdict("rejected", T0 + 100 + i)
        q = self.run_report()["quality"]
        self.assertEqual((q["held"], q["local_acceptance"]), ("dropped", 0.5))

    def test_verdicts_on_parent_answers_do_not_count(self):
        for i in range(5):
            self.verdict("rejected", T0 + i, parent="a parent answer")
        self.assertEqual(self.run_report()["quality"]["verdicts"], 0)

    def test_shadow_and_exemplar_uses(self):
        with (self.dir / "shadow_grades.jsonl").open("w") as f:
            for v in ("agree", "agree", "major_disagree"):
                f.write(json.dumps({"ts": T0 + 1, "verdict": v}) + "\n")
        with (self.dir / "exemplar_uses.jsonl").open("w") as f:
            f.write(json.dumps({"ts": T0 + 1, "pair_ids": ["abc"]}) + "\n")
            f.write(json.dumps({"ts": T0 + 2,
                                "pair_ids": ["abc", "peer:s1:x"]}) + "\n")
        r = self.run_report()
        self.assertEqual(r["quality"]["shadow_agree"], 0.667)
        self.assertEqual(r["exemplars"], {"calls_with_exemplars": 2,
                                          "calls_with_peer_exemplars": 1})


class ScriptTests(Base):
    def test_trend_and_save(self):
        import io
        from contextlib import redirect_stdout

        from scripts import distill_weekly
        self.call("chat", "ollama", T0 + 1)
        self.run_report()
        buf = io.StringIO()
        with redirect_stdout(buf):
            distill_weekly.main(["--data-dir", str(self.dir), "--week", WEEK,
                                 "--weeks", "3", "--save"])
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual([ln.split(":")[0] for ln in lines],
                         ["2026-W42", "2026-W43", "2026-W44"])
        self.assertTrue((self.dir / "bench" / "distill_weekly" /
                         f"{WEEK}.json").is_file())
