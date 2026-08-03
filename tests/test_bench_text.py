"""Tests for the Phase 2 text benchmark harness (scripts/bench_text.py).

Covers the pure decision logic — row eligibility, gold-answer preference,
deterministic holdout split, aggregation — and the contamination guard: a
replayed row must be excluded from its own few-shot retrieval pool.
No Ollama, no embedder model.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bench_text as bt  # noqa: E402


def _row(task="chat", outcome="accepted", edited=None, rid="abc123de",
         system="s", messages=True, parent="parent answer"):
    row = {"id": rid, "task": task, "modality": "text", "user_outcome": outcome,
           "parent": {"text": parent}, "meta": {"system": system}}
    if messages:
        row["meta"]["messages"] = [{"role": "user", "text": "q"}]
    if edited:
        row["edited"] = edited
    return row


class DatasetTests(unittest.TestCase):
    def test_eligible_filters(self) -> None:
        self.assertTrue(bt.eligible(_row()))
        self.assertFalse(bt.eligible(_row(outcome="rejected")))
        self.assertFalse(bt.eligible(_row(outcome="unknown")))
        self.assertFalse(bt.eligible({**_row(), "modality": "vision"}))
        self.assertFalse(bt.eligible(_row(messages=False)))       # no full prompt
        self.assertFalse(bt.eligible(_row(parent="", edited=None)))  # no gold

    def test_gold_answer_prefers_edited(self) -> None:
        self.assertEqual(bt.gold_answer(_row(edited="human fix")), "human fix")
        self.assertEqual(bt.gold_answer(_row()), "parent answer")

    def test_gold_answer_local_fallback_for_accepted_kept_rows(self) -> None:
        # local_kept rows have no parent; a 👍 makes the local text the gold —
        # and only a 👍 (unlabeled/rejected kept rows have no gold at all).
        row = _row()
        row["parent"] = None
        row["local"] = {"text": "local gold"}
        self.assertEqual(bt.gold_answer(row), "local gold")
        self.assertTrue(bt.eligible(row))
        self.assertEqual(bt.gold_answer({**row, "user_outcome": "unknown"}), "")
        self.assertFalse(bt.eligible({**row, "user_outcome": "unknown"}))

    def test_holdout_split_deterministic_and_bounded(self) -> None:
        ids = [f"{i:08x}" for i in range(200)]
        first = [bt.in_holdout(i, 34) for i in ids]
        self.assertEqual(first, [bt.in_holdout(i, 34) for i in ids])  # stable
        self.assertFalse(any(bt.in_holdout(i, 0) for i in ids))
        self.assertTrue(all(bt.in_holdout(i, 100) for i in ids))
        self.assertFalse(bt.in_holdout("not-hex!", 100))          # junk id safe

    def test_replay_messages_maps_roles(self) -> None:
        row = _row()
        row["meta"]["messages"] = [{"role": "user", "text": "hello"},
                                   {"text": "implicit-user"}]
        msgs = bt.replay_messages(row)
        self.assertEqual(msgs[0], {"role": "user", "content": "hello"})
        self.assertEqual(msgs[1]["role"], "user")


class AggregateTests(unittest.TestCase):
    def test_rollups_per_task_and_overall(self) -> None:
        scored = [
            {"task": "chat", "sim": 0.8, "pass": True, "would_escalate": False,
             "latency_s": 1.0},
            {"task": "chat", "sim": 0.4, "pass": False, "would_escalate": True,
             "latency_s": 3.0},
            {"task": "extract", "sim": 1.0, "pass": True, "would_escalate": False,
             "latency_s": 2.0},
        ]
        agg = bt.aggregate(scored)
        self.assertEqual(agg["overall"]["n"], 3)
        self.assertAlmostEqual(agg["by_task"]["chat"]["mean_sim"], 0.6)
        self.assertEqual(agg["by_task"]["chat"]["pass_rate"], 0.5)
        self.assertEqual(agg["by_task"]["extract"]["would_escalate_rate"], 0.0)


class ContaminationGuardTests(unittest.TestCase):
    def test_row_excluded_from_its_own_retrieval(self) -> None:
        row = _row(rid="deadbeef")
        local = mock.Mock()
        local.complete.return_value = {"text": "reply", "json": None,
                                       "confidence": 0.9, "parse_ok": True}
        fake_vecs = np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
        with mock.patch("app.services.few_shot.few_shot") as fs, \
             mock.patch("app.services.embeddings.embedder") as emb:
            fs.examples.return_value = []
            emb.encode_many.return_value = fake_vecs
            out = bt.run_row(row, local, fewshot=True,
                             exclude_ids=frozenset({"other"}),
                             fewshot_k=3, fewshot_min_sim=0.4,
                             escalate_min_conf=0.6)
        excl = fs.examples.call_args.kwargs["exclude_ids"]
        self.assertIn("deadbeef", excl)                 # its own id barred
        self.assertIn("other", excl)                    # holdout set kept
        self.assertEqual(out["sim"], 1.0)
        self.assertFalse(out["would_escalate"])


if __name__ == "__main__":
    unittest.main()
