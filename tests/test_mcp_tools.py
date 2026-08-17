"""Workstream 3 — read-only MCP tools; personal deny; no write/action."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import mcp_tools


class McpToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_mcp_"))
        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": str(self.tmp)}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def test_schemas_are_read_only(self) -> None:
        names = {t["name"] for t in mcp_tools.tool_schemas()}
        self.assertEqual(names, set(mcp_tools.READ_TOOLS))
        for t in mcp_tools.tool_schemas():
            d = t["description"].lower()
            self.assertIn("cannot mint facts", d)
            self.assertIn("never authorizes", d)

    def test_write_and_action_tools_refused(self) -> None:
        for name in ("mint_fact", "create_task", "browser_run", "desktop_run",
                     "send_email", "approve"):
            out = mcp_tools.call_tool(name, {})
            self.assertFalse(out.get("ok"))
            self.assertFalse(out.get("write_tools"))
            self.assertFalse(out.get("action_tools"))

    def test_personal_class_redacted_under_default_policy(self) -> None:
        payload = [{"text": "Ada mentioned her therapy appointment", "kind": "claim"}]
        out = mcp_tools.redact_result(payload)
        self.assertEqual(out, [])

    def test_work_class_passes(self) -> None:
        payload = [{"text": "Ada committed to ship the deck this week", "kind": "commitment"}]
        out = mcp_tools.redact_result(payload)
        self.assertTrue(out)
        self.assertEqual(out[0]["disclosure_class"], "work")


if __name__ == "__main__":
    unittest.main()
