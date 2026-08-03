"""Chat attach must not block the request on LLM fact mining."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
