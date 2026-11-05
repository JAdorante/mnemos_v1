"""One model seam: every paid call goes through ModelRouter.

The router's paid path carries the spend cap, the redact-or-refuse privacy
gate, the parent-provider choice, the cost log and the distillation trail.
A module that builds its own `anthropic.Anthropic()` skips all of them, so
this file pins who may import the SDK, and that the callers moved onto
`router.complete_parent` keep their behavior.
"""
from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

# The router's own backend, the provider registry (its key-validation ping
# needs a user-pasted key the router's env client cannot take), and the org
# coordinator's availability probe. Nothing else may import the SDK.
ALLOWED = {
    "app/services/model_router.py",
    "app/services/parent_model.py",
    "org_coordinator/parent.py",
    # KNOWN EXCEPTION, not a precedent. The browser agent's tool-use loop
    # needs raw responses, tools, prompt caching and effort, and it sends
    # page content that must reach the model unredacted (the recipient's
    # address is the thing it types). The router's privacy gate redacts
    # personal content, so routing it needs a gate policy for agent actions
    # first. Until then its spend shows in its own cost(), not model_calls.
    "browser_agent/llm.py",
}


def _imports_anthropic(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "anthropic" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "anthropic":
                return True
    return False


class NoDirectCallersTests(unittest.TestCase):
    def test_only_the_seam_imports_the_sdk(self):
        offenders = []
        for top in ("app", "org_coordinator", "browser_agent",
                    "desktop_agent", "mcp_server"):
            for p in (ROOT / top).rglob("*.py"):
                rel = p.relative_to(ROOT).as_posix()
                if rel in ALLOWED or "__pycache__" in rel:
                    continue
                if _imports_anthropic(p):
                    offenders.append(rel)
        self.assertEqual(offenders, [], (
            "these modules call Anthropic directly and skip the cost log, "
            "privacy gate, provider choice and distill trail; use "
            "router.complete / complete_json / complete_parent instead"))


def _resp(text="ok", in_tok=11, out_tok=7):
    block = mock.Mock(type="text", text=text)
    return mock.Mock(content=[block], usage=mock.Mock(
        input_tokens=in_tok, output_tokens=out_tok))


class _Sandboxed(unittest.TestCase):
    def setUp(self):
        from app.services.escalate_log import escalate_log
        from app.services.model_log import model_log
        tmp = Path(tempfile.mkdtemp(prefix="mnemos-test-seam-"))
        self._orig = (model_log._path, escalate_log._path)
        model_log._path = tmp / "model_calls.jsonl"
        escalate_log._path = tmp / "escalate_distill.jsonl"
        self.calls_path = model_log._path
        self.distill_path = escalate_log._path
        env = {"QUILL_PARENT_PROVIDER": "anthropic", "QUILL_BUDGET_USD_DAY": "0"}
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()

    def tearDown(self):
        from app.services.escalate_log import escalate_log
        from app.services.model_log import model_log
        model_log._path, escalate_log._path = self._orig
        self._env.stop()

    def rows(self, path):
        if not path.is_file():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines()
                if ln.strip()]


class CompleteParentTests(_Sandboxed):
    def _router(self, resp=None):
        from app.services.model_router import ModelRouter
        r = ModelRouter()
        r._client = mock.Mock()
        r._client.messages.create.return_value = resp or _resp()
        return r

    def test_returns_usage_and_logs_bytes(self):
        r = self._router(_resp("hello", 21, 4))
        out = r.complete_parent(
            "shadow_eval", system="s",
            messages=[{"role": "user", "content": "grade this"}],
            model="claude-haiku-4-5", input_bytes=1234)
        self.assertEqual(out["text"], "hello")
        self.assertEqual((out["input_tokens"], out["output_tokens"]), (21, 4))
        row = self.rows(self.calls_path)[-1]
        self.assertEqual(row["task"], "shadow_eval")
        self.assertEqual(row.get("input_bytes"), 1234)

    def test_writes_parent_only_distill_row(self):
        from app.services import escalate_log as el
        r = self._router(_resp('{"a": 1}'))
        with mock.patch.object(el.escalate_log, "enabled", return_value=True):
            out = r.complete_parent(
                "org_rollup", system="s",
                messages=[{"role": "user", "content": "roll up"}],
                schema={"type": "object"})
        rows = self.rows(self.distill_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "parent_only")
        self.assertEqual(rows[0]["id"], out["distill_id"])
        # parent_only is a kept call, not an escalation, in the histogram.
        self.assertIn("parent_only", el._KEEP_REASONS)

    def test_distill_false_writes_no_row(self):
        from app.services import escalate_log as el
        r = self._router()
        with mock.patch.object(el.escalate_log, "enabled", return_value=True):
            out = r.complete_parent(
                "vision", system="s", distill=False,
                messages=[{"role": "user", "content": "x"}])
        self.assertIsNone(out["distill_id"])
        self.assertEqual(self.rows(self.distill_path), [])

    def test_privacy_gate_applies(self):
        from app.services.privacy_class import PrivacyRefuse
        r = self._router()
        with self.assertRaises(PrivacyRefuse):
            r.complete_parent(
                "org_escalate", system="s",
                messages=[{"role": "user", "content":
                           "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}])
        r._client.messages.create.assert_not_called()

    def test_image_bytes_survive_redaction(self):
        # A base64 payload can contain a substring shaped like a key; the
        # redactor must not rewrite encoded bytes.
        from app.services.redact import redact_payload
        data = "AKIA" + "A" * 16 + " /9j/4AAQSkZJRg"
        msg = [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/jpeg",
                                         "data": data}},
            {"type": "text", "text": "key AKIA" + "B" * 16 + " here"}]}]
        out = redact_payload(msg)
        self.assertEqual(out[0]["content"][0]["source"]["data"], data)
        self.assertNotIn("AKIA" + "B" * 16, out[0]["content"][1]["text"])

    def test_media_stays_on_anthropic_with_other_provider(self):
        r = self._router()
        msgs = [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/jpeg",
                                         "data": "/9j/"}},
            {"type": "text", "text": "describe"}]}]
        with mock.patch.dict(os.environ, {"QUILL_PARENT_PROVIDER": "openai"}), \
                mock.patch("app.services.parent_model.complete") as pm:
            r.complete_parent("vision", system="s", messages=msgs,
                              distill=False)
        pm.assert_not_called()
        r._client.messages.create.assert_called_once()


class CallersUseTheSeamTests(_Sandboxed):
    def test_claude_vlm_describe_goes_through_router(self):
        from app.services import vlm
        from app.services.model_router import router
        fake = {"text": json.dumps({"description": "a desk"}), "model": "m",
                "provider": "claude", "input_tokens": 1, "output_tokens": 1,
                "distill_id": None}
        with mock.patch.object(router, "complete_parent",
                               return_value=fake) as cp:
            out = vlm.ClaudeVLM(model="claude-x").describe(b"\xff\xd8jpeg")
        self.assertEqual(out.get("description"), "a desk")
        kw = cp.call_args.kwargs
        self.assertEqual(cp.call_args.args[0], "vision")
        self.assertEqual(kw["model"], "claude-x")
        self.assertEqual(kw["input_bytes"], 6)
        self.assertFalse(kw["distill"])     # VLMRouter writes its own row
        self.assertIsNotNone(kw["schema"])

    def test_shadow_eval_default_call_uses_router(self):
        from app.services import shadow_eval
        from app.services.model_router import router
        fake = {"text": "{}", "model": "m", "provider": "claude",
                "input_tokens": 5, "output_tokens": 2, "distill_id": None}
        with mock.patch.object(router, "complete_parent",
                               return_value=fake) as cp:
            out = shadow_eval._anthropic_call("sys", "u", model="claude-h",
                                              max_tokens=50)
        self.assertEqual(out, ("{}", 5, 2))
        self.assertEqual(cp.call_args.args[0], "shadow_eval")

    def test_org_coordinator_goes_through_router(self):
        from app.services.model_router import router
        from org_coordinator import cascade, escalate, rollup
        fake = {"text": '```json\n{"guidance": "g", "items": []}\n```',
                "model": "m", "provider": "claude", "input_tokens": 1,
                "output_tokens": 1, "distill_id": None}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(router, "complete_parent",
                                  return_value=fake) as cp:
            self.assertEqual(cascade._claude_json("s", "u")["guidance"], "g")
            rollup._claude_json("s", "u")
            escalate._claude_json("s", "u")
        self.assertEqual([c.args[0] for c in cp.call_args_list],
                         ["org_cascade", "org_rollup", "org_escalate"])

    def test_org_coordinator_without_router_falls_back(self):
        from org_coordinator import escalate, parent
        with mock.patch.object(parent, "_router", return_value=None):
            out = escalate._claude_json("s", "u")
        self.assertEqual(out["reason"], parent.NO_ROUTER)
        self.assertFalse(out["escalate"])


if __name__ == "__main__":
    unittest.main()
