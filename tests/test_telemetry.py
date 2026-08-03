"""Unit tests for desktop_agent.telemetry — reliability metrics over the audit
log (strategic doc #6). The metric tests double as evals: feed a synthetic
trajectory, assert the numbers.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from desktop_agent import config as cfg          # noqa: E402
from desktop_agent import telemetry              # noqa: E402
from desktop_agent.driver import DesktopDriver   # noqa: E402


class ClassifyRefusalTests(unittest.TestCase):
    CASES = {
        "path escapes jail: '..'": "jail_escape",
        "path argument outside jail: 'x'": "jail_escape",
        "path traversal in argument: '../x'": "jail_escape",
        "argument reaches a sensitive/secret path: '.ssh'": "secret_path",
        "target reaches a sensitive path: x": "secret_path",
        "app 'ableton' not on allowlist or not installed": "unknown_app",
        "app 'flstudio' is disabled in Desktop Access": "disabled_app",
        "blocked verb: 'rm' (destructive)": "blocked_verb",
        "'telnet' is not on the command allowlist": "blocked_verb",
        "shell metacharacter in argument: 'a && b'": "shell_metachar",
        "FL Studio cannot open .txt targets": "capability_mismatch",
        "Notepad does not open folders": "capability_mismatch",
        "action budget exhausted (25); call new_task()": "budget_exhausted",
        "pixel UI disabled (QUILL_DESKTOP_UI=0)": "ui_disabled",
        "file too large: 9 bytes > 5 limit": "file_too_large",
        "timed out after 60s": "timeout",
        "launch failed: boom": "exec_error",
        "something totally new": "other",
    }

    def test_each_reason_maps(self) -> None:
        for detail, category in self.CASES.items():
            with self.subTest(detail=detail):
                self.assertEqual(telemetry.classify_refusal(detail), category)

    def test_case_insensitive(self) -> None:
        self.assertEqual(telemetry.classify_refusal("PATH ESCAPES JAIL"), "jail_escape")


def _rec(tid, outcome, action, detail="", **extra):
    return {"ts": 1, "task_id": tid, "outcome": outcome, "action": action,
            "detail": detail, **extra}


class MetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recs = [
            _rec(1, "ok", "make_dir"),
            _rec(1, "ok", "launch_app", app="cursor"),
            _rec(1, "blocked", "launch_app",
                 "app 'ableton' not on allowlist or not installed"),
            _rec(2, "blocked", "run_command", "blocked verb: 'rm' (destructive)"),
            _rec(2, "blocked", "run_command", "blocked verb: 'rm' (destructive)"),
            _rec(2, "ok", "run_command", argv=["git", "status"], code=0),
            _rec(2, "nonzero", "run_command", argv=["npm", "x"], code=1),
            _rec(2, "blocked", "make_dir",
                 "action budget exhausted (25); call new_task()"),
        ]
        self.m = telemetry.desktop_metrics(self.recs)

    def test_totals(self) -> None:
        t = self.m["totals"]
        self.assertEqual((t["executed"], t["nonzero"], t["refused"]), (3, 1, 4))
        self.assertEqual(t["refusal_rate"], 0.5)

    def test_launch_success_rate(self) -> None:
        self.assertEqual(self.m["launch"],
                         {"attempts": 2, "success": 1, "refused": 1,
                          "success_rate": 0.5})

    def test_run_command_success_rate(self) -> None:
        rc = self.m["run_command"]
        self.assertEqual((rc["ran"], rc["success"], rc["nonzero"], rc["refused"]),
                         (2, 1, 1, 2))
        self.assertEqual(rc["success_rate"], 0.5)

    def test_refusals_by_reason(self) -> None:
        self.assertEqual(self.m["refusals_by_reason"],
                         {"blocked_verb": 2, "budget_exhausted": 1, "unknown_app": 1})

    def test_safety_counters(self) -> None:
        s = self.m["safety"]
        self.assertEqual(s["blocked_verb_attempts"], 2)
        self.assertEqual(s["unknown_app_attempts"], 1)
        self.assertEqual(s["budget_exhausted"], 1)
        self.assertEqual(s["jail_escape_attempts"], 0)

    def test_per_task(self) -> None:
        pt = self.m["per_task"]
        self.assertEqual(pt["tasks"], 2)
        self.assertEqual(pt["avg_actions"], 4.0)   # task1=3, task2=5
        self.assertEqual(pt["max_actions"], 5)
        self.assertEqual(pt["budget_exhaustion_rate"], 0.5)  # task2 exhausted

    def test_repeated_failures(self) -> None:
        # the two consecutive identical 'rm' refusals in task 2 = one repeat.
        self.assertEqual(self.m["repeated_failures"], 1)

    def test_repeat_across_tasks_does_not_count(self) -> None:
        recs = [_rec(1, "blocked", "run_command", "blocked verb: 'rm'"),
                _rec(2, "blocked", "run_command", "blocked verb: 'rm'")]
        self.assertEqual(telemetry.desktop_metrics(recs)["repeated_failures"], 0)

    def test_action_inferred_from_argv(self) -> None:
        # a legacy run_command record without an 'action' field.
        rec = {"ts": 1, "task_id": 1, "outcome": "ok", "argv": ["git", "log"]}
        m = telemetry.desktop_metrics([rec])
        self.assertEqual(m["run_command"]["success"], 1)

    def test_untracked_records_excluded_from_per_task(self) -> None:
        recs = [{"ts": 1, "outcome": "ok", "action": "make_dir"},  # no task_id
                _rec(1, "ok", "make_dir")]
        self.assertEqual(telemetry.desktop_metrics(recs)["per_task"]["tasks"], 1)

    def test_empty(self) -> None:
        m = telemetry.desktop_metrics([])
        self.assertEqual(m["totals"]["records"], 0)
        self.assertEqual(m["launch"]["success_rate"], 0.0)

    def test_report_renders_ascii(self) -> None:
        telemetry.format_report(self.m).encode("ascii")


class LoadAuditTests(unittest.TestCase):
    def test_missing_file(self) -> None:
        self.assertEqual(telemetry.load_audit(Path("nope_does_not_exist.jsonl")), [])

    def test_parse_and_skip_bad_lines(self) -> None:
        p = Path(tempfile.mktemp(suffix=".jsonl"))
        p.write_text('{"ts":1,"outcome":"ok"}\n{bad\n\n{"ts":2,"outcome":"blocked"}\n',
                     encoding="utf-8")
        self.assertEqual(len(telemetry.load_audit(p)), 2)

    def test_window_filters_old(self) -> None:
        import time
        now = time.time()
        p = Path(tempfile.mktemp(suffix=".jsonl"))
        p.write_text(json.dumps({"ts": now - 9999, "outcome": "ok"}) + "\n" +
                     json.dumps({"ts": now, "outcome": "ok"}) + "\n", encoding="utf-8")
        self.assertEqual(len(telemetry.load_audit(p, window_s=100)), 1)


class DriverIntegrationTests(unittest.TestCase):
    """The driver stamps task_id; telemetry groups by it end to end."""

    def test_task_grouping_from_real_driver(self) -> None:
        saved = cfg.SESSIONS_ROOT
        cfg.SESSIONS_ROOT = Path(tempfile.mkdtemp(prefix="sess_")).resolve()
        try:
            jail = Path(tempfile.mkdtemp(prefix="jail_")).resolve()
            d = DesktopDriver(on_log=lambda s: None,
                              on_approve=lambda *a, **k: True, jail_root=jail)
            d.make_dir("a")               # task 1
            d.new_task()
            d.make_dir("b")
            d.make_dir("c")               # task 2
            m = telemetry.desktop_metrics(
                path=cfg.SESSIONS_ROOT / "desktop_audit.jsonl")
            self.assertEqual(m["totals"]["executed"], 3)
            self.assertEqual(m["per_task"]["tasks"], 2)
            self.assertEqual(m["per_task"]["max_actions"], 2)
        finally:
            cfg.SESSIONS_ROOT = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
