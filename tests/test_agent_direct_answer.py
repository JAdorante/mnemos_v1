"""The agent's no-browser answer path routes through the ModelRouter.

Finding #2 from the 2026-07-17 live test: with the agent on, typed chat never
touched the ModelRouter, so the few-shot learning loop only ever saw ambient
traffic. `LLM.direct_answer` now routes task="chat" through the router when
the app package is importable and QUILL_TEXT_LOCAL is on — and falls back to
its original direct Claude call when standalone, disabled, or on any router
failure. The route/plan/execute ladder stays Claude-internal (by design).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import app.config as app_config
from app.services import model_router as mr
from browser_agent.llm import LLM


def _fake_llm() -> LLM:
    """LLM without __init__ — no Anthropic client construction, no key needed."""
    llm = LLM.__new__(LLM)
    llm.usage = {}
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="claude says")],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2,
                              cache_read_input_tokens=0,
                              cache_creation_input_tokens=0))
    llm.client = SimpleNamespace(
        messages=SimpleNamespace(create=mock.Mock(return_value=resp)))
    return llm


def _settings(enabled: bool):
    return SimpleNamespace(text_local=SimpleNamespace(enabled=enabled))


class DirectAnswerRoutingTests(unittest.TestCase):
    def test_routes_through_model_router_when_enabled(self) -> None:
        llm = _fake_llm()
        with mock.patch.object(app_config, "settings", _settings(True)), \
             mock.patch.object(mr.router, "complete",
                               return_value="local says hi") as complete, \
             mock.patch.object(type(mr.router), "last_distill_id",
                               new_callable=mock.PropertyMock,
                               return_value="abc123"):
            out = llm.direct_answer("what's my next meeting?", context="ctx")
        self.assertEqual(out, "local says hi")
        self.assertEqual(llm.last_distill_id, "abc123")
        self.assertEqual(complete.call_args.args[0], "chat")
        self.assertIn("what's my next meeting?",
                      complete.call_args.kwargs["messages"][0]["content"])
        llm.client.messages.create.assert_not_called()   # no double-billing

    def test_flag_off_uses_original_claude_call(self) -> None:
        llm = _fake_llm()
        with mock.patch.object(app_config, "settings", _settings(False)), \
             mock.patch.object(mr.router, "complete") as complete:
            out = llm.direct_answer("hi")
        self.assertEqual(out, "claude says")
        self.assertIsNone(llm.last_distill_id)
        complete.assert_not_called()
        llm.client.messages.create.assert_called_once()
        self.assertEqual(llm.usage[list(llm.usage)[0]]["in"], 3)  # cost tracked

    def test_router_failure_falls_back_to_claude(self) -> None:
        llm = _fake_llm()
        with mock.patch.object(app_config, "settings", _settings(True)), \
             mock.patch.object(mr.router, "complete",
                               side_effect=RuntimeError("ollama gone")):
            out = llm.direct_answer("hi")
        self.assertEqual(out, "claude says")
        self.assertIsNone(llm.last_distill_id)
        llm.client.messages.create.assert_called_once()

    def test_empty_router_reply_falls_back(self) -> None:
        llm = _fake_llm()
        with mock.patch.object(app_config, "settings", _settings(True)), \
             mock.patch.object(mr.router, "complete", return_value="  "):
            out = llm.direct_answer("hi")
        self.assertEqual(out, "claude says")
        self.assertIsNone(llm.last_distill_id)

    def test_local_only_answer_has_no_distill_id(self) -> None:
        llm = _fake_llm()
        with mock.patch.object(app_config, "settings", _settings(True)), \
             mock.patch.object(mr.router, "complete", return_value="kept local"), \
             mock.patch.object(type(mr.router), "last_distill_id",
                               new_callable=mock.PropertyMock,
                               return_value=None):
            out = llm.direct_answer("hi")
        self.assertEqual(out, "kept local")
        self.assertIsNone(llm.last_distill_id)


if __name__ == "__main__":
    unittest.main()


def setUpModule() -> None:
    # Telemetry sandbox: model_log resolves its trail path once at import, so
    # without this every faked model call in this module appends a bogus row
    # (fake models, 0s latency) to the REAL data/model_calls.jsonl trail.
    global _model_log_orig_path
    import tempfile as _tempfile
    from pathlib import Path as _Path
    from app.services.model_log import model_log as _ml
    _model_log_orig_path = _ml._path
    _ml._path = (_Path(_tempfile.mkdtemp(prefix="mnemos-test-telemetry-"))
                 / "model_calls.jsonl")


def tearDownModule() -> None:
    from app.services.model_log import model_log as _ml
    _ml._path = _model_log_orig_path
