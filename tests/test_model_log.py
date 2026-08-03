"""model_log: every call row must carry a wall-clock `time` stamp.

Added after a live-debug session where the trail couldn't answer "did any
chat call happen after the restart?" — rows had latency but no timestamp.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.services.model_log import ModelLog


class ModelLogTimeTests(unittest.TestCase):
    def test_row_carries_current_timestamp(self) -> None:
        log = ModelLog()
        log._path = Path(tempfile.mkdtemp(prefix="quill_model_log_")) / "calls.jsonl"
        before = time.time()
        row = log.log_call(task="chat", provider="ollama", model="llama3.2",
                           latency_s=0.5)
        after = time.time()
        self.assertTrue(before - 1 <= row["time"] <= after + 1)
        on_disk = json.loads(log._path.read_text(encoding="utf-8").strip())
        self.assertEqual(on_disk["time"], row["time"])


if __name__ == "__main__":
    unittest.main()
