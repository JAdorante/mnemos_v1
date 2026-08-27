"""Tests for the bulk review queue (scripts/distill_label.py `review`).

Covers the ordering and filtering decisions that make a 90-row backlog
workable — not the interactive loop, which is I/O. The queue's job is to put
the rows that carry usable training signal in front of the human and keep the
ones that don't out of the way.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import distill_label as dl  # noqa: E402


def _row(rid="a1", reason="low_confidence", outcome="unknown", task="chat",
         t=100.0, parent="parent answer", local="local answer"):
    return {"id": rid, "task": task, "reason": reason, "modality": "text",
            "user_outcome": outcome, "time": t,
            "parent": {"text": parent}, "local": {"text": local},
            "meta": {"messages": [{"role": "user", "text": "the question"}]}}


class QueueTests(unittest.TestCase):
    def test_only_unlabeled(self) -> None:
        rows = [_row("a"), _row("b", outcome="accepted"),
                _row("c", outcome="edited"), _row("d", outcome=None)]
        self.assertEqual([r["id"] for r in dl.queue_rows(rows)], ["a", "d"])

    def test_kept_local_first(self) -> None:
        """A 👍 on a kept-local answer is the only cheap source of a
        'local was sufficient' label; escalations all train the other class,
        so they must not bury it."""
        rows = [_row("esc", reason="low_confidence", t=1.0),
                _row("kept", reason="local_kept", t=9.0),
                _row("pf", reason="parent_failed", t=5.0)]
        self.assertEqual([r["id"] for r in dl.queue_rows(rows)],
                         ["pf", "kept", "esc"])

    def test_speculative_excluded_by_default(self) -> None:
        """Answers to questions nobody asked — no one can judge them, and they
        are the bulk of the backlog."""
        rows = [_row("real"), _row("spec", reason="speculative_local_only")]
        self.assertEqual([r["id"] for r in dl.queue_rows(rows)], ["real"])
        self.assertEqual(len(dl.queue_rows(rows, speculative=True)), 2)

    def test_speculative_reachable_when_asked_for_by_reason(self) -> None:
        rows = [_row("real"), _row("spec", reason="speculative_local_only")]
        got = dl.queue_rows(rows, reason="speculative_local_only")
        self.assertEqual([r["id"] for r in got], ["spec"])

    def test_filters_and_limit(self) -> None:
        rows = [_row("a", task="chat", t=1.0), _row("b", task="extract", t=2.0),
                _row("c", task="chat", t=3.0)]
        self.assertEqual([r["id"] for r in dl.queue_rows(rows, task="chat")],
                         ["a", "c"])
        self.assertEqual(len(dl.queue_rows(rows, limit=2)), 2)

    def test_stable_oldest_first_so_sessions_resume(self) -> None:
        rows = [_row("late", t=99.0), _row("early", t=1.0)]
        self.assertEqual([r["id"] for r in dl.queue_rows(rows)],
                         ["early", "late"])


class FieldTests(unittest.TestCase):
    def test_question_is_the_last_user_turn(self) -> None:
        row = _row()
        row["meta"]["messages"] = [{"role": "user", "text": "first"},
                                   {"role": "assistant", "text": "reply"},
                                   {"role": "user", "text": "the real one"}]
        self.assertEqual(dl._question(row), "the real one")

    def test_question_falls_back_to_prompt_head(self) -> None:
        row = _row()
        row["meta"] = {"prompt_head": "truncated head"}
        self.assertEqual(dl._question(row), "truncated head")

    def test_answer_prefers_parent_then_local(self) -> None:
        self.assertEqual(dl._answer_shown(_row()), "parent answer")
        self.assertEqual(dl._answer_shown(_row(parent="")), "local answer")


if __name__ == "__main__":
    unittest.main()
