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
        # Targets carry the inference contract's CONFIDENCE trailer.
        golds = {ex["target"].split("\n\nCONFIDENCE:")[0]
                 for ex in stats["train"]}
        self.assertIn("good answer", golds)
        self.assertIn("human fix", golds)

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
        self.assertEqual(stats["train"][0]["target"],
                         "real teaching answer\n\nCONFIDENCE: 0.90")

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
        # Clean stored system (no few-shot augmentation) + the inference
        # contract's confidence-trailer instruction appended.
        self.assertTrue(ex["system"].startswith("sys"))
        self.assertIn("CONFIDENCE: 0.NN", ex["system"])
        self.assertNotIn("VERIFIED EXAMPLES", ex["system"])
        self.assertEqual(ex["messages"], [{"role": "user", "content": "What is X?"}])
        self.assertEqual(ex["target"], "target text\n\nCONFIDENCE: 0.90")

    def test_to_example_matches_inference_contract(self) -> None:
        """The trained pair must round-trip through the router's own parser —
        the first live adapter regressed because targets lacked the trailer
        and every answer parsed as confidence None (auto-escalate)."""
        from app.services.ollama_text import split_confidence, training_contract
        ex = dc.to_example(_row())
        clean, conf = split_confidence(ex["target"])
        self.assertEqual(clean, "good answer")
        self.assertEqual(conf, 0.9)
        # Idempotent: re-applying the contract changes nothing.
        s2, t2 = training_contract(ex["system"], ex["target"])
        self.assertEqual((s2, t2), (ex["system"], ex["target"]))

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
            self.assertEqual(loaded[0]["target"],
                             "good answer\n\nCONFIDENCE: 0.90")


def _synth(rid="s1", prompt="What is Venture Pulse?", answer="A CRM."):
    row = _row(rid=rid, parent=answer, prompt=prompt)
    row["user_outcome"] = "synthetic"
    row["synthetic"] = True
    row["reason"] = "synthetic_distill"
    return row


class SyntheticMergeTests(unittest.TestCase):
    """Parent-distilled pairs bootstrap volume but stay quarantined: train
    only, never holdout, capped vs real, no holdout-focus leakage."""

    def test_merged_into_weighted_never_holdout(self) -> None:
        real = [_row(rid=f"{i:08x}", prompt=f"real q{i}") for i in range(3)]
        synth = [_synth(rid=f"s{i}", prompt=f"synth q{i}") for i in range(4)]
        stats = dc.curate(real, holdout_pct=0, dedupe_sim=1.0,
                          synthetic_rows=synth)
        self.assertEqual(stats["train_pairs"], 3)          # real only
        self.assertEqual(stats["synthetic_pairs"], 4)
        self.assertEqual(len(stats["weighted"]), 7)
        self.assertEqual(len(stats["holdout"]), 0)
        outcomes = {ex["outcome"] for ex in stats["weighted"]}
        self.assertEqual(outcomes, {"accepted", "synthetic"})
        # Synthetic targets carry the confidence contract too.
        syn_ex = [e for e in stats["weighted"] if e["outcome"] == "synthetic"]
        self.assertTrue(all(e["target"].endswith("CONFIDENCE: 0.90")
                            for e in syn_ex))

    def test_cap_limits_synthetic_volume(self) -> None:
        real = [_row(rid="aaaaaaaa", prompt="real q")]
        synth = [_synth(rid=f"s{i}", prompt=f"synth q{i}") for i in range(9)]
        stats = dc.curate(real, holdout_pct=0, dedupe_sim=1.0,
                          synthetic_rows=synth, synthetic_cap=3.0)
        self.assertEqual(stats["synthetic_pairs"], 3)      # 3.0 x 1 real
        self.assertEqual(stats["synthetic_cap"], 3)

    def test_holdout_focus_collision_dropped(self) -> None:
        # id "00000000" hashes into the 34% holdout band; a synthetic pair
        # asking the same question must NOT enter train (answer leakage).
        real = [_row(rid="00000000", prompt="What is the valuation?"),
                _row(rid="00000050", prompt="totally different q")]
        synth = [_synth(rid="s1", prompt="What is the valuation?"),
                 _synth(rid="s2", prompt="another synth q")]
        stats = dc.curate(real, holdout_pct=34, dedupe_sim=1.0,
                          synthetic_rows=synth)
        self.assertEqual(stats["holdout_n"], 1)
        self.assertEqual(stats["synthetic_pairs"], 1)
        self.assertEqual(stats["synthetic_dropped_holdout_collision"], 1)

    def test_load_synthetic_missing_file_is_empty(self) -> None:
        self.assertEqual(dc.load_synthetic(None), [])
        self.assertEqual(dc.load_synthetic(Path("no/such/file.jsonl")), [])


class IsStubTests(unittest.TestCase):
    def test_known_stubs(self) -> None:
        self.assertTrue(dc.is_stub(_row(parent='{"tasks": ["stub"]}')))
        self.assertFalse(dc.is_stub(_row(parent="Madrid is the capital.")))


if __name__ == "__main__":
    unittest.main()
