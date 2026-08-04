"""Plan 3.2 — deterministic answer-check (fabricated-token downgrade)."""
from __future__ import annotations

import unittest


class AnswerCheckTests(unittest.TestCase):
    def test_fabricated_price_downgrades(self):
        from app.services.answer_check import check_answer

        context = (
            "David said the subscription costs $49 per month.\n"
            "Renewal is on 2026-09-01."
        )
        answer = "David said it costs $55 per month."
        checked = check_answer(
            answer, context,
            question="What did David say the price was?",
            entail=lambda *a, **k: True,
        )
        self.assertFalse(checked.ok)
        self.assertEqual(checked.status, "downgraded")
        self.assertIn("$55", checked.fabricated)
        self.assertTrue(
            checked.text.startswith("Here's what I found, with the evidence:")
        )
        self.assertIn("$55", checked.buckets["missing"])
        self.assertIn("David", checked.buckets["confirmed"])

    def test_fabricated_name_downgrades(self):
        from app.services.answer_check import check_answer

        context = "David said the subscription costs $49 per month."
        answer = "Sarah said the subscription costs $49 per month."
        checked = check_answer(
            answer, context,
            question="Who mentioned the price?",
            entail=lambda *a, **k: True,
        )
        self.assertFalse(checked.ok)
        self.assertEqual(checked.status, "downgraded")
        self.assertIn("Sarah", checked.fabricated)
        self.assertIn("$49", checked.buckets["confirmed"])

    def test_grounded_tokens_pass(self):
        from app.services.answer_check import check_answer

        context = (
            "David said the subscription costs $49 per month.\n"
            "Due 2026-09-01."
        )
        answer = "David said it costs $49, due 2026-09-01."
        checked = check_answer(
            answer, context,
            question="What did David say?",
            entail=lambda *a, **k: True,
        )
        self.assertTrue(checked.ok)
        self.assertEqual(checked.status, "ok")
        self.assertEqual(checked.text, answer)
        self.assertFalse(checked.fabricated)
        self.assertIn("David", checked.buckets["confirmed"])
        self.assertIn("$49", checked.buckets["confirmed"])

    def test_entailment_failure_downgrades_money_answer(self):
        from app.services.answer_check import check_answer

        context = "David said the subscription costs $49 per month."
        answer = "David said it costs $49 per month."
        checked = check_answer(
            answer, context,
            question="What does it cost?",
            entail=lambda *a, **k: False,
        )
        self.assertFalse(checked.ok)
        self.assertEqual(checked.status, "downgraded")
        self.assertTrue(
            checked.text.startswith("Here's what I found, with the evidence:")
        )

    def test_empty_context_skips(self):
        from app.services.answer_check import check_answer

        checked = check_answer("Paris is the capital of France.", "")
        self.assertTrue(checked.ok)
        self.assertEqual(checked.status, "skipped")

    def test_compiler_exposes_evidence_sections(self):
        from app.services.answer_check import check_answer
        from app.services.response_compiler import compile_response

        context = "David said the subscription costs $49 per month."
        answer = "Sarah said it costs $55."
        checked = check_answer(
            answer, context, entail=lambda *a, **k: True,
        )
        doc = compile_response(
            checked.text,
            sources=[{"label": "facts", "n": 1, "items": [context]}],
            evidence=checked.buckets,
            answer_check=checked.to_dict(),
        )
        self.assertIsNotNone(doc)
        types = {s["type"] for s in doc["sections"]}
        self.assertIn("missing", types)
        self.assertEqual(doc["answer_check"]["status"], "downgraded")


if __name__ == "__main__":
    unittest.main()
