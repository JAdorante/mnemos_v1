"""Chat UI 'Add context' — merges optional notes into the agent goal."""
from __future__ import annotations

import unittest

from app.api.routes import _attach_user_context


class AttachUserContextTests(unittest.TestCase):
    def test_no_context_passthrough(self) -> None:
        agent, display = _attach_user_context("What tasks are open?", None)
        self.assertEqual(agent, "What tasks are open?")
        self.assertEqual(display, "What tasks are open?")

    def test_context_merged_display_clean(self) -> None:
        agent, display = _attach_user_context(
            "Draft a status update",
            "Only use the F1 notes. Ignore Uber Eats tasks.",
        )
        self.assertEqual(display, "Draft a status update")
        self.assertIn("USER-PROVIDED CONTEXT", agent)
        self.assertIn("Only use the F1 notes", agent)
        self.assertIn("User request: Draft a status update", agent)

    def test_blank_context_ignored(self) -> None:
        agent, display = _attach_user_context("hi", "   ")
        self.assertEqual(agent, "hi")
        self.assertEqual(display, "hi")


if __name__ == "__main__":
    unittest.main()
