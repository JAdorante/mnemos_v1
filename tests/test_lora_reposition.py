"""Workstream E — LoRA pipeline repositioned as the graduation path.

Acceptance criteria covered:
  * curation pulls from learning_pairs under the new filters
    (mixed-confirmation fixtures: unconfirmed shadow pairs excluded)
  * the saturation trigger fires only under the joint conditions
  * the three-arm bench gate promotes only past the exemplar-augmented bar
  * legacy fallback: empty learning store → distill JSONL (invariant 5)
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "scripts")

import distill_curate as dc  # noqa: E402
import train_lora as tl      # noqa: E402

from app.services import learning_store as ls  # noqa: E402
from app.storage import Store                  # noqa: E402


def mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp())
    return Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")


class CurationSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = mk_store()

    def _pair(self, *, verdict="edited", confirmed=True, i=0,
              task_type="extraction.task"):
        return ls.record(
            task_type=task_type,
            input_text=f"justin said send deck number {i} to sarah kane",
            local_output=f"send deck {i}",
            final_target=f"Send deck number {i} to Sarah Kane",
            verdict=verdict, verdict_source=("shadow_eval" if
                                             verdict == "shadow_disagree"
                                             else "facts.review"),
            human_confirmed=confirmed, store=self.store)

    def test_confirmed_pairs_flow_unconfirmed_excluded(self) -> None:
        self._pair(verdict="edited", i=1)
        self._pair(verdict="accepted", i=2)
        self._pair(verdict="shadow_disagree", confirmed=True, i=3)
        self._pair(verdict="shadow_disagree", confirmed=False, i=4)  # OUT
        self._pair(verdict="dismissed", i=5)                          # OUT
        rows = dc.load_from_learning_pairs(store=self.store)
        self.assertEqual(len(rows), 3)
        stats = dc.curate(rows, holdout_pct=0, dedupe_sim=1.0,
                          upweight_edited=2)
        self.assertEqual(stats["train_pairs"], 3)
        # Edits count double via upweighting (E.1).
        self.assertEqual(stats["train_examples_weighted"], 4)
        # Targets survive as gold through the adapter.
        golds = {ex["target"] for ex in stats["train"]}
        self.assertIn("Send deck number 1 to Sarah Kane", golds)
        self.assertIn("Send deck number 3 to Sarah Kane", golds)

    def test_distill_join_wins_over_synth(self) -> None:
        import json
        tmp = Path(tempfile.mkdtemp())
        distill = tmp / "escalate_distill.jsonl"
        real_row = {"id": "d1", "time": 1.0, "task": "chat",
                    "modality": "text", "user_outcome": "unknown",
                    "local": {"text": "loc"},
                    "parent": {"text": "The real full-fidelity answer"},
                    "meta": {"system": "REAL SYSTEM PROMPT",
                             "messages": [{"role": "user",
                                           "text": "real question"}]}}
        distill.write_text(json.dumps(real_row) + "\n", encoding="utf-8")
        pid = ls.record(task_type="escalation.text",
                        input_text="real question with enough length here",
                        parent_output="The real full-fidelity answer",
                        final_target="The real full-fidelity answer",
                        verdict="accepted", verdict_source="chat.outcome",
                        source_refs={"distill_id": "d1"}, store=self.store)
        self.assertIsNotNone(pid)
        rows = dc.load_from_learning_pairs(store=self.store,
                                           distill_path=distill)
        self.assertEqual(rows[0]["meta"]["system"], "REAL SYSTEM PROMPT")
        self.assertEqual(rows[0]["user_outcome"], "accepted")

    def test_fallback_to_jsonl_when_store_empty(self) -> None:
        import json
        tmp = Path(tempfile.mkdtemp())
        distill = tmp / "d.jsonl"
        distill.write_text(json.dumps({
            "id": "x", "modality": "text", "user_outcome": "accepted",
            "task": "chat", "local": {"text": "a"},
            "parent": {"text": "long enough answer"},
            "meta": {"system": "s", "messages": [{"role": "user", "text": "q"}]},
        }) + "\n", encoding="utf-8")

        class FakeSettings:
            class escalate_log:
                path = str(distill)
        rows, source = dc.load_training_rows(FakeSettings, store=self.store)
        self.assertEqual(source, "escalate_distill.jsonl")
        self.assertEqual(len(rows), 1)


class SaturationTests(unittest.TestCase):
    def _hist(self, deltas):
        return [{"by_type": {"extraction.task": {"delta": d}}} for d in deltas]

    def test_fires_only_under_joint_conditions(self) -> None:
        from app.services.idle_trainer import lora_saturation
        counts_ready = {"extraction.task": {"accepted": 250, "edited": 60}}
        counts_thin = {"extraction.task": {"accepted": 20, "edited": 5}}
        plateau = self._hist([0.051, 0.052])
        climbing = self._hist([0.02, 0.08])
        # pairs + plateau → fire
        ok, why = lora_saturation(counts_ready, plateau,
                                  min_type_pairs=300, plateau_eps=0.01)
        self.assertTrue(ok)
        self.assertIn("extraction.task", why)
        # pairs but still climbing → hold
        self.assertFalse(lora_saturation(counts_ready, climbing,
                                         min_type_pairs=300,
                                         plateau_eps=0.01)[0])
        # plateau but thin pairs → hold
        self.assertFalse(lora_saturation(counts_thin, plateau,
                                         min_type_pairs=300,
                                         plateau_eps=0.01)[0])
        # pairs + token budget binding → fire without plateau evidence
        self.assertTrue(lora_saturation(counts_ready, climbing,
                                        min_type_pairs=300, plateau_eps=0.01,
                                        truncating=True)[0])
        # single eval (no history to compare) → hold
        self.assertFalse(lora_saturation(counts_ready, self._hist([0.05]),
                                         min_type_pairs=300,
                                         plateau_eps=0.01)[0])

    def test_should_run_respects_saturation_probe(self) -> None:
        from app.services.idle_trainer import should_run
        probes = {"enabled": True, "now": 1e9, "pairs": 500, "idle_s": 99999,
                  "on_ac": True, "free_gb": 100, "min_new_pairs": 150,
                  "min_idle_s": 1200, "min_free_gb": 25, "min_days": 7,
                  "max_fails": 3}
        go, _ = should_run({}, {**probes, "lora_saturated": True})
        self.assertTrue(go)
        go, why = should_run({}, {**probes, "lora_saturated": False})
        self.assertFalse(go)
        self.assertIn("saturation", why)
        # Probes without the key (legacy callers/tests) keep old behavior.
        go, _ = should_run({}, probes)
        self.assertTrue(go)


class ThreeArmGateTests(unittest.TestCase):
    def _bench(self, pass_rate, sim, escal=0.1):
        return {"overall": {"pass_rate": pass_rate, "mean_sim": sim,
                            "would_escalate_rate": escal}, "rows": []}

    def _args(self):
        return argparse.Namespace(base="qwen2.5:7b-instruct", holdout_pct=34)

    def test_promotion_bar_is_exemplar_augmented_incumbent(self) -> None:
        """Challenger beats the plain incumbent but NOT incumbent+exemplars
        → no promotion (retrieval is still winning)."""
        benches = {("qwen2.5:7b-instruct", False): self._bench(0.7, 0.80),
                   ("qwen2.5:7b-instruct", True): self._bench(0.9, 0.92),
                   ("challenger-tag", False): self._bench(0.8, 0.85)}

        def fake_bench(model, *, pct, exemplars=False):
            return benches[(model, exemplars)]
        with patch.object(tl, "run_bench", side_effect=fake_bench):
            promote, reasons = tl.stage_gate(self._args(), "challenger-tag")
        self.assertFalse(promote)
        self.assertIn("incumbent+exemplars", reasons[0])

    def test_promotes_when_beating_the_augmented_bar(self) -> None:
        benches = {("qwen2.5:7b-instruct", False): self._bench(0.7, 0.80),
                   ("qwen2.5:7b-instruct", True): self._bench(0.8, 0.85),
                   ("challenger-tag", False): self._bench(0.95, 0.93,
                                                          escal=0.05)}

        def fake_bench(model, *, pct, exemplars=False):
            return benches[(model, exemplars)]
        with patch.object(tl, "run_bench", side_effect=fake_bench):
            promote, _ = tl.stage_gate(self._args(), "challenger-tag")
        self.assertTrue(promote)

    def test_missing_exemplar_arm_falls_back_to_plain(self) -> None:
        benches = {("qwen2.5:7b-instruct", False): self._bench(0.7, 0.80),
                   ("qwen2.5:7b-instruct", True): None,
                   ("challenger-tag", False): self._bench(0.9, 0.9)}

        def fake_bench(model, *, pct, exemplars=False):
            return benches[(model, exemplars)]
        with patch.object(tl, "run_bench", side_effect=fake_bench):
            promote, reasons = tl.stage_gate(self._args(), "challenger-tag")
        self.assertTrue(promote)
        self.assertIn("exemplar arm unavailable", reasons[0])


if __name__ == "__main__":
    unittest.main()
