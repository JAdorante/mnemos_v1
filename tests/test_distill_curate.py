"""Tests for Phase 3 LoRA curation (scripts/distill_curate.py).

Covers the funnel: eligibility, stub drop, holdout exclusion, exact/near
dedupe, edited upweight, and readiness bands. No Ollama; embedder mocked
for near-dup tests.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import distill_curate as dc  # noqa: E402


def _row(rid="abc123de", task="chat", outcome="accepted", *,
         system="sys", messages=True, parent="good answer", edited=None,
         prompt="What is X?"):
    row = {
        "id": rid,
        "task": task,
        "modality": "text",
        "user_outcome": outcome,
        "reason": "low_confidence",
        "parent": {"text": parent},
        "meta": {"system": system, "prompt_head": prompt},
    }
    if messages:
        row["meta"]["messages"] = [{"role": "user", "text": prompt}]
    if edited is not None:
        row["edited"] = edited
    return row


class FunnelTests(unittest.TestCase):
    def test_eligible_trusted_full_fidelity_kept(self) -> None:
        rows = [
            _row(rid="11111111", outcome="accepted", prompt="q accepted"),
            _row(rid="22222222", outcome="rejected", prompt="q rejected"),
            _row(rid="33333333", outcome="accepted", messages=False,
                 prompt="q no msgs"),
            _row(rid="44444444", outcome="edited", edited="human fix",
                 prompt="q edited"),
        ]
        stats = dc.curate(rows, holdout_pct=0, dedupe_sim=1.0)
        self.assertEqual(stats["trusted"], 3)
        self.assertEqual(stats["eligible"], 2)
        self.assertEqual(stats["dropped_missing_fidelity"], 1)
        self.assertEqual(stats["train_pairs"], 2)
        targets = {ex["target"] for ex in stats["train"]}
        self.assertIn("good answer", targets)
        self.assertIn("human fix", targets)

    def test_stub_answers_dropped(self) -> None:
        rows = [
            _row(rid="aaaaaaaa", parent='{"tasks": ["stub"]}'),
            _row(rid="bbbbbbbb", parent="parent (Claude) answer"),
            _row(rid="cccccccc", parent="rescued by parent"),
            _row(rid="dddddddd", parent="real teaching answer"),
        ]
        stats = dc.curate(rows, holdout_pct=0, dedupe_sim=1.0)
        self.assertEqual(stats["dropped_stub"], 3)
        self.assertEqual(stats["train_pairs"], 1)
        self.assertEqual(stats["train"][0]["target"], "real teaching answer")

    def test_holdout_excluded_from_train(self) -> None:
        # ids chosen so one is in holdout at 34% and one is not
        in_h = "00000000"   # 0 % 100 < 34
        out_h = "00000050"  # 80 % 100 >= 34  (0x50 = 80)
        self.assertTrue(dc.bt.in_holdout(in_h, 34))
        self.assertFalse(dc.bt.in_holdout(out_h, 34))
        rows = [
            _row(rid=in_h + "deadbeef", prompt="holdout q"),
            _row(rid=out_h + "cafebabe", prompt="train q"),
        ]
        stats = dc.curate(rows, holdout_pct=34, dedupe_sim=1.0)
        self.assertEqual(stats["holdout_n"], 1)
        self.assertEqual(stats["train_pairs"], 1)
        self.assertEqual(stats["train"][0]["id"], out_h + "cafebabe")
        self.assertEqual(stats["holdout"][0]["id"], in_h + "deadbeef")

    def test_exact_dedupe(self) -> None:
        rows = [
            _row(rid="aaaaaaaa", prompt="same question"),
            _row(rid="bbbbbbbb", prompt="same question"),
            _row(rid="cccccccc", prompt="different"),
        ]
        stats = dc.curate(rows, holdout_pct=0, dedupe_sim=1.0)
        self.assertEqual(stats["dropped_near_dup"], 1)
        self.assertEqual(stats["train_pairs"], 2)

    def test_near_dedupe_uses_embeddings(self) -> None:
        rows = [
            _row(rid="aaaaaaaa", prompt="alpha"),
            _row(rid="bbbbbbbb", prompt="beta"),
            _row(rid="cccccccc", prompt="gamma"),
        ]
        # vec0 ≈ vec1; vec2 orthogonal
        vecs = np.asarray([
            [1.0, 0.0],
            [0.99, 0.0],
            [0.0, 1.0],
        ], dtype=np.float32)
        # normalize for cosine via dot
        vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)

        def fake_embed(texts):
            self.assertEqual(len(texts), 3)
            return vecs

        stats = dc.curate(rows, holdout_pct=0, dedupe_sim=0.95,
                          embed_fn=fake_embed)
        self.assertEqual(stats["dropped_near_dup"], 1)
        self.assertEqual(stats["train_pairs"], 2)

    def test_upweight_edited(self) -> None:
        rows = [
            _row(rid="aaaaaaaa", outcome="accepted", prompt="accepted q"),
            _row(rid="bbbbbbbb", outcome="edited", edited="fixed",
                 prompt="edited q"),
        ]
        stats = dc.curate(rows, holdout_pct=0, dedupe_sim=1.0,
                          upweight_edited=3)
        self.assertEqual(stats["train_pairs"], 2)
        self.assertEqual(stats["train_examples_weighted"], 4)  # 1 + 3
        outcomes = [ex["outcome"] for ex in stats["weighted"]]
        self.assertEqual(outcomes.count("edited"), 3)
        self.assertEqual(outcomes.count("accepted"), 1)

    def test_to_example_uses_clean_system_and_messages(self) -> None:
        row = _row(edited="target text")
        row["user_outcome"] = "edited"
        ex = dc.to_example(row)
        self.assertEqual(ex["system"], "sys")
        self.assertEqual(ex["messages"], [{"role": "user", "content": "What is X?"}])
        self.assertEqual(ex["target"], "target text")
        self.assertNotIn("VERIFIED EXAMPLES", ex["system"])

    def test_readiness_bands(self) -> None:
        def _n(n: int) -> list[dict]:
            return [_row(rid=f"{i:08x}", prompt=f"q{i}") for i in range(n)]

        self.assertEqual(
            dc.curate(_n(7), holdout_pct=0, dedupe_sim=1.0)["readiness"],
            "accumulating")
        self.assertEqual(
            dc.curate(_n(100), holdout_pct=0, dedupe_sim=1.0)["readiness"],
            "critical_mass")
        self.assertEqual(
            dc.curate(_n(300), holdout_pct=0, dedupe_sim=1.0)["readiness"],
            "ready")

    def test_perishable_flagged_not_dropped(self) -> None:
        rows = [
            _row(rid="aaaaaaaa", parent="Your next meeting is Tuesday 9am.",
                 prompt="when is my meeting?"),
            _row(rid="bbbbbbbb", parent="Reply in one short sentence.",
                 prompt="how should you answer?"),
        ]
        stats = dc.curate(rows, holdout_pct=0, dedupe_sim=1.0)
        self.assertEqual(stats["train_pairs"], 2)
        self.assertEqual(stats["flagged_perishable"], 1)

    def test_write_jsonl(self) -> None:
        rows = [_row(rid="aaaaaaaa")]
        stats = dc.curate(rows, holdout_pct=0, dedupe_sim=1.0)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "train.jsonl"
            dc.write_jsonl(path, stats["weighted"])
            loaded = [json.loads(ln) for ln in path.read_text().splitlines()]
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["target"], "good answer")


class IsStubTests(unittest.TestCase):
    def test_known_stubs(self) -> None:
        self.assertTrue(dc.is_stub(_row(parent='{"tasks": ["stub"]}')))
        self.assertFalse(dc.is_stub(_row(parent="Madrid is the capital.")))


if __name__ == "__main__":
    unittest.main()
