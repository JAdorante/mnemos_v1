"""Hosted (QUILL_HEADLESS=1) must not advertise local filesystem enrichment.

Seats only mount /srv/sparrow/data — scanning "the system" or "my documents"
against an empty container home looks like broken Setup buttons. Instead the
wizard offers a document upload that reuses the chat attach pipeline.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import attachments
from app.storage import Store


class HeadlessOnboardingLocalFsTests(unittest.TestCase):
    def test_scan_available_is_false_when_headless(self) -> None:
        from app.api import routes
        with patch.dict(os.environ, {"QUILL_HEADLESS": "1"}):
            out = routes.onboarding_scan_available()
        self.assertFalse(out["available"])
        self.assertIn("hosted", out.get("reason", "").lower())

    def test_documents_available_offers_upload_when_headless(self) -> None:
        from app.api import routes
        docs = SimpleNamespace(enabled=True, exts=frozenset({".txt", ".pdf"}))
        with patch.dict(os.environ, {"QUILL_HEADLESS": "1"}), \
             patch("app.api.routes.settings",
                   SimpleNamespace(documents=docs)):
            out = routes.onboarding_documents_available()
        self.assertFalse(out["available"])
        self.assertTrue(out["upload"])
        self.assertTrue(out["accept"])
        self.assertEqual(out.get("reason"), "")

    def test_documents_available_offers_upload_when_local(self) -> None:
        """Local run_all matches hosted: same Upload documents control."""
        from app.api import routes
        docs = SimpleNamespace(enabled=True, exts=frozenset({".txt", ".pdf"}))
        with patch.dict(os.environ, {"QUILL_HEADLESS": "0"}), \
             patch("app.api.routes.settings",
                   SimpleNamespace(documents=docs)), \
             patch("app.services.documents.roots",
                   return_value=[Path("/tmp/Documents")]):
            out = routes.onboarding_documents_available()
        self.assertFalse(out["available"])
        self.assertTrue(out["upload"])
        self.assertTrue(out["accept"])

    def test_documents_available_no_upload_when_docs_disabled(self) -> None:
        from app.api import routes
        docs = SimpleNamespace(enabled=False, exts=frozenset())
        with patch.dict(os.environ, {"QUILL_HEADLESS": "1"}), \
             patch("app.api.routes.settings",
                   SimpleNamespace(documents=docs)):
            out = routes.onboarding_documents_available()
        self.assertFalse(out["available"])
        self.assertFalse(out["upload"])

    def test_enrich_refuses_headless(self) -> None:
        from app.api import routes
        with patch.dict(os.environ, {"QUILL_HEADLESS": "1"}):
            out = routes.onboarding_enrich()
        self.assertFalse(out["ok"])
        self.assertIn("hosted", out["error"].lower())

    def test_documents_ingest_refuses_headless(self) -> None:
        from app.api import routes
        with patch.dict(os.environ, {"QUILL_HEADLESS": "1"}):
            out = routes.onboarding_documents()
        self.assertFalse(out["ok"])
        self.assertIn("hosted", out["error"].lower())

    def test_scan_available_still_honors_flag_when_not_headless(self) -> None:
        from app.api import routes
        with patch.dict(os.environ, {"QUILL_HEADLESS": "0",
                                     "QUILL_ONBOARDING_SCAN": "1"},
                        clear=False):
            out = routes.onboarding_scan_available()
        self.assertTrue(out["available"])


class OnboardingUploadIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ob_upload_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def test_upload_uses_onboarding_source_and_rejects_images(self) -> None:
        with patch.object(attachments, "upload_dir", return_value=self.tmp), \
             patch("app.storage.get_store", return_value=self.store), \
             patch("app.services.attachments._index_event"), \
             patch("app.services.documents.extract_text",
                   return_value="Resume for Jordan Lee at Foundry."), \
             patch.object(attachments, "_schedule_fact_mine"):
            doc = attachments.ingest_bytes(
                "resume.txt", b"placeholder",
                source="onboarding.upload", section="onboarding.upload",
                allow_images=False)
            img = attachments.ingest_bytes(
                "face.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40,
                source="onboarding.upload", section="onboarding.upload",
                allow_images=False)
        self.assertTrue(doc["ok"], doc)
        ev = self.store.get_event(doc["event_id"])
        self.assertIsNotNone(ev)
        self.assertEqual(ev["source"], "onboarding.upload")
        self.assertFalse(img["ok"])
        self.assertIn("unsupported", img["error"])


if __name__ == "__main__":
    unittest.main()
