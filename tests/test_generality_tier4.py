"""Tier 4 — the feedback -> model loop. The models learn the user's preferences
from the user's OWN persisted verdicts (edit diffs, dismissed facts, approval
streaks), never from a hardcoded example. Every consumer is OPT-IN (bias/eval
guardrail): OFF by default -> empty (behavior unchanged); ON -> learned blocks.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path


class Tier4FeedbackLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.storage import Store
        from app.services.agent_log import Recorder
        import app.services.feedback_learning as FL
        self.FL = FL
        self.store = Store(db_path=Path(tempfile.mkdtemp()) / "t.db")
        FL._store = lambda: self.store
        FL.reset_cache()

        rec = Recorder(store=self.store)
        rec.start_run("draft a follow-up email", intent="follow_up_email",
                      surface="browser")
        pid = rec.record_packet(summary="Send email", goal="draft",
                                fields={"body": "Hey, following up on the thing."})
        rec.record_decision(pid, "edit",
                            user_edit="Hi Dana — circling back on the pricing doc.")
        for _ in range(3):
            p = rec.record_packet(summary="Send email", goal="draft",
                                  fields={"body": "x"})
            rec.record_decision(p, "approve")
        fid = self.store.add_task("remember to breathe", source_span="small talk",
                                  confidence=0.3, extracted_at=time.time())
        self.store.review_fact(fid, "dismissed")

    def tearDown(self) -> None:
        for k in ("QUILL_LEARN_DRAFTING", "QUILL_LEARN_EXTRACTION_NEGATIVES",
                  "QUILL_LEARN_TRUST"):
            os.environ.pop(k, None)
        self.FL.reset_cache()

    def _set(self, **flags) -> None:
        for k, v in flags.items():
            os.environ[k] = v
        self.FL.reset_cache()

    def test_disabled_by_default(self) -> None:
        for k in ("QUILL_LEARN_DRAFTING", "QUILL_LEARN_EXTRACTION_NEGATIVES",
                  "QUILL_LEARN_TRUST"):
            os.environ.pop(k, None)
        self.FL.reset_cache()
        self.assertEqual(self.FL.drafting_preference_block(), "")
        self.assertEqual(self.FL.extraction_negatives_block(), "")
        self.assertIsNone(self.FL.trust_proposal("follow_up_email"))

    def test_drafting_preferences_learned(self) -> None:
        self._set(QUILL_LEARN_DRAFTING="1")
        block = self.FL.drafting_preference_block()
        self.assertIn("following up on the thing", block)     # the before
        self.assertIn("circling back on the pricing doc", block)  # the after

    def test_extraction_negatives_learned(self) -> None:
        self._set(QUILL_LEARN_EXTRACTION_NEGATIVES="1")
        block = self.FL.extraction_negatives_block()
        self.assertIn("remember to breathe", block)

    def test_trust_proposal_streak(self) -> None:
        self._set(QUILL_LEARN_TRUST="1")
        tp = self.FL.trust_proposal("follow_up_email", min_streak=3)
        self.assertIsNotNone(tp)
        self.assertGreaterEqual(tp["streak"], 3)
        self.assertEqual(tp["proposal"], "propose")           # never auto-applies
        # an unknown intent yields nothing
        self.assertIsNone(self.FL.trust_proposal("nonexistent_intent", min_streak=3))


if __name__ == "__main__":
    unittest.main()
