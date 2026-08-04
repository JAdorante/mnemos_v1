"""Plan 6.2 — model_log.privacy_max + egress inventory."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services.model_log import ModelLog


class PrivacyMaxLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = ModelLog()
        self.log._path = (
            Path(tempfile.mkdtemp(prefix="quill_ml_")) / "calls.jsonl"
        )

    def test_privacy_max_on_row(self):
        row = self.log.log_call(
            task="chat", provider="claude", model="claude-opus-4-8",
            latency_s=0.2, privacy_max="sensitive",
            meta={"privacy_action": "redact"},
        )
        self.assertEqual(row["privacy_max"], "sensitive")
        self.assertEqual(row["privacy_action"], "redact")
        on_disk = json.loads(self.log._path.read_text(encoding="utf-8").strip())
        self.assertEqual(on_disk["privacy_max"], "sensitive")

    def test_meta_privacy_class_promoted(self):
        row = self.log.log_call(
            task="chat", provider="claude", model="m",
            latency_s=0.1,
            meta={"privacy_class": "personal", "privacy_action": "redact"},
        )
        self.assertEqual(row["privacy_max"], "personal")

    def test_local_not_counted_as_egress(self):
        self.log.log_call(
            task="chat", provider="ollama", model="qwen",
            latency_s=0.1, privacy_max="sensitive",
        )
        stats = self.log.stats()
        self.assertEqual(stats["privacy"]["cloud_calls"], 0)

    def test_stats_tracks_max_seen(self):
        self.log.log_call(
            task="chat", provider="claude", model="m",
            latency_s=0.1, privacy_max="internal",
        )
        self.log.log_call(
            task="extract", provider="claude", model="m",
            latency_s=0.1, privacy_max="sensitive",
            meta={"privacy_action": "redact"},
        )
        priv = self.log.stats()["privacy"]
        self.assertEqual(priv["max_seen"], "sensitive")
        self.assertEqual(priv["by_class"]["sensitive"], 1)
        self.assertEqual(priv["cloud_calls"], 2)

    def test_refuse_counted(self):
        self.log.log_call(
            task="chat", provider="claude", model="m",
            latency_s=0.0, ok=False, privacy_max="never-send",
            meta={"privacy_action": "refuse"},
        )
        self.assertEqual(self.log.stats()["privacy"]["refused"], 1)

    def test_egress_inventory(self):
        self.log.log_call(
            task="chat", provider="claude", model="m",
            latency_s=0.1, privacy_max="internal",
        )
        self.log.log_call(
            task="chat", provider="claude", model="m",
            latency_s=0.0, ok=False, privacy_max="never-send",
            meta={"privacy_action": "refuse"},
        )
        inv = self.log.egress_inventory(recent=10)
        self.assertTrue(inv["ok"])
        self.assertEqual(inv["max_seen"], "never-send")
        self.assertGreaterEqual(inv["refused"], 1)
        self.assertTrue(inv["recent"])
        self.assertEqual(inv["recent"][0]["privacy_max"], "never-send")


if __name__ == "__main__":
    unittest.main()
