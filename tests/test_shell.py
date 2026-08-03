"""Track E shell state — world / attention / existing offer peek."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class ShellStateTests(unittest.TestCase):
    def test_world_and_attention_without_agent(self):
        from app.services import shell_state
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                due = time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(now - 2 * 86400))
                store.add_commitment(
                    "Send the deck",
                    confidence=0.9, extracted_at=now - 10 * 86400, due=due)
                out = shell_state.build(store, agent_worker=None)
                self.assertIn("world", out)
                self.assertIn("attention", out)
                self.assertIn("forgotten", out)
                self.assertIsInstance(out["forgotten"], list)
                self.assertIsNone(out.get("proposal"))
                self.assertTrue(isinstance(out["attention"]["at_risk"], list))
                self.assertGreaterEqual(len(out["attention"]["at_risk"]), 1)
            finally:
                store.close()

    def test_proposal_comes_from_worker_peek(self):
        from app.services import shell_state
        from app.storage import Store

        class FakeWorker:
            def expire_stale_offers(self):
                pass

            def pending_offer(self):
                return {
                    "kind": "reasoner_commitment",
                    "title": "Follow through",
                    "message": "Yes?",
                    "items": ["Follow through on X"],
                    "reasoner": "commitment",
                    "why": ["overdue"],
                }

            def offer_queue_len(self):
                return 2

            def snapshot(self, since):
                return [], {"awaiting": False, "todo_pending": True,
                            "waiting_on": "Waiting: yes/no"}

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                out = shell_state.build(store, agent_worker=FakeWorker())
                self.assertEqual(out["proposal"]["reasoner"], "commitment")
                self.assertEqual(out["queued_offers"], 2)
                self.assertTrue(out["awaiting_approval"])
            finally:
                store.close()

    def test_no_parallel_offer_channel_in_module(self):
        """Shell must not invent offers — only peek / forward."""
        import inspect
        from app.services import shell_state
        src = inspect.getsource(shell_state)
        self.assertNotIn("propose_reasoner", src)
        self.assertNotIn("propose_task", src)


if __name__ == "__main__":
    unittest.main()
