"""Local-tier hardening — the fixes for the infra-driven escalation share.

Telemetry (week of 2026-08-24) showed ~42% of text escalations were
infrastructure, not judgement: a probe cached negative forever, no retry on a
transient local error, truncations mislabeled as low_confidence, and every
escalation — however mundane — paying the accurate-tier price. Covered here:

  * availability probe: a NEGATIVE result expires after local_probe_ttl_s
    (Ollama starting after Sparrow no longer pins the session to Claude);
    within the TTL the cached answer is used without re-probing.
  * half-open breaker: one local error triggers a cheap probe — alive means
    one free local retry; dead means mark-down (skip the timeout wait on the
    next calls) and escalate this one.
  * truncation labeling: a generation that hit num_predict escalates as
    local_truncated, not low_confidence/parse_failure.
  * ollama_text's free rescue: truncated output re-runs once with double the
    budget; an unparseable reply is re-asked once with a correction.
  * reason-tiered parent: infra-driven escalations on ambient tasks go to the
    cheap parent; chat/plan and explicit model= overrides never downgrade.
  * multi-reason logging + parent_sim (did escalating change the answer?).

Providers are faked; no network, no real model calls.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.services import model_router as mr
from app.services.escalate_log import escalate_log
from app.services.ollama_text import OllamaText


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _cfg(**over):
    base = dict(enabled=True, local_model="fake-local",
                ollama_url="http://127.0.0.1:1", local_timeout_s=1.0,
                escalate_min_conf=0.6, high_stakes_tasks=("plan",),
                fewshot_k=0, fewshot_min_sim=0.4, fewshot_conf_weight=0.85,
                local_max_prompt_chars=12_000, local_probe_ttl_s=60.0,
                cheap_parent_model="cheap-tier",
                cheap_parent_reasons=("local_error", "local_unavailable",
                                      "parse_failure", "local_truncated"))
    base.update(over)
    return SimpleNamespace(**base)


class _FakeLocal:
    """Local stand-in with scriptable failures and a countable probe."""

    def __init__(self, res=None, *, fail_times: int = 0, available: bool = True):
        self.model = "fake-local"
        self.url = "http://127.0.0.1:1"
        self._res = res or {"text": "local answer", "json": None,
                            "confidence": 0.9, "parse_ok": True}
        self.fail_times = fail_times
        self.available_result = available
        self.calls = 0
        self.probe_calls = 0

    def available(self) -> bool:
        self.probe_calls += 1
        return self.available_result

    def complete(self, task, *, system, messages, max_tokens=1024,
                 schema=None, exemplars=""):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("gpu hiccup")
        return dict(self._res)


class _TempTrailMixin:
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_hardening_"))
        self.trail = self.tmp / "escalate_distill.jsonl"
        self._orig = (escalate_log._path, escalate_log._counts,
                      escalate_log._total)
        escalate_log._path = self.trail
        from collections import Counter
        escalate_log._counts = Counter()
        escalate_log._total = 0
        self._router_env = mock.patch.dict(
            os.environ, {"QUILL_ROUTER_DIR": str(self.tmp / "router")},
            clear=False)
        self._router_env.start()

    def tearDown(self) -> None:
        self._router_env.stop()
        escalate_log._path, escalate_log._counts, escalate_log._total = self._orig


class _RouterHarness(_TempTrailMixin):
    def _router(self, local: _FakeLocal, *, local_ok=True,
                claude_text="parent answer") -> mr.ModelRouter:
        r = mr.ModelRouter()
        r._local = local
        r._local_ok = local_ok
        r._local_probe_t = time.time()
        r._complete_claude = mock.Mock(return_value=claude_text)
        return r

    def _complete(self, r, task="chat", **kw):
        with mock.patch.object(mr, "_text_cfg",
                               return_value=kw.pop("cfg", _cfg())):
            return r.complete(task, system="s",
                              messages=[{"role": "user", "content": "q"}],
                              **kw)


class ProbeTTLTests(_RouterHarness, unittest.TestCase):
    def test_negative_probe_expires_and_local_recovers(self) -> None:
        # Probe said "down" long ago; Ollama has since come up. The old
        # cache-forever behavior kept paying Claude for the whole session.
        local = _FakeLocal(available=True)
        r = self._router(local, local_ok=False)
        r._local_probe_t = time.time() - 3600
        out = self._complete(r)
        self.assertEqual(out, "local answer")
        self.assertEqual(local.probe_calls, 1)
        r._complete_claude.assert_not_called()
        self.assertTrue(r._local_ok)

    def test_negative_probe_is_cached_within_the_ttl(self) -> None:
        local = _FakeLocal(available=True)
        r = self._router(local, local_ok=False)   # probe_t = now
        out = self._complete(r)
        self.assertEqual(out, "parent answer")
        self.assertEqual(local.probe_calls, 0)    # no re-probe storm
        self.assertEqual(local.calls, 0)
        self.assertEqual(_rows(self.trail)[0]["reason"], "local_unavailable")

    def test_positive_probe_is_not_re_probed(self) -> None:
        local = _FakeLocal(available=True)
        r = self._router(local, local_ok=True)
        r._local_probe_t = time.time() - 3600
        self._complete(r)
        self.assertEqual(local.probe_calls, 0)


class HalfOpenBreakerTests(_RouterHarness, unittest.TestCase):
    def test_transient_error_retries_locally_for_free(self) -> None:
        local = _FakeLocal(fail_times=1, available=True)
        r = self._router(local)
        out = self._complete(r)
        self.assertEqual(out, "local answer")
        self.assertEqual(local.calls, 2)           # failed once, retried once
        self.assertEqual(local.probe_calls, 1)     # the half-open probe
        r._complete_claude.assert_not_called()     # no paid rescue needed

    def test_dead_daemon_marks_local_down_and_escalates(self) -> None:
        local = _FakeLocal(fail_times=99, available=False)
        r = self._router(local, claude_text="rescued")
        out = self._complete(r)
        self.assertEqual(out, "rescued")
        self.assertEqual(local.calls, 1)           # no blind retry into a wall
        self.assertIs(r._local_ok, False)          # next calls skip the wait
        self.assertEqual(_rows(self.trail)[0]["reason"], "local_error")

    def test_error_after_alive_probe_still_escalates(self) -> None:
        local = _FakeLocal(fail_times=99, available=True)
        r = self._router(local, claude_text="rescued")
        out = self._complete(r)
        self.assertEqual(out, "rescued")
        self.assertEqual(local.calls, 2)           # one retry, then give up
        self.assertIsNot(r._local_ok, False)       # daemon is up — not marked down
        self.assertEqual(_rows(self.trail)[0]["reason"], "local_error")


class TruncationLabelTests(_RouterHarness, unittest.TestCase):
    def test_truncated_empty_answer_is_labeled_honestly(self) -> None:
        # An unterminated <think> that ate num_predict used to be logged as
        # low_confidence, hiding a token-budget problem in a judgement metric.
        local = _FakeLocal({"text": "", "json": None, "confidence": None,
                            "parse_ok": True, "truncated": True})
        r = self._router(local)
        self._complete(r)
        self.assertEqual(_rows(self.trail)[0]["reason"], "local_truncated")

    def test_truncated_parse_failure_is_labeled_honestly(self) -> None:
        local = _FakeLocal({"text": '{"tasks": ["x', "json": None,
                            "confidence": 0.0, "parse_ok": False,
                            "truncated": True})
        r = self._router(local, claude_text='{"tasks": []}')
        with mock.patch.object(mr, "_text_cfg", return_value=_cfg()):
            r.complete_json("extract", system="s",
                            messages=[{"role": "user", "content": "t"}],
                            schema={"type": "object",
                                    "properties": {"tasks": {"type": "array"}}})
        self.assertEqual(_rows(self.trail)[0]["reason"], "local_truncated")


class CheapParentTierTests(_RouterHarness, unittest.TestCase):
    def test_ambient_infra_escalation_uses_the_cheap_parent(self) -> None:
        local = _FakeLocal(fail_times=99, available=False)
        r = self._router(local)
        self._complete(r, task="reflect")
        self.assertEqual(r._complete_claude.call_args.kwargs["model"],
                         "cheap-tier")
        self.assertEqual(_rows(self.trail)[0]["parent_model"], "cheap-tier")

    def test_chat_never_downgrades_on_infra_reasons(self) -> None:
        # User-initiated: the user asked, so answer quality is the point.
        local = _FakeLocal(fail_times=99, available=False)
        r = self._router(local)
        self._complete(r, task="chat")
        self.assertEqual(r._complete_claude.call_args.kwargs["model"],
                         r.model_for("chat"))

    def test_low_confidence_keeps_the_configured_parent(self) -> None:
        # A judgement escalation is exactly where the accurate tier earns it.
        local = _FakeLocal({"text": "meh", "json": None, "confidence": 0.2,
                            "parse_ok": True})
        r = self._router(local)
        self._complete(r, task="reflect")
        self.assertEqual(r._complete_claude.call_args.kwargs["model"],
                         r.model_for("reflect"))

    def test_explicit_model_override_always_wins(self) -> None:
        local = _FakeLocal(fail_times=99, available=False)
        r = self._router(local)
        self._complete(r, task="reflect", model="pinned-model")
        self.assertEqual(r._complete_claude.call_args.kwargs["model"],
                         "pinned-model")


class ReasonTelemetryTests(_RouterHarness, unittest.TestCase):
    def test_every_tripped_gate_is_recorded_not_just_the_first(self) -> None:
        # high-stakes AND unsure: the single-label histogram used to
        # undercount everything below the winning if/elif branch.
        local = _FakeLocal({"text": "plan", "json": None, "confidence": 0.2,
                            "parse_ok": True})
        r = self._router(local)
        self._complete(r, task="plan")
        row = _rows(self.trail)[0]
        self.assertEqual(row["reason"], "high_stakes_task")
        self.assertEqual(row["meta"]["reasons"],
                         ["high_stakes_task", "low_confidence"])

    def test_single_reason_rows_stay_clean(self) -> None:
        local = _FakeLocal({"text": "meh", "json": None, "confidence": 0.2,
                            "parse_ok": True})
        r = self._router(local)
        self._complete(r)
        self.assertNotIn("reasons", _rows(self.trail)[0]["meta"])

    def test_parent_sim_rides_the_distill_row(self) -> None:
        local = _FakeLocal({"text": "meh", "json": None, "confidence": 0.2,
                            "parse_ok": True})
        r = self._router(local)
        with mock.patch.object(mr, "_answer_sim", return_value=0.93):
            self._complete(r)
        self.assertEqual(_rows(self.trail)[0]["meta"]["parent_sim"], 0.93)


class PerTaskConfidenceFloorTests(_RouterHarness, unittest.TestCase):
    def test_env_override_lowers_the_bar_for_one_task(self) -> None:
        local = _FakeLocal({"text": "hi", "json": None, "confidence": 0.4,
                            "parse_ok": True})
        r = self._router(local)
        with mock.patch.dict(os.environ,
                             {"QUILL_TEXT_ESCALATE_MIN_CONF_CHAT": "0.3"}):
            out = self._complete(r)
        self.assertEqual(out, "hi")
        r._complete_claude.assert_not_called()

    def test_other_tasks_keep_the_global_floor(self) -> None:
        local = _FakeLocal({"text": "hi", "json": None, "confidence": 0.4,
                            "parse_ok": True})
        r = self._router(local)
        with mock.patch.dict(os.environ,
                             {"QUILL_TEXT_ESCALATE_MIN_CONF_CHAT": "0.3"}):
            self._complete(r, task="reflect")
        self.assertEqual(r._complete_claude.call_count, 1)


class _SeqCapture:
    """urlopen stand-in returning scripted replies in order."""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.payloads: list[dict] = []

    def __call__(self, req, timeout=None):
        self.payloads.append(json.loads(req.data.decode("utf-8")))
        reply = self.replies[min(len(self.payloads) - 1,
                                 len(self.replies) - 1)]
        body = json.dumps(reply).encode("utf-8")

        class R:
            def read(self, *a):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()


class LocalRetryTests(unittest.TestCase):
    """ollama_text.complete's free rescue pass — before any paid escalation."""

    def _complete(self, cap, **kw):
        with mock.patch("urllib.request.urlopen", cap):
            return OllamaText().complete(
                "chat", system="S",
                messages=[{"role": "user", "content": "hi"}], **kw)

    def test_truncated_reply_retries_with_double_budget(self) -> None:
        cap = _SeqCapture([
            {"message": {"content": "<think>counting and coun"},
             "done_reason": "length"},
            {"message": {"content": "The answer is 4.\nCONFIDENCE: 0.9"},
             "done_reason": "stop"},
        ])
        out = self._complete(cap, max_tokens=100)
        self.assertEqual(out["text"], "The answer is 4.")
        self.assertEqual(out["confidence"], 0.9)
        self.assertFalse(out["truncated"])
        self.assertEqual(len(cap.payloads), 2)
        self.assertEqual(cap.payloads[0]["options"]["num_predict"], 100)
        self.assertEqual(cap.payloads[1]["options"]["num_predict"], 200)

    def test_clean_reply_makes_exactly_one_call(self) -> None:
        cap = _SeqCapture([
            {"message": {"content": "ok\nCONFIDENCE: 0.9"},
             "done_reason": "stop"},
        ])
        out = self._complete(cap)
        self.assertEqual(out["confidence"], 0.9)
        self.assertEqual(len(cap.payloads), 1)

    def test_bad_json_is_reasked_once_with_a_correction(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "number"}}}
        cap = _SeqCapture([
            {"message": {"content": "sure! here you go"},
             "done_reason": "stop"},
            {"message": {"content": '{"a": 1, "confidence": 0.8}'},
             "done_reason": "stop"},
        ])
        out = self._complete(cap, schema=schema)
        self.assertTrue(out["parse_ok"])
        self.assertEqual(out["json"], {"a": 1})
        self.assertEqual(out["confidence"], 0.8)
        # The retry carries the bad reply + a correction, so temperature-0
        # decoding actually produces something different.
        msgs = cap.payloads[1]["messages"]
        self.assertEqual(len(msgs), len(cap.payloads[0]["messages"]) + 2)
        self.assertEqual(msgs[-2]["role"], "assistant")
        self.assertIn("valid JSON", msgs[-1]["content"])

    def test_still_bad_after_retry_reports_parse_failure(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "number"}}}
        cap = _SeqCapture([
            {"message": {"content": "nope"}, "done_reason": "stop"},
        ])
        out = self._complete(cap, schema=schema)
        self.assertFalse(out["parse_ok"])
        self.assertEqual(out["confidence"], 0.0)
        self.assertEqual(len(cap.payloads), 2)     # one retry, then stop

    def test_truncated_json_reports_truncated(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "number"}}}
        cap = _SeqCapture([
            {"message": {"content": '{"a": 1'}, "done_reason": "length"},
        ])
        out = self._complete(cap, schema=schema)
        self.assertFalse(out["parse_ok"])
        self.assertTrue(out["truncated"])
        # Retry widened the budget before giving up.
        self.assertEqual(cap.payloads[1]["options"]["num_predict"],
                         2 * cap.payloads[0]["options"]["num_predict"])


if __name__ == "__main__":
    unittest.main()


def setUpModule() -> None:
    # Telemetry sandbox — same reason as the other router test modules: the
    # faked calls in here must not append rows to the real model_calls trail.
    global _model_log_orig_path
    import tempfile as _tempfile
    from app.services.model_log import model_log as _ml
    _model_log_orig_path = _ml._path
    _ml._path = (Path(_tempfile.mkdtemp(prefix="mnemos-test-telemetry-"))
                 / "model_calls.jsonl")


def tearDownModule() -> None:
    from app.services.model_log import model_log as _ml
    _ml._path = _model_log_orig_path
