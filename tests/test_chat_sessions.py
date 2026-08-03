"""Chat conversation archive — New conversation saves prior turns to disk."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import chat_sessions as cs
from app.services.agent_bridge import AgentWorker


class ChatSessionsArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_chat_sessions_"))
        self._dir_patch = patch.object(cs, "sessions_dir", return_value=self.tmp)
        self._dir_patch.start()

    def tearDown(self) -> None:
        self._dir_patch.stop()

    def test_skips_noise_only(self) -> None:
        self.assertIsNone(cs.archive_events([
            {"id": 0, "kind": "system", "text": "Agent ready"},
            {"id": 1, "kind": "progress", "text": "thinking…"},
        ]))
        self.assertEqual(cs.list_sessions(), [])

    def test_archives_user_turn(self) -> None:
        events = [
            {"id": 0, "kind": "system", "text": "ready"},
            {"id": 1, "kind": "user", "text": "Remind me about the pricing follow-up"},
            {"id": 2, "kind": "result", "text": "You committed to email Justin.",
             "compiled": {"sections": [{"huge": True}]}},
        ]
        meta = cs.archive_events(events)
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta["n_turns"], 1)
        self.assertIn("pricing follow-up", meta["title"])

        listed = cs.list_sessions()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], meta["id"])

        body = cs.load_session(meta["id"])
        self.assertIsNotNone(body)
        assert body is not None
        kinds = [e["kind"] for e in body["events"]]
        self.assertEqual(kinds, ["system", "user", "result"])
        # Compiled docs are stripped — text is enough to replay.
        self.assertNotIn("compiled", body["events"][2])

    def test_rejects_path_traversal_ids(self) -> None:
        self.assertIsNone(cs.load_session("../secrets"))
        self.assertIsNone(cs.load_session(r"..\secrets"))


class AgentWorkerNewConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_chat_new_"))
        self._dir_patch = patch.object(cs, "sessions_dir", return_value=self.tmp)
        self._dir_patch.start()
        self.worker = AgentWorker()

    def tearDown(self) -> None:
        self._dir_patch.stop()

    def test_new_archives_and_clears_live_log(self) -> None:
        self.worker._emit("user", "What did Marc say about price?")
        self.worker._emit("result", "Forty-nine a month.")
        self.assertEqual(len(self.worker.events), 2)

        archived = self.worker.new()
        self.assertTrue(archived.get("id"))
        self.assertEqual(archived.get("n_turns"), 1)

        # Live log reset + one system line for the new conversation.
        self.assertEqual(len(self.worker.events), 1)
        self.assertEqual(self.worker.events[0]["kind"], "system")
        self.assertIn("saved", self.worker.events[0]["text"].lower())

        path = self.tmp / f"{archived['id']}.json"
        self.assertTrue(path.is_file())
        body = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(body["events"][0]["kind"], "user")

        # Transcript-clear commands were queued for both lanes.
        self.assertEqual(self.worker.cmd_q.get_nowait()["type"], "new")
        self.assertEqual(self.worker.fast_q.get_nowait()["type"], "new")

    def test_new_without_turns_still_resets(self) -> None:
        self.worker._emit("system", "Agent ready")
        archived = self.worker.new()
        self.assertEqual(archived, {})
        self.assertEqual(len(self.worker.events), 1)
        self.assertIn("New conversation", self.worker.events[0]["text"])


if __name__ == "__main__":
    unittest.main()
