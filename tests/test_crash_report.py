"""Workstream 4.4 — local crash zip redacts keys and personal lines."""
from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.services import crash_report


class CrashReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_cr_"))
        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": str(self.tmp)}, clear=False)
        self.env.start()
        (self.tmp / "model_calls.jsonl").write_text(
            "sk-ant-api03-SECRETKEYVALUEHERE0001 called\ntherapy appointment noted\n",
            encoding="utf-8")

    def tearDown(self) -> None:
        self.env.stop()

    def test_zip_redacts_key_and_personal(self) -> None:
        out = crash_report.write_report(note="broke on standup")
        self.assertTrue(out["ok"])
        zpath = Path(out["path"])
        self.assertTrue(zpath.is_file())
        self.assertTrue(str(zpath).startswith(str(self.tmp)))
        with zipfile.ZipFile(zpath) as zf:
            blob = zf.read("model_calls.jsonl").decode("utf-8")
        self.assertNotIn("sk-ant-api03-SECRETKEYVALUEHERE0001", blob)
        self.assertIn("[REDACTED_KEY]", blob)
        self.assertIn("[redacted personal-class line]", blob)
        self.assertNotIn("therapy", blob.lower())


if __name__ == "__main__":
    unittest.main()
