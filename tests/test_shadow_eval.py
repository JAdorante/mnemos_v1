"""Workstream B — idle shadow evaluation (silent-failure mining).

Acceptance criteria covered:
  * sampling follows the strategy (eligible-only, stratified, uncertainty-first)
  * shadow_eligible=false rows are NEVER selected or sent (privacy invariant)
  * the daily token budget stops the job mid-batch
  * disagreements land as learning pairs flagged human_confirmed=false
  * idle gating: no calls while the user is active / on battery
  * the report generates with agreement rates by task
  * one run per calendar day
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import shadow_eval as se
from app.storage import Store


def mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp())
    return Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")


class _Env(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = mk_store()
        p = patch.dict(os.environ, {
            "QUILL_SHADOW_EVAL": "1",
            "QUILL_LEARNING": "1",
            "QUILL_EXEMPLARS": "0",
            "QUILL_SHADOW_LOCAL_OUTPUTS_PATH": str(self.tmp / "local.jsonl"),
            "QUILL_SHADOW_STATE_PATH": str(self.tmp / "state.json"),
            "QUILL_SHADOW_REPORT_PATH": str(self.tmp / "report.json"),
        }, clear=False)
        p.start()
        self.addCleanup(p.stop)

    def _log(self, task="chat", text="the meeting is on tuesday",
             q="when is my meeting with sarah", conf=0.9):
        return se.log_local_output(task,
                                   messages=[{"role": "user", "content": q}],
                                   text=text, confidence=conf,
                                   model_tag="qwen2.5:7b-instruct")


class LogTests(_Env):
    def test_kept_output_logged_and_classified(self) -> None:
        rid = self._log()
        self.assertIsNotNone(rid)
        rows = se._read_rows(since=0)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["shadow_eligible"])
        self.assertEqual(rows[0]["task"], "chat")

    def test_personal_content_marked_ineligible(self) -> None:
        self._log(q="call sarah at 610-555-0147 or sarah.k@example.com",
                  text="I'll remind you to call Sarah")
        rows = se._read_rows(since=0)
        self.assertFalse(rows[0]["shadow_eligible"])

    def test_disabled_logs_nothing(self) -> None:
        with patch.dict(os.environ, {"QUILL_SHADOW_EVAL": "0"}, clear=False):
            self.assertIsNone(self._log())


class SamplingTests(_Env):
    def test_strategy(self) -> None:
        rows = [
            {"id": "a", "task": "chat", "confidence": 0.9,
             "shadow_eligible": True},
            {"id": "b", "task": "chat", "confidence": 0.6,
             "shadow_eligible": True},
            {"id": "c", "task": "chat", "confidence": None,
             "shadow_eligible": True},
            {"id": "d", "task": "extract", "confidence": 0.95,
             "shadow_eligible": True},
            {"id": "p", "task": "chat", "confidence": 0.99,
             "shadow_eligible": False},           # personal — never sampled
            {"id": "g", "task": "chat", "confidence": 0.1,
             "shadow_eligible": True},
        ]
        picked = se.sample(rows, 3, graded_ids=frozenset({"g"}))
        ids = [r["id"] for r in picked]
        self.assertNotIn("p", ids)                 # privacy invariant
        self.assertNotIn("g", ids)                 # already graded
        # Stratified: both task types appear; uncertainty-first within chat
        # (missing confidence beats 0.6 beats 0.9).
        self.assertIn("d", ids)
        self.assertEqual(ids[0], "c")

    def test_priority_flag_wins(self) -> None:
        rows = [
            {"id": "a", "task": "chat", "confidence": 0.1,
             "shadow_eligible": True},
            {"id": "b", "task": "chat", "confidence": 0.9,
             "shadow_eligible": True, "shadow_priority": True},
        ]
        self.assertEqual(se.sample(rows, 1)[0]["id"], "b")


def _fake_call_factory(log, verdict="major_disagree",
                       corrected="Your meeting is Wednesday at 2pm",
                       tokens=(500, 100)):
    def call(system, user, *, model, max_tokens):
        log.append({"system": system, "user": user, "model": model})
        return (json.dumps({"verdict": verdict,
                            "corrected_output": corrected,
                            "reason_code": "wrong_content"}),
                tokens[0], tokens[1])
    return call


class NightlyRunTests(_Env):
    def test_disagreement_becomes_unconfirmed_pair(self) -> None:
        self._log()
        calls: list = []
        out = se.run_nightly(call=_fake_call_factory(calls), store=self.store)
        self.assertEqual(out["graded"], 1)
        self.assertEqual(out["pairs_recorded"], 1)
        pairs = self.store.list_learning_pairs(verdict="shadow_disagree")
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertFalse(p["human_confirmed"])
        self.assertEqual(p["verdict_source"], "shadow_eval")
        self.assertEqual(p["task_type"], "escalation.text")
        self.assertEqual(p["final_target"], "Your meeting is Wednesday at 2pm")
        self.assertEqual(p["source_refs"]["reason_code"], "wrong_content")

    def test_agree_records_nothing(self) -> None:
        self._log()
        out = se.run_nightly(call=_fake_call_factory([], verdict="agree"),
                             store=self.store)
        self.assertEqual(out["verdicts"].get("agree"), 1)
        self.assertEqual(self.store.list_learning_pairs(limit=10), [])

    def test_personal_rows_never_sent(self) -> None:
        self._log(q="sarah's cell is 610-555-0147 call her",
                  text="I'll remind you")
        calls: list = []
        se.run_nightly(call=_fake_call_factory(calls), store=self.store)
        self.assertEqual(calls, [])               # nothing left the machine

    def test_budget_stops_mid_batch(self) -> None:
        for i in range(5):
            self._log(q=f"question number {i} about the roadmap",
                      text=f"answer number {i}")
        calls: list = []
        with patch.dict(os.environ,
                        {"QUILL_SHADOW_BUDGET_TOKENS": "2000"}, clear=False):
            out = se.run_nightly(
                call=_fake_call_factory(calls, tokens=(800, 100)),
                store=self.store)
        self.assertTrue(out["cutoff"])
        self.assertLess(out["graded"], 5)
        self.assertLessEqual(out["tokens_spent"], 2000)

    def test_one_run_per_day(self) -> None:
        self._log()
        now = 1765000000.0
        se.run_nightly(now=now, call=_fake_call_factory([]), store=self.store)
        out2 = se.run_nightly(now=now + 3600,
                              call=_fake_call_factory([]), store=self.store)
        self.assertEqual(out2.get("skipped"), "already ran today")

    def test_report_rolls_up_agreement(self) -> None:
        self._log()
        se.run_nightly(call=_fake_call_factory([], verdict="agree"),
                       store=self.store)
        rep = se.report(days=7)
        self.assertTrue(rep["enabled"])
        self.assertIn("chat", rep["agreement_by_task"])
        self.assertEqual(rep["agreement_by_task"]["chat"]["agree_rate"], 1.0)


class IdleGateTests(_Env):
    def test_no_calls_while_active_or_on_battery(self) -> None:
        self._log()
        with patch.object(se, "run_nightly") as rn:
            se.maybe_run_idle(idle_s=10.0, on_ac=True)      # user active
            se.maybe_run_idle(idle_s=99999.0, on_ac=False)  # on battery
            rn.assert_not_called()
            se.maybe_run_idle(idle_s=99999.0, on_ac=True)
            rn.assert_called_once()

    def test_disabled_never_runs(self) -> None:
        with patch.dict(os.environ, {"QUILL_SHADOW_EVAL": "0"}, clear=False):
            with patch.object(se, "run_nightly") as rn:
                se.maybe_run_idle(idle_s=99999.0, on_ac=True)
                rn.assert_not_called()


class ConfirmFlowTests(_Env):
    def test_confirm_promotes_to_exemplar_store(self) -> None:
        """B.4(a): a human confirm makes the shadow pair exemplar-eligible."""
        self._log()
        se.run_nightly(call=_fake_call_factory([]), store=self.store)
        pair = self.store.list_learning_pairs(verdict="shadow_disagree")[0]
        ingested: list = []
        from app.services import learning_store as ls
        with patch("app.services.exemplar_store.ingest_pair",
                   side_effect=lambda row, store=None: ingested.append(row)):
            ls.confirm(pair["id"], store=self.store)
        self.assertEqual(len(ingested), 1)
        self.assertTrue(ingested[0]["human_confirmed"])


if __name__ == "__main__":
    unittest.main()
