"""Tests for the Phase 3 LoRA pipeline's pure logic (scripts/train_lora.py +
scripts/lora_train_wsl.py helpers).

Covered: Windows->WSL path mapping, HF base mapping, dated tag naming,
Modelfile assembly, the promotion gate (all four conditions + the
strict-improvement requirement), chat-family response markers, and training
conversation assembly. No WSL, no GPU, no subprocesses.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lora_train_wsl as ltw  # noqa: E402
import train_lora as tl  # noqa: E402


def _overall(pass_rate=0.6, mean_sim=0.6, escal=0.3):
    return {"pass_rate": pass_rate, "mean_sim": mean_sim,
            "would_escalate_rate": escal}


def _res(pass_rate=0.6, mean_sim=0.6, escal=0.3, rows=()):
    return {"overall": _overall(pass_rate, mean_sim, escal),
            "rows": list(rows)}


class HelperTests(unittest.TestCase):
    def test_wsl_path_maps_drive(self) -> None:
        self.assertEqual(tl.wsl_path("C:\\Users\\x\\repo\\data\\t.jsonl"),
                         "/mnt/c/Users/x/repo/data/t.jsonl")

    def test_hf_base_mapping(self) -> None:
        self.assertEqual(tl.hf_base_for("qwen2.5:7b-instruct"),
                         "unsloth/Qwen2.5-7B-Instruct")
        self.assertEqual(tl.hf_base_for("LLAMA3.2"),
                         "unsloth/Llama-3.2-3B-Instruct")
        self.assertIsNone(tl.hf_base_for("some-unknown:latest"))

    def test_default_tag_is_dated(self) -> None:
        self.assertEqual(tl.default_tag("qwen2.5:7b-instruct", date="20260717"),
                         "qwen2.5-mnemos-20260717")

    def test_modelfile_copies_template_and_params(self) -> None:
        mf = tl.build_modelfile("m.gguf", "{{ .Prompt }}",
                                'stop    "<|im_end|>"\nnum_ctx  4096')
        self.assertIn("FROM ./m.gguf", mf)
        self.assertIn('TEMPLATE """{{ .Prompt }}\n"""', mf)
        self.assertIn('PARAMETER stop "<|im_end|>"', mf)
        self.assertIn("PARAMETER num_ctx 4096", mf)


class MergedCleanupTests(unittest.TestCase):
    def test_merged_artifacts_spare_adapter_and_gguf(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            (run / "adapter").mkdir()
            for n in ("model-00001-of-00002.safetensors",
                      "model-00002-of-00002.safetensors",
                      "pytorch_model-00001-of-00002.bin",
                      "qwen2.5-7b-instruct.Q4_K_M.gguf",
                      "Modelfile", "tokenizer.json"):
                (run / n).write_bytes(b"x")
            (run / "adapter" / "adapter_model.safetensors").write_bytes(b"x")
            doomed = {p.name for p in tl.merged_artifacts(run)}
            self.assertEqual(doomed, {"model-00001-of-00002.safetensors",
                                      "model-00002-of-00002.safetensors",
                                      "pytorch_model-00001-of-00002.bin"})
            freed = tl.cleanup_merged(run)
            self.assertEqual(freed, 3)
            self.assertTrue((run / "adapter" / "adapter_model.safetensors").exists())
            self.assertTrue((run / "qwen2.5-7b-instruct.Q4_K_M.gguf").exists())
            self.assertTrue((run / "Modelfile").exists())
            self.assertEqual(tl.merged_artifacts(run), [])


class RetentionTests(unittest.TestCase):
    TAGS = ["qwen2.5-mnemos-20260718:latest", "qwen2.5-mnemos-20260725:latest",
            "qwen2.5-mnemos-20260801:latest", "qwen2.5:7b-instruct",
            "llava:7b", "minicpm-v:latest"]

    def test_parse_ollama_list_takes_first_column(self) -> None:
        text = ("NAME                ID       SIZE   MODIFIED\n"
                "qwen2.5-mnemos-20260718:latest  be4b  4.7 GB  9 days ago\n"
                "qwen2.5:7b-instruct             845d  4.7 GB  10 days ago\n")
        self.assertEqual(tl.parse_ollama_list(text),
                         ["qwen2.5-mnemos-20260718:latest",
                          "qwen2.5:7b-instruct"])

    def test_keeps_newest_two_prunes_rest(self) -> None:
        doomed = tl.tags_to_prune(self.TAGS, live="qwen2.5:7b-instruct", keep=2)
        self.assertEqual(doomed, ["qwen2.5-mnemos-20260718:latest"])

    def test_live_mnemos_tag_is_never_pruned(self) -> None:
        doomed = tl.tags_to_prune(self.TAGS, live="qwen2.5-mnemos-20260718",
                                  keep=1)
        # newest (0801) kept + live (0718) kept -> only 0725 goes
        self.assertEqual(doomed, ["qwen2.5-mnemos-20260725:latest"])

    def test_non_mnemos_tags_untouchable(self) -> None:
        doomed = tl.tags_to_prune(self.TAGS, live="x", keep=0)
        self.assertNotIn("qwen2.5:7b-instruct", doomed)
        self.assertNotIn("llava:7b", doomed)
        self.assertNotIn("minicpm-v:latest", doomed)

    def test_prune_run_dirs_mirrors_tag_retention(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td)
            for name in ("qwen2.5-mnemos-20260718", "qwen2.5-mnemos-20260801",
                         "not-a-run"):
                (runs / name).mkdir()
                (runs / name / "x").write_bytes(b"x")
            removed = tl.prune_run_dirs(
                runs, {"qwen2.5-mnemos-20260801:latest"})
            self.assertEqual(removed, ["qwen2.5-mnemos-20260718"])
            self.assertTrue((runs / "qwen2.5-mnemos-20260801").exists())
            self.assertTrue((runs / "not-a-run").exists())


class DataWatchSummaryTests(unittest.TestCase):
    def test_summary_none_when_healthy(self) -> None:
        from app.services import data_watch
        self.assertIsNone(data_watch.summarize([]))

    def test_summary_caps_and_counts_overflow(self) -> None:
        from app.services import data_watch
        msg = data_watch.summarize([f"warning {i}" for i in range(6)], cap=4)
        self.assertIn("warning 0", msg)
        self.assertIn("warning 3", msg)
        self.assertNotIn("warning 4", msg)
        self.assertIn("(+2 more)", msg)
        self.assertIn("data_audit.py", msg)


class GateTests(unittest.TestCase):
    def test_strict_improvement_promotes(self) -> None:
        promote, _ = tl.gate_decision(_res(), _res(pass_rate=0.7))
        self.assertTrue(promote)

    def test_tie_everywhere_is_not_promotion(self) -> None:
        promote, reasons = tl.gate_decision(_res(), _res())
        self.assertFalse(promote)
        self.assertTrue(any("no strict improvement" in r for r in reasons))

    def test_any_regression_blocks(self) -> None:
        # Better pass rate but worse escalation -> keep incumbent.
        promote, _ = tl.gate_decision(
            _res(), _res(pass_rate=0.9, escal=0.5))
        self.assertFalse(promote)

    def test_new_confidently_wrong_rows_block(self) -> None:
        # Challenger improves every aggregate but grows the conf-high/sim-low
        # quadrant (stays local at sim 0.1) — the doc's condition #4.
        bad_row = {"would_escalate": False, "sim": 0.1}
        promote, reasons = tl.gate_decision(
            _res(rows=[]),
            _res(pass_rate=0.9, mean_sim=0.9, escal=0.1, rows=[bad_row]))
        self.assertFalse(promote)
        self.assertTrue(any("confidently_wrong" in r and "FAIL" in r
                            for r in reasons))

    def test_confidently_wrong_counts_only_local_kept(self) -> None:
        rows = [{"would_escalate": True, "sim": 0.1},    # escalates: not counted
                {"would_escalate": False, "sim": 0.1},   # counted
                {"would_escalate": False, "sim": 0.9}]   # good: not counted
        self.assertEqual(tl.confidently_wrong(rows), 1)


class WslTrainerHelperTests(unittest.TestCase):
    def test_response_markers_by_family(self) -> None:
        self.assertEqual(ltw.response_markers_for("unsloth/Qwen2.5-7B-Instruct"),
                         ("<|im_start|>user\n", "<|im_start|>assistant\n"))
        self.assertEqual(
            ltw.response_markers_for("unsloth/Llama-3.2-3B-Instruct")[1],
            "<|start_header_id|>assistant<|end_header_id|>\n\n")
        self.assertIsNone(ltw.response_markers_for("some/other-model"))

    def test_to_conversation_shapes_chat(self) -> None:
        conv = ltw.to_conversation({
            "system": "be brief",
            "messages": [{"role": "user", "content": "Question: capital of France?"}],
            "target": "Paris.",
        })
        self.assertEqual([m["role"] for m in conv],
                         ["system", "user", "assistant"])
        self.assertEqual(conv[-1]["content"], "Paris.")

    def test_to_conversation_rejects_unteachable(self) -> None:
        self.assertIsNone(ltw.to_conversation(
            {"system": "s", "messages": [], "target": "x"}))
        self.assertIsNone(ltw.to_conversation(
            {"system": "s", "messages": [{"role": "user", "content": "q"}],
             "target": ""}))


if __name__ == "__main__":
    unittest.main()
