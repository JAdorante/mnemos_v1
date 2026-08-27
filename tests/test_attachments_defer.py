"""Chat attach must not block the request on LLM fact mining."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import attachments
from app.storage import Store


class AttachDeferExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="attach_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def test_document_returns_before_llm_extract(self) -> None:
        called = {"extract": 0, "scheduled": 0}

        def fake_extract(text):
            called["extract"] += 1
            return {"tasks": [], "commitments": [], "claims": [],
                    "people": [], "entities": [], "relations": []}

        def fake_schedule(fn, *args, label="attach"):
            called["scheduled"] += 1
            # Do not run — proves the HTTP path does not wait on mining.

        pdf_text = "Executive brief on Physical AI. Contact Jordan Lee at Foundry."
        with patch.object(attachments, "upload_dir", return_value=self.tmp), \
             patch("app.storage.get_store", return_value=self.store), \
             patch("app.services.attachments._index_event"), \
             patch("app.services.documents.extract_text", return_value=pdf_text), \
             patch.object(attachments, "_schedule_fact_mine", side_effect=fake_schedule), \
             patch("app.services.extractor.extractor._extract_text", side_effect=fake_extract):
            # Force document path via .txt
            out = attachments.ingest_bytes("brief.txt", b"placeholder")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out.get("facts_pending"))
        self.assertEqual(out.get("facts"), 0)
        self.assertIn("Physical AI", out.get("context", ""))
        self.assertEqual(called["scheduled"], 1)
        self.assertEqual(called["extract"], 0)  # not on the request path


class AttachContextWindowTests(unittest.TestCase):
    """The turn right after an upload is answered from the attach `context`
    alone — retrieval runs on the goal and returns unrelated memories, so this
    window has to carry enough of the file, labelled as the turn's subject."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="attach_ctx_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def _ingest(self, text: str, *, attach_chars: int, max_chars: int = 40_000):
        cfg = SimpleNamespace(
            documents=SimpleNamespace(attach_context_chars=attach_chars,
                                      max_chars=max_chars),
            storage=SimpleNamespace(data_dir=str(self.tmp)))
        with patch.object(attachments, "upload_dir", return_value=self.tmp), \
             patch("app.storage.get_store", return_value=self.store), \
             patch("app.services.attachments._index_event"), \
             patch("app.services.documents.extract_text", return_value=text), \
             patch.object(attachments, "settings", cfg), \
             patch.object(attachments, "_schedule_fact_mine"):
            return attachments.ingest_bytes("brief.txt", b"placeholder")

    def test_context_labels_the_file_as_the_turn_subject(self) -> None:
        out = self._ingest("Quarterly plan for Foundry.", attach_chars=16_000)
        ctx = out["context"]
        self.assertIn("ATTACHED DOCUMENT", ctx)
        self.assertIn("brief.txt", ctx)
        self.assertIn("Quarterly plan for Foundry.", ctx)

    def test_short_document_rides_along_whole_with_no_truncation_note(self) -> None:
        out = self._ingest("Short note.", attach_chars=16_000)
        self.assertIn("Short note.", out["context"])
        self.assertNotIn("first 16,000", out["context"])

    def test_long_document_is_cut_to_the_configured_window_and_says_so(self) -> None:
        text = "z" * 30_000
        out = self._ingest(text, attach_chars=16_000)
        ctx = out["context"]
        self.assertEqual(ctx.count("z"), 16_000)
        self.assertIn("first 16,000 of 30,000 characters", ctx)

    def test_window_is_configurable(self) -> None:
        out = self._ingest("z" * 5_000, attach_chars=1_000)
        self.assertEqual(out["context"].count("z"), 1_000)

    def test_a_file_capped_at_ingest_reports_an_open_ended_total(self) -> None:
        # extract_text already stopped at documents.max_chars, so len(text) is
        # a floor, not the file's real size — the note must not claim otherwise.
        out = self._ingest("z" * 40_000, attach_chars=16_000, max_chars=40_000)
        self.assertIn("first 16,000 of 40,000+ characters", out["context"])

    def test_a_capped_file_shorter_than_the_window_still_flags_the_cut(self) -> None:
        out = self._ingest("z" * 40_000, attach_chars=80_000, max_chars=40_000)
        ctx = out["context"]
        self.assertEqual(ctx.count("z"), 40_000)
        self.assertIn("leading section, not the whole", ctx)


if __name__ == "__main__":
    unittest.main()
