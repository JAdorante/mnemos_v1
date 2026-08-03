"""Unit tests for desktop_agent.config.desktop_autoapprove — the granular
autonomy policy (strategic doc #4).

Decides, per verb, whether an autonomous run auto-approves it or defers to the
human. Pure over env config; the driver's actual gate is unaffected — a False
here only ever means "ask the human", never "allow more".
"""
from __future__ import annotations

import unittest

from desktop_agent import config as cfg


class AutonomyBase(unittest.TestCase):
    _FLAGS = ("AGENT_AUTONOMY_DESKTOP", "AGENT_AUTONOMY_SHELL")

    def setUp(self) -> None:
        self._saved = {k: getattr(cfg, k) for k in self._FLAGS}
        cfg.AGENT_AUTONOMY_SHELL = False

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(cfg, k, v)

    def auto(self, level: str) -> set[str]:
        cfg.AGENT_AUTONOMY_DESKTOP = level
        verbs = ["launch_app", "make_dir", "write_file", "run_command",
                 "click_at", "type_text", "press_key"]
        return {v for v in verbs if cfg.desktop_autoapprove(v)}


class LevelLadderTests(AutonomyBase):
    def test_off_auto_approves_no_mutating_verb(self) -> None:
        self.assertEqual(self.auto("off"), set())

    def test_launch_only(self) -> None:
        self.assertEqual(self.auto("launch_only"), {"launch_app"})

    def test_jailed_files_adds_authoring(self) -> None:
        self.assertEqual(self.auto("jailed_files"),
                         {"launch_app", "make_dir", "write_file"})

    def test_ui_control_adds_pixel_verbs(self) -> None:
        self.assertEqual(self.auto("ui_control"),
                         {"launch_app", "make_dir", "write_file",
                          "click_at", "type_text", "press_key"})

    def test_full_is_everything_except_shell(self) -> None:
        # run_command stays out — it's governed only by the shell axis.
        self.assertEqual(self.auto("full"),
                         {"launch_app", "make_dir", "write_file",
                          "click_at", "type_text", "press_key"})

    def test_levels_are_monotonic(self) -> None:
        prev: set[str] = set()
        for lvl in cfg.DESKTOP_AUTONOMY_LEVELS:
            got = self.auto(lvl)
            self.assertTrue(prev <= got, f"{lvl} not a superset of the prior level")
            prev = got


class ReadOnlyAndUnknownTests(AutonomyBase):
    def test_read_only_verbs_always_auto(self) -> None:
        for lvl in cfg.DESKTOP_AUTONOMY_LEVELS:
            cfg.AGENT_AUTONOMY_DESKTOP = lvl
            for v in ("list_dir", "screenshot"):
                self.assertTrue(cfg.desktop_autoapprove(v),
                                f"{v} should auto at level {lvl}")

    def test_unknown_verb_never_auto(self) -> None:
        cfg.AGENT_AUTONOMY_DESKTOP = "full"
        for v in ("delete_file", "frobnicate", "", None):
            self.assertFalse(cfg.desktop_autoapprove(v))

    def test_invalid_level_treated_as_off(self) -> None:
        cfg.AGENT_AUTONOMY_DESKTOP = "banana"
        self.assertFalse(cfg.desktop_autoapprove("launch_app"))


class ShellAxisTests(AutonomyBase):
    def test_shell_off_gates_run_command_even_at_full(self) -> None:
        cfg.AGENT_AUTONOMY_DESKTOP = "full"
        cfg.AGENT_AUTONOMY_SHELL = False
        self.assertFalse(cfg.desktop_autoapprove("run_command"))

    def test_shell_on_allows_run_command(self) -> None:
        cfg.AGENT_AUTONOMY_SHELL = True
        cfg.AGENT_AUTONOMY_DESKTOP = "off"   # shell is an independent axis
        self.assertTrue(cfg.desktop_autoapprove("run_command"))


class ExplicitOverrideTests(AutonomyBase):
    def test_level_argument_overrides_env(self) -> None:
        cfg.AGENT_AUTONOMY_DESKTOP = "off"
        self.assertTrue(cfg.desktop_autoapprove("click_at", level="ui_control"))
        self.assertFalse(cfg.desktop_autoapprove("click_at", level="jailed_files"))

    def test_shell_argument_overrides_env(self) -> None:
        cfg.AGENT_AUTONOMY_SHELL = False
        self.assertTrue(cfg.desktop_autoapprove("run_command", shell=True))
        self.assertFalse(cfg.desktop_autoapprove("run_command", shell=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
