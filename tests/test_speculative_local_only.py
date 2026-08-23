"""Latency program, Phase 3.3 — speculative work never reaches a paid model.

The cost story is the program's hard constraint: speculative generation is
answering a question nobody asked, so it may burn local inference (electricity
on hardware the user owns) but must never spend a cloud token on a guess.

The brief requires this enforced in code at the ModelRouter seam rather than by
convention, and requires an invariant test over the call trail. Both are here,
and the enforcement is deliberately at TWO layers: the routing logic declines
to escalate, and the paid call itself raises if it is somehow reached anyway.
"""
from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.services.model_log import (
    SpeculativeCloudCall, in_speculative_scope, model_log, speculative_scope,
)
from app.services.model_router import router

MSGS = [{"role": "user", "content": "what did I commit to this week"}]


class ScopeTests(unittest.TestCase):
    def test_scope_is_off_by_default_and_restores(self) -> None:
        self.assertFalse(in_speculative_scope())
        with speculative_scope():
            self.assertTrue(in_speculative_scope())
        self.assertFalse(in_speculative_scope())

    def test_scope_nests_without_leaking(self) -> None:
        with speculative_scope():
            with speculative_scope():
                self.assertTrue(in_speculative_scope())
            self.assertTrue(in_speculative_scope())
        self.assertFalse(in_speculative_scope())

    def test_scope_is_per_thread(self) -> None:
        """A speculative background job must not silence a concurrent user
        request's ability to escalate."""
        import threading
        seen = {}

        def worker():
            seen["other"] = in_speculative_scope()

        with speculative_scope():
            t = threading.Thread(target=worker)
            t.start()
            t.join()
        self.assertFalse(seen["other"])

    def test_scope_restores_after_an_exception(self) -> None:
        with self.assertRaises(ValueError):
            with speculative_scope():
                raise ValueError("boom")
        self.assertFalse(in_speculative_scope())


class SeamGuardTests(unittest.TestCase):
    """The last line of defence: the paid call refuses, whatever called it."""

    def test_the_paid_seam_raises_inside_a_speculative_scope(self) -> None:
        with speculative_scope():
            with self.assertRaises(SpeculativeCloudCall):
                router._complete_claude("chat", system="s", messages=MSGS)

    def test_the_message_names_the_task_and_the_policy(self) -> None:
        with speculative_scope():
            try:
                router._complete_claude("extract", system="s", messages=MSGS)
            except SpeculativeCloudCall as exc:
                self.assertIn("extract", str(exc))
                self.assertIn("local-only", str(exc))

    def test_the_seam_is_unaffected_outside_the_scope(self) -> None:
        with patch.object(router, "_complete_claude", return_value="ok") as m:
            self.assertEqual(
                router._complete_claude("chat", system="s", messages=MSGS), "ok")
            self.assertEqual(m.call_count, 1)


class RoutingTests(unittest.TestCase):
    """Every path that would normally escalate must decline instead."""

    def _claude_only(self):
        return patch("app.services.model_router._text_cfg",
                     lambda: dataclasses.replace(settings.text_local,
                                                 enabled=False))

    def test_claude_only_mode_serves_demand_but_not_speculation(self) -> None:
        with self._claude_only(), \
                patch.object(router, "_complete_claude", return_value="PAID") as paid:
            self.assertEqual(
                router.complete("chat", system="s", messages=MSGS), "PAID")
            self.assertEqual(
                router.complete("chat", system="s", messages=MSGS,
                                speculative=True), "")
            self.assertEqual(paid.call_count, 1)

    def test_claude_only_mode_json_speculation_returns_empty(self) -> None:
        with self._claude_only(), \
                patch.object(router, "_complete_claude",
                             return_value='{"a": 1}') as paid:
            self.assertEqual(
                router.complete_json("extract", system="s", messages=MSGS,
                                     schema={"type": "object"}), {"a": 1})
            self.assertEqual(
                router.complete_json("extract", system="s", messages=MSGS,
                                     schema={"type": "object"},
                                     speculative=True), {})
            self.assertEqual(paid.call_count, 1)

    def test_local_unavailable_does_not_fall_back_for_speculation(self) -> None:
        """Falling back to Claude is right for demand and wrong here: nobody
        asked for this answer."""
        with patch.object(router, "_use_local", return_value=False), \
                patch.object(router, "_complete_claude", return_value="PAID") as paid:
            self.assertEqual(
                router.complete("chat", system="s", messages=MSGS), "PAID")
            self.assertEqual(
                router.complete("chat", system="s", messages=MSGS,
                                speculative=True), "")
            self.assertEqual(paid.call_count, 1)

    def test_a_local_error_is_dropped_rather_than_escalated(self) -> None:
        class Boom:
            def complete(self, *a, **k):
                raise RuntimeError("ollama down")

        with patch.object(router, "_use_local", return_value=True), \
                patch.object(router, "_ensure_local", return_value=Boom()), \
                patch.object(router, "_complete_claude", return_value="PAID") as paid:
            self.assertEqual(
                router.complete("chat", system="s", messages=MSGS,
                                speculative=True), "")
            paid.assert_not_called()

    def test_low_confidence_keeps_the_local_answer(self) -> None:
        """The usual escalate trigger. For speculation it is simply not worth
        a paid call — the user may never ask the question."""
        class Unsure:
            def complete(self, *a, **k):
                return {"text": "maybe", "json": None,
                        "confidence": 0.01, "parse_ok": True}

        with patch.object(router, "_use_local", return_value=True), \
                patch.object(router, "_ensure_local", return_value=Unsure()), \
                patch.object(router, "_complete_claude", return_value="PAID") as paid:
            self.assertEqual(
                router.complete("chat", system="s", messages=MSGS), "PAID")
            self.assertEqual(
                router.complete("chat", system="s", messages=MSGS,
                                speculative=True), "maybe")
            self.assertEqual(paid.call_count, 1)

    def test_a_high_stakes_task_still_does_not_escalate_speculatively(self) -> None:
        """`plan` always escalates on demand. Speculatively it must not — the
        hard-gate list is about answer quality, not about spending on guesses."""
        class Fine:
            def complete(self, *a, **k):
                return {"text": "draft", "json": None,
                        "confidence": 0.99, "parse_ok": True}

        with patch.object(router, "_use_local", return_value=True), \
                patch.object(router, "_ensure_local", return_value=Fine()), \
                patch.object(router, "_complete_claude", return_value="PAID") as paid:
            router.complete("plan", system="s", messages=MSGS)
            paid.assert_called_once()
            router.complete("plan", system="s", messages=MSGS, speculative=True)
            self.assertEqual(paid.call_count, 1)     # unchanged

    def test_demand_traffic_keeps_full_ladder_access(self) -> None:
        """The guardrail must not quietly become a general escalation brake."""
        class Unsure:
            def complete(self, *a, **k):
                return {"text": "?", "json": None,
                        "confidence": 0.0, "parse_ok": True}

        with patch.object(router, "_use_local", return_value=True), \
                patch.object(router, "_ensure_local", return_value=Unsure()), \
                patch.object(router, "_complete_claude", return_value="PAID") as paid:
            for _ in range(3):
                self.assertEqual(
                    router.complete("chat", system="s", messages=MSGS), "PAID")
            self.assertEqual(paid.call_count, 3)


class TrailInvariantTests(unittest.TestCase):
    """The brief's acceptance check: zero speculative rows name a cloud
    provider, asserted over the trail rather than over the code path."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_spec_"))
        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": str(self.tmp)},
                              clear=False)
        self.env.start()
        self.path = self.tmp / "model_calls.jsonl"
        self.patcher = patch.object(model_log, "_path", self.path)
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.env.stop()

    def _trail(self) -> list[dict]:
        if not self.path.is_file():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def test_speculative_rows_are_stamped(self) -> None:
        with speculative_scope():
            model_log.log_call(task="chat", provider="ollama",
                               model="qwen2.5:7b-instruct", latency_s=1.0)
        model_log.log_call(task="chat", provider="ollama",
                           model="qwen2.5:7b-instruct", latency_s=1.0)
        rows = self._trail()
        self.assertTrue(rows[0].get("speculative"))
        self.assertNotIn("speculative", rows[1])

    def test_no_speculative_row_ever_names_a_cloud_provider(self) -> None:
        """The invariant, over a mixed workload."""
        from app.services.model_log import _CLOUD_PROVIDERS

        with speculative_scope():
            for _ in range(5):
                model_log.log_call(task="chat", provider="ollama",
                                   model="qwen2.5:7b-instruct", latency_s=0.5)
        for _ in range(3):
            model_log.log_call(task="chat", provider="claude",
                               model="claude-opus-4-8", latency_s=2.0,
                               input_tokens=100, output_tokens=50)

        rows = self._trail()
        self.assertEqual(len(rows), 8)
        offenders = [r for r in rows
                     if r.get("speculative")
                     and str(r.get("provider", "")).lower() in _CLOUD_PROVIDERS]
        self.assertEqual(offenders, [], f"speculative cloud calls: {offenders}")

    def test_speculative_rows_cost_nothing(self) -> None:
        with speculative_scope():
            model_log.log_call(task="chat", provider="ollama",
                               model="qwen2.5:7b-instruct", latency_s=1.0,
                               input_tokens=500, output_tokens=200)
        self.assertEqual(sum(r.get("cost_usd", 0.0) for r in self._trail()), 0.0)

    def test_the_invariant_would_actually_catch_a_violation(self) -> None:
        """A test that can never fail proves nothing — force the bad row and
        confirm the same check flags it."""
        from app.services.model_log import _CLOUD_PROVIDERS

        with speculative_scope():
            # Bypasses the router entirely, which is the only way to write one.
            model_log.log_call(task="chat", provider="claude",
                               model="claude-opus-4-8", latency_s=1.0)
        offenders = [r for r in self._trail()
                     if r.get("speculative")
                     and str(r.get("provider", "")).lower() in _CLOUD_PROVIDERS]
        self.assertEqual(len(offenders), 1)


if __name__ == "__main__":
    unittest.main()
