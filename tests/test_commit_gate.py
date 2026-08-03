"""Commit-gate: compose placeholders are not irreversible sends."""
from __future__ import annotations

import unittest

from browser_agent.orchestrator import _looks_irreversible


class LooksIrreversibleTests(unittest.TestCase):
    def test_real_send_button_is_commit(self):
        self.assertTrue(_looks_irreversible("Send"))
        self.assertTrue(_looks_irreversible("Send message"))
        self.assertTrue(_looks_irreversible("Submit"))
        self.assertTrue(_looks_irreversible("Delete"))

    def test_snapchat_compose_placeholder_is_not_commit(self):
        # Accessible name of Snapchat Web's chat composer (live fail Jul 22).
        self.assertFalse(_looks_irreversible("Send a chat"))
        self.assertFalse(_looks_irreversible("Send a message"))
        self.assertFalse(_looks_irreversible("Type a message"))
        self.assertFalse(_looks_irreversible("Write a message…"))

    def test_textbox_role_never_commit_even_if_named_send(self):
        self.assertFalse(_looks_irreversible(
            "Send", role="textbox", editable=True))
        self.assertFalse(_looks_irreversible(
            "Send", role="textbox"))
        # Real button role still commits.
        self.assertTrue(_looks_irreversible("Send", role="button"))


if __name__ == "__main__":
    unittest.main()
