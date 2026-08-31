"""Model fetch — installer resume, and the frozen-build path.

The blocker is not the happy path; it is a tester on hotel wifi at 60%. What is
asserted here: a step retries instead of aborting, a failing step does not
cancel the ones after it, the caller still learns something is missing, and
--check never opens a connection.

Also guarded: the packaged build must call this code by *import*. Shelling out
to `[sys.executable, "scripts/download_models.py"]` re-executes `Mnemos.exe` in
a frozen build and the script is not in the bundle either — the first-run page
looked fine and could never have worked.
"""
from __future__ import annotations

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.services import model_fetch as mf

_SPEC = importlib.util.spec_from_file_location(
    "download_models_cli", Path(__file__).resolve().parent.parent
    / "scripts" / "download_models.py")
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)


class _Flaky:
    """Fails `fail_times` times, then succeeds. Counts every call."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("connection reset by peer")


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class TestAttempt(unittest.TestCase):
    def setUp(self) -> None:
        self.slept: list[float] = []
        self.sleep = patch.object(mf.time, "sleep", self.slept.append)
        self.sleep.start()
        self.log = _Log()

    def tearDown(self) -> None:
        self.sleep.stop()

    def test_first_try_costs_no_retry(self) -> None:
        fn = _Flaky(0)
        ok, note = mf.attempt("test model", fn, retries=3, log=self.log)
        self.assertTrue(ok)
        self.assertEqual(note, "")
        self.assertEqual(fn.calls, 1)
        self.assertEqual(self.slept, [])

    def test_retries_and_recovers(self) -> None:
        fn = _Flaky(2)
        ok, _ = mf.attempt("test model", fn, retries=3, log=self.log)
        self.assertTrue(ok)
        self.assertEqual(fn.calls, 3)
        # Backoff doubles rather than hammering a flaky connection.
        self.assertEqual(self.slept, [mf.BACKOFF_BASE_S, mf.BACKOFF_BASE_S * 2])
        # The tester must be told this continues, not restarts.
        self.assertIn("resuming", self.log.text)

    def test_gives_up_after_retries_with_the_reason(self) -> None:
        fn = _Flaky(99)
        ok, note = mf.attempt("test model", fn, retries=2, log=self.log)
        self.assertFalse(ok)
        self.assertEqual(fn.calls, 2)
        self.assertIn("connection reset", note)
        self.assertIn("ConnectionError", note)

    def test_interrupt_is_not_retried(self) -> None:
        def fn() -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            mf.attempt("test model", fn, retries=3, log=self.log)


class TestFetchModels(unittest.TestCase):
    def setUp(self) -> None:
        self.sleep = patch.object(mf.time, "sleep", lambda *_: None)
        self.sleep.start()
        self.log = _Log()

    def tearDown(self) -> None:
        self.sleep.stop()

    def _run(self, plan, **kw) -> bool:
        with patch.object(mf, "steps", lambda sizes=None: plan):
            return mf.fetch_models(log=self.log, **kw)

    def test_all_cached_is_true(self) -> None:
        plan = [("vad", _Flaky(0)), ("whisper", _Flaky(0))]
        self.assertTrue(self._run(plan))
        self.assertIn("2/2 ready.", self.log.text)

    def test_one_failure_does_not_cancel_the_rest(self) -> None:
        doomed, later = _Flaky(99), _Flaky(0)
        plan = [("vad", doomed), ("whisper", later)]
        self.assertFalse(self._run(plan, retries=2))
        self.assertEqual(doomed.calls, 2)
        # The model after the broken one still downloads.
        self.assertEqual(later.calls, 1)
        self.assertIn("1/2 ready.", self.log.text)
        self.assertIn("still missing — vad", self.log.text)
        self.assertIn("re-run to resume", self.log.text)


class TestCheck(unittest.TestCase):
    def test_reports_without_downloading(self) -> None:
        cached, missing = _Flaky(0), _Flaky(99)
        plan = [("vad", cached), ("whisper", missing)]
        log = _Log()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HF_HUB_OFFLINE", None)
            with patch.object(mf, "steps", lambda sizes=None: plan):
                out = mf.check(log=log)
            # Forced offline, so --check can never start a download.
            self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")
        self.assertEqual(out, ["whisper"])
        self.assertIn("NOT cached  whisper", log.text)
        self.assertIn("cached      vad", log.text)
        # Probed once each — no retry storm on a report.
        self.assertEqual(missing.calls, 1)


class TestPlan(unittest.TestCase):
    def test_vad_leads_so_something_finishes_early(self) -> None:
        plan = mf.steps(["tiny"])
        self.assertIn("VAD", plan[0][0])
        self.assertIn("tiny", plan[1][0])

    def test_each_size_is_bound_not_captured_late(self) -> None:
        seen: list[str] = []
        with patch.object(mf, "download_whisper", seen.append):
            plan = mf.steps(["tiny", "base"])
            for _label, fn in plan[1:3]:
                fn()
        self.assertEqual(seen, ["tiny", "base"])


class TestCli(unittest.TestCase):
    def _run(self, argv, **patches):
        with patch.multiple(cli, **patches):
            with redirect_stdout(io.StringIO()) as out:
                code = cli.main(argv)
        return code, out.getvalue()

    def test_check_exits_nonzero_when_something_is_missing(self) -> None:
        code, log = self._run(["download_models.py", "--check"],
                              check=lambda sizes, log: ["whisper 'small'"],
                              default_sizes=lambda: ["small"])
        self.assertEqual(code, 1)
        self.assertIn("still to download", log)

    def test_check_exits_zero_when_everything_is_cached(self) -> None:
        code, log = self._run(["download_models.py", "--check"],
                              check=lambda sizes, log: [],
                              default_sizes=lambda: ["small"])
        self.assertEqual(code, 0)
        self.assertIn("every model is cached", log)

    def test_download_propagates_failure(self) -> None:
        code, _ = self._run(["download_models.py"],
                            fetch_models=lambda **kw: False,
                            default_sizes=lambda: ["small"])
        self.assertEqual(code, 1)


class TestFrozenBootstrapPath(unittest.TestCase):
    """The packaged first-run page must import, never shell out."""

    def test_bootstrap_calls_the_service_in_process(self) -> None:
        from app.api import adoption
        called = {}

        def _fake(*, log) -> bool:
            called["log"] = log
            log("fetching things ...")
            return True

        with patch.object(mf, "fetch_models", _fake):
            ok = adoption._bootstrap_models()
        self.assertTrue(ok)
        self.assertIn("log", called)

    def test_no_subprocess_relaunch_of_the_app(self) -> None:
        """Read the code, not the comments — the docstring names the old bug."""
        import ast
        source = (Path(__file__).resolve().parent.parent
                  / "app" / "api" / "adoption.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.FunctionDef) and n.name == "_bootstrap_models")
        body = fn.body[1:] if ast.get_docstring(fn) else fn.body
        code = "\n".join(ast.unparse(node) for node in body)
        self.assertNotIn("sys.executable", code)
        self.assertNotIn("download_models.py", code)
        self.assertIn("fetch_models", code)

    def test_chromium_is_skipped_in_a_frozen_build(self) -> None:
        from app.api import adoption
        with patch("app.runtime.is_frozen", lambda: True):
            self.assertTrue(adoption._bootstrap_chromium())
        with adoption._bootstrap_lock:
            log = list(adoption._bootstrap_state.get("log") or [])
        self.assertTrue(any("skipped" in line for line in log), log)


if __name__ == "__main__":
    unittest.main()
