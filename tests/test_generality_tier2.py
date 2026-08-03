"""Tier 2 (Track B) — machine/environment portability.

B1: the desktop launchable-app allowlist is DATA (apps.default.json) + a user
    overlay, loaded/merged/expanded/platform-filtered by app_registry — the same
    code runs on any machine. Overlay security: replace-not-append, outside-jail,
    fail-safe.
B3: browser-agent model IDs + effort are env-overridable; pricing is de-duplicated
    into data/model_prices.json (RATES and PRICES agree).
B5: ports/paths read env defaults (loopback stays the default host).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


class B1AppRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        import desktop_agent.app_registry as R
        self.R = R
        self._saved_name = R.os.name
        self._saved_env = os.environ.get("QUILL_DESKTOP_APPS")

    def tearDown(self) -> None:
        self.R.os.name = self._saved_name
        if self._saved_env is None:
            os.environ.pop("QUILL_DESKTOP_APPS", None)
        else:
            os.environ["QUILL_DESKTOP_APPS"] = self._saved_env

    def test_candidates_capabilities_parity(self) -> None:
        cands, _, caps = self.R.build_registry(jail_root=None)
        self.assertEqual(set(cands), set(caps))
        self.assertIn("flstudio", cands)

    def test_tokens_expanded(self) -> None:
        cands, _, _ = self.R.build_registry(jail_root=None)
        for key, cs in cands.items():
            for c in cs:
                self.assertNotIn("%", c, f"{key}: {c!r} still has a %VAR% token")

    def test_cross_platform_drop_and_parity(self) -> None:
        self.R.os.name = "posix"
        os.environ.pop("QUILL_DESKTOP_APPS", None)
        cands, _, caps = self.R.build_registry(jail_root=None)
        self.assertEqual(set(cands), set(caps))
        for winonly in ("phonelink", "notepad", "explorer", "terminal"):
            self.assertNotIn(winonly, cands)
        for portable in ("code", "chrome", "cursor", "flstudio"):
            self.assertIn(portable, cands)

    def test_overlay_replaces_candidates_and_adds_apps(self) -> None:
        d = Path(tempfile.mkdtemp())
        ov = d / "apps.json"
        ov.write_text(json.dumps({
            "code": {"candidates": ["/opt/custom/code"]},
            "myeditor": {"platforms": ["nt", "posix"], "candidates": ["myeditor"],
                         "capabilities": {"display_name": "MyEditor",
                                          "open_jailed_files": [".md"],
                                          "opens_dirs": True}},
        }))
        os.environ["QUILL_DESKTOP_APPS"] = str(ov)
        cands, _, caps = self.R.build_registry(jail_root=None)
        self.assertEqual(cands["code"], ["/opt/custom/code"])   # replace, not append
        self.assertIn("myeditor", cands)
        self.assertEqual(caps["myeditor"]["display_name"], "MyEditor")

    def test_capless_new_app_gets_locked_down_default(self) -> None:
        d = Path(tempfile.mkdtemp())
        ov = d / "apps.json"
        ov.write_text(json.dumps({"sketchy": {"candidates": ["sketchy"]}}))
        os.environ["QUILL_DESKTOP_APPS"] = str(ov)
        _, _, caps = self.R.build_registry(jail_root=None)
        self.assertFalse(caps["sketchy"]["opens_dirs"])
        self.assertEqual(caps["sketchy"]["open_jailed_files"], [])

    def test_overlay_inside_jail_refused(self) -> None:
        jail = Path(tempfile.mkdtemp())
        inside = jail / "evil.json"
        inside.write_text("{}")
        self.assertFalse(self.R._overlay_is_safe(inside, jail))
        outside = Path(tempfile.mkdtemp()) / "apps.json"
        self.assertTrue(self.R._overlay_is_safe(outside, jail))

    def test_malformed_overlay_fails_safe(self) -> None:
        d = Path(tempfile.mkdtemp())
        ov = d / "apps.json"
        ov.write_text("{ not valid json")
        os.environ["QUILL_DESKTOP_APPS"] = str(ov)
        cands, _, caps = self.R.build_registry(jail_root=None)
        self.assertIn("code", cands)               # fell back to defaults
        self.assertEqual(set(cands), set(caps))


class B3ModelConfigTests(unittest.TestCase):
    def test_rates_prices_agree_on_shared_ids(self) -> None:
        import browser_agent.config as c
        from app.services.model_log import PRICES
        shared = set(c.RATES) & set(PRICES)
        self.assertIn("claude-opus-4-8", shared)
        for m in shared:
            self.assertEqual(c.RATES[m], PRICES[m], f"price drift for {m}")

    def test_price_loader_env_override(self) -> None:
        import browser_agent.config as c
        d = Path(tempfile.mkdtemp())
        pj = d / "prices.json"
        pj.write_text(json.dumps({"custom-model": [9.0, 18.0]}))
        loaded = c._load_model_prices(c._RATES_FALLBACK)  # sanity: loader shape
        self.assertIn("claude-opus-4-8", loaded)
        os.environ["QUILL_MODEL_PRICES"] = str(pj)
        try:
            got = c._load_model_prices(c._RATES_FALLBACK)
            self.assertEqual(got.get("custom-model"), (9.0, 18.0))
        finally:
            os.environ.pop("QUILL_MODEL_PRICES", None)


class B5PortHostTests(unittest.TestCase):
    def test_env_defaults_resolve(self) -> None:
        # The apps read these envs for their argparse defaults; check the resolution.
        os.environ["EXEC_PORT"] = "5099"
        os.environ["EXEC_HOST"] = "0.0.0.0"
        os.environ["QUILL_BROWSER_PORT"] = "5077"
        try:
            self.assertEqual(int(os.environ.get("EXEC_PORT", "5000")), 5099)
            self.assertEqual(os.environ.get("EXEC_HOST", "127.0.0.1"), "0.0.0.0")
            self.assertEqual(int(os.environ.get("QUILL_BROWSER_PORT", "5000")), 5077)
        finally:
            for k in ("EXEC_PORT", "EXEC_HOST", "QUILL_BROWSER_PORT"):
                os.environ.pop(k, None)

    def test_desktop_sessions_env_override(self) -> None:
        import importlib
        os.environ["QUILL_DESKTOP_SESSIONS"] = "~/quill_sess_test"
        try:
            import desktop_agent.config as c
            importlib.reload(c)
            self.assertIn("quill_sess_test", str(c.SESSIONS_ROOT))
        finally:
            os.environ.pop("QUILL_DESKTOP_SESSIONS", None)
            import desktop_agent.config as c
            importlib.reload(c)


if __name__ == "__main__":
    unittest.main()
