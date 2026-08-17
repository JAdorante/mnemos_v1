"""Workstream 5 — portable trust core (no capture/memory imports)."""
from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.trust import (
    RISK_TABLE,
    approval_binding_is_enforce,
    classify_risk,
    source_can_authorize,
)


class TrustCoreTests(unittest.TestCase):
    def test_memory_never_authorizes(self) -> None:
        self.assertFalse(source_can_authorize("audio.whisper", {}))
        self.assertFalse(source_can_authorize("omi:dev1", {"external_source": True}))
        self.assertFalse(source_can_authorize("exhaust.gmail", {}))

    def test_risk_table_not_llm(self) -> None:
        self.assertEqual(classify_risk("send")[0], "high")
        self.assertEqual(classify_risk("delete")[0], "blocked")
        self.assertEqual(RISK_TABLE["draft"], "low")

    def test_approval_bind_default_enforce(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUILL_APPROVAL_BIND", None)
            self.assertTrue(approval_binding_is_enforce())
        with patch.dict(os.environ, {"QUILL_APPROVAL_BIND": "shadow"}, clear=False):
            self.assertFalse(approval_binding_is_enforce())
        with patch.dict(os.environ, {"QUILL_APPROVAL_BIND": "enforce"}, clear=False):
            self.assertTrue(approval_binding_is_enforce())

    def test_trust_module_has_no_capture_imports(self) -> None:
        src = Path("app/services/trust.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        banned = {"app.services.audio", "app.services.memory", "app.services.vision",
                  "app.storage"}
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            if isinstance(node, ast.Import):
                for n in node.names:
                    found.add(n.name)
        self.assertFalse(found & banned)


if __name__ == "__main__":
    unittest.main()
