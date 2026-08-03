"""Tests for the self-quiz (learning from its own mistakes, no human labels).

Policy under test:
  * failure (answer dissimilar to the trusted fact) -> ONE distill row,
    auto-labeled `edited` with the FACT as the corrected answer (gold is the
    human-verified fact, never the model's own output), provenance marked
    (source=self_quiz, meta.auto=True).
  * success -> stats only, no row (the model must not distill on itself).
  * answer-leaking generated questions are skipped.
  * dry-run writes nothing; repeated local errors abort instead of looping.

Local model, embedder similarity, and the fact list are injected fakes.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import self_quiz as sq
from app.services.escalate_log import escalate_log


class _FakeLocal:
    """Scripted OllamaText: qgen -> `question`, answerability probe ->
    `probe`, RAG answer -> `answer`."""

    def __init__(self, question="What did I plan?", answer="no idea",
                 probe="the professor email", exc: Exception | None = None):
        self.model = "fake-local"
        self.question, self.answer, self.probe = question, answer, probe
        self.exc = exc

    def complete(self, task, *, system, messages, max_tokens=1024, schema=None):
        if self.exc is not None:
            raise self.exc
        if system == sq._QGEN_SYSTEM:
            return {"text": self.question, "json": None, "confidence": 0.8,
                    "parse_ok": True}
        if system == sq._ANSWERABLE_SYSTEM:
            return {"text": self.probe, "json": None, "confidence": 0.8,
                    "parse_ok": True}
        return {"text": self.answer, "json": None, "confidence": 0.4,
                "parse_ok": True}


def _fact(text="email the professor about the dataset", fid=7):
    return {"id": fid, "text": text, "kind": "task", "extracted_at": 1.0}


class SelfQuizTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_self_quiz_"))
        self.trail = self.tmp / "escalate_distill.jsonl"
        self._orig = (escalate_log._path, escalate_log._counts, escalate_log._total)
        from collections import Counter
        escalate_log._path = self.trail
        escalate_log._counts = Counter()
        escalate_log._total = 0
        # _rag_call grounds through compose (same as live chat) — stub it so
        # tests never touch the real store/graph/timeline.
        self._ground = mock.patch("app.services.grounding.compose",
                                  return_value={"block": "", "hits": []})
        self._ground.start()

    def tearDown(self) -> None:
        self._ground.stop()
        escalate_log._path, escalate_log._counts, escalate_log._total = self._orig

    def _rows(self):
        if not self.trail.is_file():
            return []
        return [json.loads(ln) for ln in
                self.trail.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def test_failure_writes_auto_labeled_row_with_fact_gold(self) -> None:
        stats = sq.run_quiz(facts=[_fact()], local=_FakeLocal(),
                            sim_fn=lambda a, b: 0.2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["rows_written"], 1)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], "self_quiz")
        self.assertEqual(row["reason"], "self_quiz_failure")
        self.assertEqual(row["user_outcome"], "edited")
        self.assertEqual(row["edited"], _fact()["text"])       # gold = the FACT
        self.assertEqual(row["parent"]["text"], _fact()["text"])
        self.assertEqual(row["local"]["text"], "no idea")      # not the gold
        self.assertTrue(row["meta"]["auto"])
        self.assertEqual(row["meta"]["quiz"]["fact_id"], 7)
        self.assertIn("messages", row["meta"])                 # replayable

    def test_success_writes_no_row(self) -> None:
        stats = sq.run_quiz(facts=[_fact()], local=_FakeLocal(),
                            sim_fn=lambda a, b: 0.9)
        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["rows_written"], 0)
        self.assertEqual(self._rows(), [])

    def test_leaking_question_is_skipped(self) -> None:
        leak = _FakeLocal(question=_fact()["text"])            # echoes the fact
        stats = sq.run_quiz(facts=[_fact()], local=leak,
                            sim_fn=lambda a, b: 0.0)
        self.assertEqual(stats["skipped_qgen"], 1)
        self.assertEqual(stats["asked"], 0)
        self.assertEqual(self._rows(), [])

    def test_dry_run_scores_but_writes_nothing(self) -> None:
        stats = sq.run_quiz(facts=[_fact()], local=_FakeLocal(),
                            sim_fn=lambda a, b: 0.2, dry_run=True)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["rows_written"], 0)
        self.assertEqual(self._rows(), [])

    def test_unanswerable_question_is_skipped(self) -> None:
        # qgen invented a question the fact can't answer (live bug: "what
        # vehicle?" from a name-only fact) — the probe must veto it.
        bad = _FakeLocal(probe="UNANSWERABLE")
        stats = sq.run_quiz(facts=[_fact()], local=bad, sim_fn=lambda a, b: 0.0)
        self.assertEqual(stats["skipped_qgen"], 1)
        self.assertEqual(stats["asked"], 0)
        self.assertEqual(self._rows(), [])

    def test_already_quizzed_fact_is_skipped(self) -> None:
        # First run writes the lesson row; second run must not duplicate it.
        sq.run_quiz(facts=[_fact()], local=_FakeLocal(),
                    sim_fn=lambda a, b: 0.2)
        stats = sq.run_quiz(facts=[_fact()], local=_FakeLocal(),
                            sim_fn=lambda a, b: 0.2)
        self.assertEqual(stats["skipped_quizzed"], 1)
        self.assertEqual(stats["asked"], 0)
        self.assertEqual(len(self._rows()), 1)          # still just one row

    def test_repeated_local_errors_abort(self) -> None:
        bad = _FakeLocal(exc=RuntimeError("gpu gone"))
        facts = [_fact(fid=i) for i in range(5)]
        stats = sq.run_quiz(facts=facts, local=bad, sim_fn=lambda a, b: 0.0)
        self.assertFalse(stats["ok"])
        self.assertEqual(stats["reason"], "too_many_errors")
        self.assertEqual(stats["errors"], 3)
        self.assertEqual(self._rows(), [])


if __name__ == "__main__":
    unittest.main()
