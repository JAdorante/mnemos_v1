"""Workstream 6 — external capture never authorizes."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import external_capture
from app.services.agent_planner import source_can_authorize


class ExternalCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_ext_"))
        self.env = patch.dict(os.environ, {
            "QUILL_DATA_DIR": str(self.tmp),
            "QUILL_EXTERNAL_CAPTURE": "1",
        }, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def test_unauthenticated_rejected_by_authenticate(self) -> None:
        self.assertIsNone(external_capture.authenticate(None))
        self.assertIsNone(external_capture.authenticate("Bearer not-a-token"))

    def test_transcript_flags_never_authorizes(self) -> None:
        device = {"device_id": "dev1", "name": "omi"}
        out = external_capture.ingest_transcript(device, {
            "text": "we should follow up with Justin",
            "started_at": 1.0, "ended_at": 2.0, "kind": "omi",
        })
        self.assertTrue(out.get("ok"))
        self.assertTrue(out.get("never_authorizes"))
        ev = Event(time=2.0, modality=Modality.AUDIO, raw="hi",
                   source="omi:dev1", meta={"never_authorizes": True,
                                           "external_source": True})
        self.assertTrue(external_capture.never_authorizes_event(ev))
        self.assertFalse(source_can_authorize(ev.source, ev.meta))

    def test_memory_never_authorizes_even_for_mic(self) -> None:
        self.assertFalse(source_can_authorize("audio.whisper", {}))
        self.assertFalse(source_can_authorize("exhaust.gmail", {}))


if __name__ == "__main__":
    unittest.main()
