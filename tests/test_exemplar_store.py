"""Workstream C — exemplar store: retrieval-first learning.

Acceptance criteria covered:
  * end-to-end: record an edited pair → exemplar appears → same-type retrieval
    returns it and the rendered local prompt contains it → deleting the pair
    removes it from retrieval
  * token budget + similarity floor respected under adversarial fixtures
  * near-tie rotation: the least-used exemplar wins inside the tie band
  * per-type gates (and the _all kill switch) silence retrieval
  * A/B harness produces per-type deltas; a negative-delta type is auto-gated
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.services import exemplar_store as xs
from app.services import learning_store as ls
from app.storage import Store


def fake_vec(text: str) -> np.ndarray:
    """Deterministic bag-of-words embedding: shared words → high cosine."""
    v = np.zeros(64, dtype=np.float32)
    for w in str(text).lower().split():
        w = w.strip("?.,!:;")
        if w:
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
    n = float(np.linalg.norm(v)) or 1.0
    return v / n


def fake_encode_many(texts):
    return [fake_vec(t) for t in texts]


class _Env:
    """Shared fixture: temp SQLite store, temp Lance dir, temp gates file,
    exemplars enabled, embeddings faked."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=self.tmp / "t.db",
                           audio_dir=self.tmp / "audio")
        self.xstore = xs.ExemplarStore(path=str(self.tmp / "lance"))
        self._patches = [
            patch.dict(os.environ, {
                "QUILL_EXEMPLARS": "1",
                "QUILL_LEARNING": "1",
                "QUILL_EXEMPLAR_GATES_PATH": str(self.tmp / "gates.json"),
                "QUILL_EXEMPLAR_USES_PATH": str(self.tmp / "uses.jsonl"),
            }, clear=False),
            patch.object(xs, "_embed", fake_encode_many),
            patch.object(xs, "exemplar_store", self.xstore),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self._patches)])

    def _mint_pair(self, *, input_text: str, target: str,
                   verdict: str = "edited",
                   task_type: str = "extraction.task") -> str:
        pid = ls.record(task_type=task_type, input_text=input_text,
                        final_target=target, verdict=verdict,
                        verdict_source="facts.review", store=self.store)
        assert pid, "fixture pair failed to record"
        return pid


class EndToEndTests(_Env, unittest.TestCase):
    def test_edit_to_retrieval_to_prompt_to_delete(self) -> None:
        pid = self._mint_pair(
            input_text="justin said send the quarterly deck to sarah kane",
            target="Send the Q3 deck to Sarah Kane")
        # Exemplar minted by the record() hook, backlinked on the pair.
        rows = self.xstore.list_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quality_tier"], "human_edited")
        self.assertEqual(self.store.get_learning_pair(pid)["embedding_id"],
                         rows[0]["exemplar_id"])
        # Same-type retrieval finds it for a similar input...
        ex = self.xstore.examples(("extraction.task",),
                                  "send the quarterly deck to sarah kane")
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["answer"], "Send the Q3 deck to Sarah Kane")
        # ...and the rendered local prompt block carries the verified answer.
        from app.services.few_shot import few_shot
        block = few_shot.render(ex)
        self.assertIn("Send the Q3 deck to Sarah Kane", block)
        self.assertIn("VERIFIED EXAMPLES", block)
        # Deleting the pair cascades: nothing retrievable afterwards.
        self.assertTrue(ls.delete(pid, store=self.store))
        self.assertEqual(self.xstore.examples(
            ("extraction.task",),
            "send the quarterly deck to sarah kane"), [])

    def test_rejected_pairs_never_ingest(self) -> None:
        ls.record(task_type="extraction.task",
                  input_text="wrong hallucinated task about the deck",
                  final_target="", verdict="dismissed",
                  verdict_source="facts.review", store=self.store)
        self.assertEqual(self.xstore.list_rows(), [])

    def test_router_examples_maps_task_and_logs_use(self) -> None:
        self._mint_pair(
            input_text="when is my meeting with sarah kane",
            target="Wednesday at 2pm with Sarah Kane",
            task_type="escalation.text")
        msgs = [{"role": "user", "content": "when is my meeting with sarah?"}]
        ex = xs.router_examples("chat", msgs)
        self.assertEqual(len(ex), 1)
        uses = Path(os.environ["QUILL_EXEMPLAR_USES_PATH"])
        self.assertTrue(uses.is_file())
        # Unsupported router task → no exemplars, no crash.
        self.assertEqual(xs.router_examples("reflect", msgs), [])

    def test_local_prompt_injection_in_model_router(self) -> None:
        """The acceptance assertion: the LOCAL system prompt carries the
        exemplar; the parent-bound system stays clean."""
        self._mint_pair(
            input_text="when is my meeting with sarah kane",
            target="Wednesday at 2pm with Sarah Kane",
            task_type="escalation.text")
        from app.services.model_router import ModelRouter
        seen = {}

        class StubLocal:
            def complete(self, task, *, system, messages, max_tokens=1024,
                         schema=None, exemplars=""):
                seen["system"] = system
                seen["exemplars"] = exemplars
                return {"text": "Your meeting is Wednesday at 2pm.",
                        "json": None, "confidence": 0.95, "parse_ok": True}

        r = ModelRouter()
        r._local = StubLocal()
        r._local_ok = True
        r._distill = lambda **kw: None            # keep the test off disk
        text, _, _ = r._local_first(
            "chat", system="You are Sparrow.",
            messages=[{"role": "user",
                       "content": "when is my meeting with sarah?"}],
            max_tokens=256, schema=None, model=None)
        self.assertEqual(text, "Your meeting is Wednesday at 2pm.")
        # Phase 1.2 moved the recalled exemplars out of `system` into their own
        # argument so the static prefix can lead. The guarantee is unchanged —
        # the local model sees the exemplar, the task prompt stays clean — but
        # it now reads off the block rather than off `system`.
        self.assertEqual(seen["system"], "You are Sparrow.")
        self.assertIn("Wednesday at 2pm with Sarah Kane", seen["exemplars"])
        self.assertIn("VERIFIED EXAMPLES", seen["exemplars"])


class BudgetAndRankingTests(_Env, unittest.TestCase):
    def test_similarity_floor(self) -> None:
        self._mint_pair(
            input_text="send the quarterly deck to sarah kane",
            target="Send the Q3 deck to Sarah Kane")
        self.assertEqual(
            self.xstore.examples(("extraction.task",),
                                 "water the office plants on thursday"), [])

    def test_token_budget_caps_selection(self) -> None:
        long_txt = "review the annual budget spreadsheet line "
        for i in range(6):
            self._mint_pair(
                input_text=long_txt + f"variant{i} " + ("filler " * 300),
                target="Review the annual budget spreadsheet " + ("x " * 300))
        ex = self.xstore.examples(("extraction.task",),
                                  long_txt + "for finance",
                                  k=6, min_sim=0.1, token_budget=800)
        # 800 tokens ≈ 3200 chars; each exemplar costs ~2700+ — so at most 1.
        self.assertLessEqual(len(ex), 1)
        spent = sum(min(len(e["prompt"]), 1500) + min(len(e["answer"]), 1500)
                    for e in ex)
        self.assertLessEqual(spent, 3200)

    def test_near_tie_rotation_prefers_least_used(self) -> None:
        vec = fake_vec("send the deck to sarah kane")
        with self.xstore._lock:
            table = self.xstore._ensure(len(vec))
            for eid, uses in (("aa", 5), ("bb", 0)):
                table.add([{
                    "exemplar_id": eid, "learning_pair_id": "p" + eid,
                    "task_type": "extraction.task",
                    "input_text": "send the deck to sarah kane",
                    "target_text": "Send the deck to Sarah Kane",
                    "quality_tier": "human_accepted", "created_at": 1.0,
                    "use_count": uses, "last_used_at": 0.0,
                    "vector": [float(x) for x in vec],
                }])
        ex = self.xstore.examples(("extraction.task",),
                                  "send the deck to sarah kane",
                                  k=1, min_sim=0.2)
        self.assertEqual(ex[0]["id"], "bb")   # identical sim → least-used wins

    def test_gates_silence_retrieval(self) -> None:
        self._mint_pair(
            input_text="send the quarterly deck to sarah kane",
            target="Send the Q3 deck to Sarah Kane")
        q = "send the quarterly deck to sarah"
        self.assertTrue(self.xstore.examples(("extraction.task",), q))
        self.xstore.set_gate("extraction.task", True, reason="test")
        self.assertEqual(self.xstore.examples(("extraction.task",), q), [])
        self.xstore.set_gate("extraction.task", False)
        self.xstore.set_gate("_all", True, reason="kill switch")
        self.assertEqual(self.xstore.examples(("extraction.task",), q), [])


class _FakeEmbedder:
    def encode_many(self, texts):
        return fake_encode_many(texts)


class ABHarnessTests(_Env, unittest.TestCase):
    def test_deltas_and_auto_gate(self) -> None:
        import sys
        sys.path.insert(0, "scripts")
        import eval_exemplars as ab

        # Two types: exemplars HELP goodtype, HURT badtype.
        pairs = []
        for i in range(20):
            pairs.append({"id": f"{i:08x}", "task_type": "extraction.task",
                          "input_text": f"goodtype request number {i}",
                          "final_target": "the verified correct answer"})
            pairs.append({"id": f"{i + 64:08x}", "task_type": "brief.section",
                          "input_text": f"badtype request number {i}",
                          "final_target": "the verified correct answer"})

        class FakeLocal:
            def complete(self, task, *, system, messages, max_tokens=1024,
                         schema=None, exemplars=""):
                aided = "VERIFIED EXAMPLES" in system
                good = "goodtype" in messages[0]["content"]
                if good:
                    text = ("the verified correct answer" if aided
                            else "some unrelated rambling reply entirely")
                else:
                    text = ("some unrelated rambling reply entirely" if aided
                            else "the verified correct answer")
                return {"text": text, "confidence": 0.9, "parse_ok": True}

        # Seed one exemplar per type so the ON arm actually injects.
        self._mint_pair(input_text="goodtype request number 99",
                        target="the verified correct answer")
        self._mint_pair(input_text="badtype request number 99",
                        target="the verified correct answer",
                        task_type="brief.section")

        with patch("app.services.embeddings.embedder", _FakeEmbedder()):
            report = ab.evaluate(pairs, FakeLocal(), pct=100)
        by = report["by_type"]
        self.assertGreater(by["extraction.task"]["delta"], 0)
        self.assertLess(by["brief.section"]["delta"], 0)

        gated = ab.apply_gates(report, min_n=3)
        self.assertEqual(gated, ["brief.section"])
        self.assertTrue(self.xstore._gated("brief.section"))
        self.assertFalse(self.xstore._gated("extraction.task"))


if __name__ == "__main__":
    unittest.main()


def setUpModule() -> None:
    # Telemetry sandbox: model_log resolves its trail path once at import, so
    # without this every faked model call in this module appends a bogus row
    # (fake models, 0s latency) to the REAL data/model_calls.jsonl trail.
    global _model_log_orig_path
    import tempfile as _tempfile
    from pathlib import Path as _Path
    from app.services.model_log import model_log as _ml
    _model_log_orig_path = _ml._path
    _ml._path = (_Path(_tempfile.mkdtemp(prefix="mnemos-test-telemetry-"))
                 / "model_calls.jsonl")


def tearDownModule() -> None:
    from app.services.model_log import model_log as _ml
    _ml._path = _model_log_orig_path
