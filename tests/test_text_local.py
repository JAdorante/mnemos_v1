"""Tests for the local-first TEXT tier (Task 3: chat/plan local→parent escalate).

Three layers:
  * ollama_text helpers — confidence trailer parse/strip, schema injection,
    tolerant JSON parse.
  * ModelRouter policy — local success stays local (zero Claude calls); a kept
    CHAT answer still writes a labelable local_kept row (buttons on every
    bubble), while kept answers on non-verdict tasks write nothing; escalation
    fires on low confidence / missing confidence / parse failure / high-stakes
    task / local error / local unavailable, calls Claude exactly once, and
    writes one modality="text" distill row.
  * Disabled flag — QUILL_TEXT_LOCAL off routes straight to the Claude path
    with no local probe and no distill row (today's behavior).

Providers are faked; no network, no real model calls.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.services import model_router as mr
from app.services.escalate_log import escalate_log
from app.services.ollama_text import (split_confidence, strip_reasoning,
                                      with_confidence, _parse_json)


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _cfg(enabled=True, min_conf=0.6, high_stakes=("plan",), fewshot_k=0,
         max_prompt=12_000):
    return SimpleNamespace(enabled=enabled, local_model="fake-local",
                           ollama_url="http://127.0.0.1:1", local_timeout_s=1.0,
                           escalate_min_conf=min_conf,
                           high_stakes_tasks=tuple(high_stakes),
                           fewshot_k=fewshot_k, fewshot_min_sim=0.4,
                           fewshot_conf_weight=0.85,
                           local_max_prompt_chars=max_prompt)


class _FakeLocal:
    def __init__(self, res=None, exc: Exception | None = None):
        self.model = "fake-local"
        self.url = "http://127.0.0.1:1"
        self._res = res or {"text": "local answer", "json": None,
                            "confidence": 0.9, "parse_ok": True}
        self._exc = exc
        self.calls = 0

    def complete(self, task, *, system, messages, max_tokens=1024,
                 schema=None, exemplars=""):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return dict(self._res)


class _TempTrailMixin:
    """Point the escalate_log singleton at a temp file for the test.

    Also redirects the escalation router's directory. With QUILL_ROUTER=shadow
    in the developer's own environment, `decide()` appends to the REAL
    data/router/shadow_log.jsonl — so running the suite would inject fixture
    rows into live routing telemetry and skew the weekly router-vs-heuristic
    report. Tests must never write into the user's corpus.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_text_distill_"))
        self.trail = self.tmp / "escalate_distill.jsonl"
        self._orig = (escalate_log._path, escalate_log._counts, escalate_log._total)
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


class HelperTests(unittest.TestCase):
    def test_split_confidence_strips_trailer(self) -> None:
        text, conf = split_confidence("The answer is 4.\nCONFIDENCE: 0.85")
        self.assertEqual(text, "The answer is 4.")
        self.assertEqual(conf, 0.85)

    def test_split_confidence_missing_is_none(self) -> None:
        text, conf = split_confidence("The answer is 4.")
        self.assertEqual(text, "The answer is 4.")
        self.assertIsNone(conf)

    def test_split_confidence_clamps(self) -> None:
        _, conf = split_confidence("x\nconfidence: 1.0")   # case-insensitive
        self.assertEqual(conf, 1.0)

    def test_strip_reasoning_removes_think_block(self) -> None:
        text = strip_reasoning("<think>let me count\n1,2,3,4</think>\n"
                               "The answer is 4.\nCONFIDENCE: 0.85")
        self.assertEqual(text, "The answer is 4.\nCONFIDENCE: 0.85")
        # ...and the trailer still parses, which is the point of stripping first.
        self.assertEqual(split_confidence(text), ("The answer is 4.", 0.85))

    def test_strip_reasoning_noop_without_tags(self) -> None:
        self.assertEqual(strip_reasoning("The answer is 4."), "The answer is 4.")

    def test_strip_reasoning_stray_closing_tag(self) -> None:
        """Prefilled mid-thought: no opener, everything up to </think> is monologue."""
        self.assertEqual(strip_reasoning("counting...</think>\nThe answer is 4."),
                         "The answer is 4.")

    def test_strip_reasoning_unterminated_empties(self) -> None:
        """num_predict hit mid-thought -> no answer at all, so escalate."""
        text = strip_reasoning("<think>let me count and count and coun")
        self.assertEqual(text, "")
        self.assertIsNone(split_confidence(text)[1])

    def test_with_confidence_injects_and_flags(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}},
                  "required": ["a"], "additionalProperties": False}
        out, injected = with_confidence(schema)
        self.assertTrue(injected)
        self.assertIn("confidence", out["properties"])
        self.assertIn("confidence", out["required"])
        # Caller's schema untouched.
        self.assertNotIn("confidence", schema["properties"])

    def test_with_confidence_respects_existing(self) -> None:
        schema = {"type": "object",
                  "properties": {"confidence": {"type": "number"}}}
        out, injected = with_confidence(schema)
        self.assertFalse(injected)
        self.assertIs(out, schema)

    def test_parse_json_tolerates_fences_and_fails_none(self) -> None:
        self.assertEqual(_parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(_parse_json('noise {"a": 1} noise'), {"a": 1})
        self.assertIsNone(_parse_json("not json at all"))


class LongPromptGuardTests(_TempTrailMixin, unittest.TestCase):
    """A small local model handed a whole attached document does not fail
    loudly — it answers fluently from the fraction it held (the grounding
    block) and self-reports high confidence, so every post-hoc gate passes.
    Prompt length is the only signal available before the call."""

    def _router(self, local: _FakeLocal) -> mr.ModelRouter:
        r = mr.ModelRouter()
        r._local = local
        r._local_ok = True
        r._complete_claude = mock.Mock(return_value="parent answer")
        return r

    def _complete(self, r, chars: int, *, cfg=None, task="chat"):
        with mock.patch.object(mr, "_text_cfg", return_value=cfg or _cfg()):
            return r.complete(task, system="s",
                              messages=[{"role": "user", "content": "q" * chars}])

    def test_oversized_prompt_skips_local_entirely(self) -> None:
        local = _FakeLocal()
        r = self._router(local)
        out = self._complete(r, 20_000)
        self.assertEqual(out, "parent answer")
        self.assertEqual(local.calls, 0)          # not even attempted
        r._complete_claude.assert_called_once()
        rows = _rows(self.trail)
        self.assertEqual(rows[0]["reason"], "prompt_too_long_for_local")

    def test_prompt_within_budget_still_runs_local(self) -> None:
        local = _FakeLocal({"text": "hi", "json": None, "confidence": 0.9,
                            "parse_ok": True})
        r = self._router(local)
        out = self._complete(r, 500)
        self.assertEqual(out, "hi")
        self.assertEqual(local.calls, 1)
        r._complete_claude.assert_not_called()

    def test_budget_counts_the_system_prompt_and_every_message(self) -> None:
        local = _FakeLocal()
        r = self._router(local)
        with mock.patch.object(mr, "_text_cfg", return_value=_cfg(max_prompt=1_000)):
            r.complete("chat", system="s" * 600,
                       messages=[{"role": "user", "content": "a" * 300},
                                 {"role": "assistant", "content": "b" * 300}])
        self.assertEqual(local.calls, 0)          # 1200 > 1000, summed
        r._complete_claude.assert_called_once()

    def test_zero_disables_the_guard(self) -> None:
        local = _FakeLocal({"text": "hi", "json": None, "confidence": 0.9,
                            "parse_ok": True})
        r = self._router(local)
        out = self._complete(r, 50_000, cfg=_cfg(max_prompt=0))
        self.assertEqual(out, "hi")
        self.assertEqual(local.calls, 1)

    def test_speculative_prefetch_does_not_buy_a_parent_call(self) -> None:
        # Nobody asked for this answer; paying Claude for it is the wrong
        # trade — leave the cache cold, exactly as when local is unavailable.
        local = _FakeLocal()
        r = self._router(local)
        with mock.patch.object(mr, "_text_cfg", return_value=_cfg()):
            out = r.complete("chat", system="s",
                             messages=[{"role": "user", "content": "q" * 20_000}],
                             speculative=True)
        self.assertEqual(out, "")
        self.assertEqual(local.calls, 0)
        r._complete_claude.assert_not_called()


class RouterPolicyTests(_TempTrailMixin, unittest.TestCase):
    def _router(self, local: _FakeLocal, *, local_ok=True,
                claude_text='{"a": 1}') -> mr.ModelRouter:
        r = mr.ModelRouter()
        r._local = local
        r._local_ok = local_ok       # skip the availability probe
        r._complete_claude = mock.Mock(return_value=claude_text)
        return r

    def _complete(self, r, task="chat", **kw):
        with mock.patch.object(mr, "_text_cfg", return_value=kw.pop("cfg", _cfg())):
            return r.complete(task, system="s",
                              messages=[{"role": "user", "content": "q" * 900}],
                              **kw)

    def test_local_chat_success_no_claude_writes_labelable_row(self) -> None:
        # Kept chat answers get a distill row too — the UI needs a row id to
        # put 👍/👎/✏️ on EVERY bubble, not just escalated ones.
        local = _FakeLocal({"text": "hi", "json": None, "confidence": 0.9,
                            "parse_ok": True})
        r = self._router(local)
        out = self._complete(r)
        self.assertEqual(out, "hi")
        self.assertEqual(local.calls, 1)
        r._complete_claude.assert_not_called()          # no double-billing
        rows = _rows(self.trail)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["reason"], "local_kept")
        self.assertEqual(row["local"]["text"], "hi")
        self.assertIsNone(row["parent"])                # no parent call made
        self.assertEqual(row["user_outcome"], "unknown")
        self.assertIn("messages", row["meta"])          # replayable
        self.assertEqual(r.last_distill_id, row["id"])  # verdict wiring

    def test_local_success_non_verdict_task_writes_nothing(self) -> None:
        # Only verdict-able tasks (chat) persist kept answers — extract/
        # reflect/activity run constantly with no labeling surface.
        local = _FakeLocal({"text": "hi", "json": None, "confidence": 0.9,
                            "parse_ok": True})
        r = self._router(local)
        out = self._complete(r, task="reflect")
        self.assertEqual(out, "hi")
        r._complete_claude.assert_not_called()
        self.assertEqual(_rows(self.trail), [])
        self.assertIsNone(r.last_distill_id)

    def test_retrieval_stats_ride_the_kept_row(self) -> None:
        """D.2b: the stats measured before the call must reach the row router
        training reads. Reconstructing them later would sample a memory store
        that has since grown — a different feature than production saw."""
        stats = {"n_chunks": 3, "max_sim": 0.8, "mean_sim": 0.55,
                 "entity_coverage": 0.5}
        local = _FakeLocal({"text": "hi", "json": None, "confidence": 0.9,
                            "parse_ok": True})
        r = self._router(local)
        self._complete(r, retrieval=stats)
        self.assertEqual(_rows(self.trail)[0]["meta"]["retrieval"], stats)

    def test_retrieval_stats_ride_the_escalated_row(self) -> None:
        stats = {"n_chunks": 0, "max_sim": None, "mean_sim": None,
                 "entity_coverage": 0.0}
        local = _FakeLocal({"text": "meh", "json": None, "confidence": 0.2,
                            "parse_ok": True})
        r = self._router(local, claude_text="parent answer")
        self._complete(r, retrieval=stats)
        self.assertEqual(_rows(self.trail)[0]["meta"]["retrieval"], stats)

    def test_retrieval_is_optional(self) -> None:
        """Every caller that does not ground a prompt keeps working, and the
        row stays clean rather than carrying a misleading empty dict."""
        local = _FakeLocal({"text": "hi", "json": None, "confidence": 0.9,
                            "parse_ok": True})
        r = self._router(local)
        self._complete(r)
        self.assertNotIn("retrieval", _rows(self.trail)[0]["meta"])

    def test_low_confidence_escalates_and_logs(self) -> None:
        local = _FakeLocal({"text": "meh", "json": None, "confidence": 0.2,
                            "parse_ok": True})
        r = self._router(local, claude_text="parent answer")
        out = self._complete(r)
        self.assertEqual(out, "parent answer")
        self.assertEqual(r._complete_claude.call_count, 1)
        rows = _rows(self.trail)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task"], "chat")
        self.assertEqual(row["reason"], "low_confidence")
        self.assertEqual(row["modality"], "text")
        self.assertEqual(row["local_model"], "fake-local")
        self.assertEqual(row["local"]["text"], "meh")
        self.assertEqual(row["local"]["confidence"], 0.2)
        self.assertEqual(row["parent"]["text"], "parent answer")
        self.assertEqual(row["user_outcome"], "unknown")
        # Prompt is truncated, not stored whole (900 chars in, ~500 kept).
        self.assertLessEqual(len(row["meta"]["prompt_head"]), 501)
        # Chat UI wires verdicts to this id via router.last_distill_id.
        self.assertEqual(r.last_distill_id, row["id"])

    def test_local_success_clears_stale_distill_id(self) -> None:
        local = _FakeLocal({"text": "hi", "json": None, "confidence": 0.9,
                            "parse_ok": True})
        r = self._router(local)
        # Seed a stale id as if a prior escalate left one on the thread; a
        # non-verdict task's kept answer must not leak it to the caller.
        mr._tls.distill_id = "stale"
        self._complete(r, task="reflect")
        self.assertIsNone(r.last_distill_id)

    def test_missing_confidence_reads_as_unsure(self) -> None:
        local = _FakeLocal({"text": "unlabeled", "json": None,
                            "confidence": None, "parse_ok": True})
        r = self._router(local)
        self._complete(r)
        self.assertEqual(r._complete_claude.call_count, 1)
        self.assertEqual(_rows(self.trail)[0]["reason"], "low_confidence")

    def test_high_stakes_task_always_escalates(self) -> None:
        local = _FakeLocal({"text": "plan", "json": None, "confidence": 0.99,
                            "parse_ok": True})
        r = self._router(local)
        self._complete(r, task="plan")
        self.assertEqual(r._complete_claude.call_count, 1)
        row = _rows(self.trail)[0]
        self.assertEqual(row["task"], "plan")
        self.assertEqual(row["reason"], "high_stakes_task")

    def test_parse_failure_escalates_json_path(self) -> None:
        local = _FakeLocal({"text": "not json", "json": None,
                            "confidence": 0.0, "parse_ok": False})
        r = self._router(local, claude_text='{"tasks": []}')
        schema = {"type": "object", "properties": {"tasks": {"type": "array"}}}
        with mock.patch.object(mr, "_text_cfg", return_value=_cfg()):
            out = r.complete_json("extract", system="s",
                                  messages=[{"role": "user", "content": "t"}],
                                  schema=schema)
        self.assertEqual(out, {"tasks": []})
        self.assertEqual(r._complete_claude.call_count, 1)
        row = _rows(self.trail)[0]
        self.assertEqual(row["task"], "extract")
        self.assertEqual(row["reason"], "parse_failure")

    def test_local_json_success_stays_local(self) -> None:
        local = _FakeLocal({"text": '{"tasks": ["x"]}', "json": {"tasks": ["x"]},
                            "confidence": 0.95, "parse_ok": True})
        r = self._router(local)
        schema = {"type": "object", "properties": {"tasks": {"type": "array"}}}
        with mock.patch.object(mr, "_text_cfg", return_value=_cfg()):
            out = r.complete_json("extract", system="s",
                                  messages=[{"role": "user", "content": "t"}],
                                  schema=schema)
        self.assertEqual(out, {"tasks": ["x"]})
        r._complete_claude.assert_not_called()
        self.assertEqual(_rows(self.trail), [])

    def test_local_error_fails_open_and_logs(self) -> None:
        local = _FakeLocal(exc=RuntimeError("gpu gone"))
        r = self._router(local, claude_text="rescued")
        out = self._complete(r)
        self.assertEqual(out, "rescued")
        row = _rows(self.trail)[0]
        self.assertEqual(row["reason"], "local_error")
        self.assertIsNone(row["local"])
        self.assertIn("gpu gone", row["local_error"])

    def test_local_unavailable_fails_open_and_logs(self) -> None:
        local = _FakeLocal()
        r = self._router(local, local_ok=False, claude_text="rescued")
        out = self._complete(r)
        self.assertEqual(out, "rescued")
        self.assertEqual(local.calls, 0)
        self.assertEqual(_rows(self.trail)[0]["reason"], "local_unavailable")

    def test_parent_failure_keeps_local_with_labelable_row(self) -> None:
        # Claude down mid-escalation: the shaky local answer is what the user
        # sees, so it must still be labelable (reason marks the failed parent).
        local = _FakeLocal({"text": "shaky", "json": None, "confidence": 0.2,
                            "parse_ok": True})
        r = self._router(local)
        r._complete_claude = mock.Mock(side_effect=RuntimeError("api down"))
        out = self._complete(r)
        self.assertEqual(out, "shaky")
        rows = _rows(self.trail)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "parent_failed")
        self.assertEqual(rows[0]["local"]["text"], "shaky")
        self.assertEqual(r.last_distill_id, rows[0]["id"])

    def test_disabled_routes_straight_to_claude_no_distill(self) -> None:
        local = _FakeLocal()
        r = self._router(local, claude_text="claude only")
        out = self._complete(r, cfg=_cfg(enabled=False))
        self.assertEqual(out, "claude only")
        self.assertEqual(local.calls, 0)
        self.assertEqual(r._complete_claude.call_count, 1)
        self.assertEqual(_rows(self.trail), [])

    def test_disabled_json_parse_degrades_to_empty(self) -> None:
        r = self._router(_FakeLocal(), claude_text="not json")
        with mock.patch.object(mr, "_text_cfg", return_value=_cfg(enabled=False)):
            out = r.complete_json("extract", system="s",
                                  messages=[{"role": "user", "content": "t"}],
                                  schema={"type": "object"})
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
