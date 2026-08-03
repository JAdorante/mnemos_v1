"""Unit tests for desktop_agent.access — the Desktop Access read-model (#5).

Covers the inspectable state, the per-app disable override (persisted + enforced),
the audit-backed recent-actions feed, and that a disabled app is refused end to
end (driver) and reflected in the planner's preflight.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from desktop_agent import access                    # noqa: E402
from desktop_agent import config as cfg             # noqa: E402
from desktop_agent import preflight as pf           # noqa: E402
from desktop_agent.driver import DesktopDriver      # noqa: E402


class AccessBase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: getattr(cfg, k) for k in ("SESSIONS_ROOT", "PIXEL_UI")}
        cfg.SESSIONS_ROOT = Path(tempfile.mkdtemp(prefix="sess_")).resolve()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(cfg, k, v)


class StateShapeTests(AccessBase):
    def test_state_has_environment_and_all_apps(self) -> None:
        st = access.desktop_access_state()
        self.assertIn("environment", st)
        self.assertEqual(len(st["apps"]), len(cfg.APP_CANDIDATES))

    def test_app_row_fields(self) -> None:
        row = access.desktop_access_state()["apps"][0]
        for key in ("key", "display_name", "installed", "resolved_path",
                    "disabled", "launch_allowed", "opens_dirs", "opens_files",
                    "ui_control", "risk"):
            self.assertIn(key, row)

    def test_environment_reflects_config(self) -> None:
        env = access.desktop_access_state()["environment"]
        self.assertEqual(env["jail"], str(cfg.JAIL_ROOT))
        self.assertEqual(env["autonomy_desktop"], cfg.AGENT_AUTONOMY_DESKTOP)
        self.assertIn("auto_verbs", env)
        self.assertIn("gated_verbs", env)

    def test_ui_control_tracks_pixel_ui(self) -> None:
        cfg.PIXEL_UI = True
        rows = {r["key"]: r for r in access.desktop_access_state()["apps"]}
        self.assertEqual(rows["cursor"]["ui_control"], "on")     # UI-capable
        self.assertEqual(rows["explorer"]["ui_control"], "n/a")  # not UI-driven
        cfg.PIXEL_UI = False
        rows = {r["key"]: r for r in access.desktop_access_state()["apps"]}
        self.assertEqual(rows["cursor"]["ui_control"], "off")


class DisableOverrideTests(AccessBase):
    def test_disable_roundtrip(self) -> None:
        self.assertFalse(access.app_disabled("flstudio"))
        self.assertTrue(access.set_app_disabled("flstudio", True))
        self.assertTrue(access.app_disabled("flstudio"))
        self.assertIn("flstudio", access.disabled_apps())
        row = next(r for r in access.desktop_access_state()["apps"]
                   if r["key"] == "flstudio")
        self.assertTrue(row["disabled"])
        self.assertFalse(row["launch_allowed"])
        access.set_app_disabled("flstudio", False)
        self.assertFalse(access.app_disabled("flstudio"))

    def test_unknown_app_toggle_follows_discovery(self) -> None:
        # With runtime discovery on, disabling an arbitrary bare name is a
        # standing "never this app" — it blocks discovery before it starts.
        with mock.patch.object(cfg, "APP_DISCOVERY", True):
            self.assertTrue(access.set_app_disabled("ableton", True))
            self.assertIn("ableton", access.disabled_apps())
            access.set_app_disabled("ableton", False)
        # With discovery off (closed allowlist), unknown keys are rejected.
        with mock.patch.object(cfg, "APP_DISCOVERY", False):
            self.assertFalse(access.set_app_disabled("bitwig", True))
            self.assertNotIn("bitwig", access.disabled_apps())

    def test_override_persists_to_disk(self) -> None:
        access.set_app_disabled("chrome", True)
        path = cfg.SESSIONS_ROOT / "desktop_overrides.json"
        self.assertTrue(path.exists())
        self.assertTrue(json.loads(path.read_text())["chrome"]["disabled"])

    def test_disabled_app_refused_by_driver(self) -> None:
        access.set_app_disabled("flstudio", True)
        d = DesktopDriver(on_log=lambda s: None, on_approve=lambda *a, **k: True,
                          jail_root=Path(os.environ["QUILL_DESKTOP_JAIL"]))
        res = d.launch_app("flstudio")
        self.assertFalse(res["ok"])
        self.assertIn("disabled", res["detail"])

    def test_disabled_app_reflected_in_preflight(self) -> None:
        access.set_app_disabled("flstudio", True)
        r = pf.preflight("make a song in FL Studio", autonomous=False)
        self.assertFalse(r["focus"]["can_launch"])
        self.assertFalse(r["apps"]["flstudio"]["launch_allowed"])
        self.assertTrue(any("Enable" in x for x in r["focus"]["recoveries"]))


class RecentActionsTests(AccessBase):
    def _write_audit(self, records) -> None:
        path = cfg.SESSIONS_ROOT / "desktop_audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                        encoding="utf-8")

    def test_empty_when_no_log(self) -> None:
        self.assertEqual(access.recent_actions(), [])

    def test_returns_newest_first(self) -> None:
        self._write_audit([
            {"ts": 1000, "outcome": "ok", "action": "make_dir", "detail": "a"},
            {"ts": 2000, "outcome": "blocked", "action": "launch_app", "app": "x"},
        ])
        rec = access.recent_actions(10)
        self.assertEqual(len(rec), 2)
        self.assertEqual(rec[0]["action"], "launch_app")   # newest first
        self.assertEqual(rec[0]["outcome"], "blocked")
        self.assertEqual(rec[1]["action"], "make_dir")

    def test_limit_and_bad_lines(self) -> None:
        self._write_audit([{"ts": i, "action": f"a{i}", "outcome": "ok"}
                           for i in range(5)])
        # append a malformed line — must be skipped, not crash.
        with (cfg.SESSIONS_ROOT / "desktop_audit.jsonl").open("a") as f:
            f.write("{not json\n")
        rec = access.recent_actions(2)
        self.assertEqual(len(rec), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
