"""Promote-on-first-use: templates + overlay writes from Console approval."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop_agent import app_promotion, app_templates


class TemplateTests(unittest.TestCase):
    def test_infer_from_extension(self) -> None:
        self.assertEqual(app_templates.infer_template("x", "", ["/j/a.md"]), "text_notes")
        self.assertEqual(app_templates.infer_template("x", "", ["/j/a.html"]), "browser")

    def test_describe_plain(self) -> None:
        text = app_templates.describe_plain("browser", "Firefox")
        self.assertIn("Firefox", text)
        self.assertIn("browser", text.lower())


class PromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="promo_"))
        self.ov = self.tmp / "apps.json"
        os.environ["QUILL_DESKTOP_APPS"] = str(self.ov)

    def tearDown(self) -> None:
        os.environ.pop("QUILL_DESKTOP_APPS", None)

    def test_promote_writes_overlay_and_reloads(self) -> None:
        from desktop_agent import config as cfg

        exe = self.tmp / "obsidian"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
        before = set(cfg.APP_CANDIDATES)
        r = app_promotion.promote_app(
            key="obsidian", exe_path=str(exe), template_id="text_notes",
            display_name="Obsidian", packet_id=42)
        self.assertTrue(r["ok"])
        self.assertTrue(self.ov.is_file())
        self.assertIn("obsidian", cfg.APP_CANDIDATES)
        self.assertTrue(app_promotion.is_promoted("obsidian"))
        data = json.loads(self.ov.read_text(encoding="utf-8"))
        self.assertEqual(data["obsidian"]["_promoted"]["packet_id"], 42)

    def test_maybe_promote_skips_when_not_remembered(self) -> None:
        r = app_promotion.maybe_promote_from_approval({
            "action": "launch_unlisted_app",
            "app": "obsidian",
            "exe": "/usr/bin/obsidian",
            "remember_app": False,
        })
        self.assertIsNone(r)

    def test_revoke_removes_overlay_entry(self) -> None:
        exe = self.tmp / "foo"
        exe.write_text("x", encoding="utf-8")
        app_promotion.promote_app(key="fooapp", exe_path=str(exe), template_id="browser")
        self.assertTrue(app_promotion.is_promoted("fooapp"))
        r = app_promotion.revoke_promotion("fooapp")
        self.assertTrue(r["ok"])
        self.assertFalse(app_promotion.is_promoted("fooapp"))


if __name__ == "__main__":
    unittest.main()
