"""Parent model — the cloud tier is a user-connected account (Anthropic /
OpenAI / Gemini / Grok), not a hardcoded vendor. Model labels resolve per
provider, keys persist only after live validation, and the router detours to
the OpenAI-compatible dialect only when a non-Anthropic parent is active."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import parent_model as pm


class ProviderResolutionTests(unittest.TestCase):
    def test_default_is_anthropic(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUILL_PARENT_PROVIDER", None)
            self.assertEqual(pm.provider(), "anthropic")

    def test_unknown_provider_falls_back(self):
        with mock.patch.dict(os.environ, {"QUILL_PARENT_PROVIDER": "skynet"}):
            self.assertEqual(pm.provider(), "anthropic")

    def test_anthropic_models_pass_through(self):
        with mock.patch.dict(os.environ, {"QUILL_PARENT_PROVIDER": "anthropic"}):
            self.assertEqual(pm.resolve_model("claude-opus-4-8"),
                             "claude-opus-4-8")

    def test_tier_mapping_on_foreign_provider(self):
        with mock.patch.dict(os.environ, {"QUILL_PARENT_PROVIDER": "openai"}):
            os.environ.pop("QUILL_PARENT_MODEL", None)
            os.environ.pop("QUILL_PARENT_MODEL_FLAGSHIP", None)
            os.environ.pop("QUILL_PARENT_MODEL_LIGHT", None)
            self.assertEqual(pm.resolve_model("claude-opus-4-8"),
                             pm.PROVIDERS["openai"]["flagship"])
            self.assertEqual(pm.resolve_model("claude-haiku-4-5"),
                             pm.PROVIDERS["openai"]["light"])

    def test_forced_model_wins(self):
        with mock.patch.dict(os.environ, {"QUILL_PARENT_PROVIDER": "xai",
                                          "QUILL_PARENT_MODEL": "grok-next"}):
            self.assertEqual(pm.resolve_model("claude-opus-4-8"), "grok-next")

    def test_status_lists_all_providers(self):
        s = pm.status()
        self.assertEqual({p["id"] for p in s["providers"]},
                         set(pm.PROVIDERS))
        self.assertIn(s["active"], pm.PROVIDERS)


class KeyValidationTests(unittest.TestCase):
    def test_prefix_gate_before_any_network(self):
        # Wrong-shaped keys are rejected without a network call.
        self.assertIn("does not look like",
                      pm.validate_key("openai", "AIza-not-openai"))
        self.assertIn("does not look like",
                      pm.validate_key("google", "sk-not-google"))
        self.assertIn("unknown provider", pm.validate_key("skynet", "x"))

    def test_openai_compat_validation_hits_endpoint(self):
        ok = mock.Mock(status_code=200)
        with mock.patch("httpx.post", return_value=ok) as post:
            self.assertIsNone(pm.validate_key("xai", "xai-test-key"))
        url = post.call_args.args[0]
        self.assertTrue(url.startswith(pm.PROVIDERS["xai"]["base"]))

    def test_rejected_key_reports_status(self):
        bad = mock.Mock(status_code=401, text="nope")
        with mock.patch("httpx.post", return_value=bad):
            err = pm.validate_key("google", "AIza-bad")
        self.assertIn("401", err)


class PersistenceTests(unittest.TestCase):
    def test_save_round_trips_provider_and_key(self):
        with tempfile.TemporaryDirectory() as td:
            cred = Path(td) / ".credentials.env"
            cred.write_text("KEEP_ME=1\nOPENAI_API_KEY=old\n",
                            encoding="utf-8")
            with mock.patch.dict(os.environ,
                                 {"QUILL_CREDENTIALS_FILE": str(cred)}):
                pm.save("openai", "sk-new-key")
                text = cred.read_text(encoding="utf-8")
                self.assertIn("KEEP_ME=1", text)
                self.assertIn("QUILL_PARENT_PROVIDER=openai", text)
                self.assertIn("OPENAI_API_KEY=sk-new-key", text)
                self.assertNotIn("OPENAI_API_KEY=old", text)
                self.assertEqual(os.environ["QUILL_PARENT_PROVIDER"], "openai")
                self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-new-key")
            os.environ.pop("QUILL_PARENT_PROVIDER", None)
            os.environ.pop("OPENAI_API_KEY", None)


class CompleteDialectTests(unittest.TestCase):
    def _resp(self):
        r = mock.Mock(status_code=200)
        r.json.return_value = {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        return r

    def test_openai_compat_payload_shape(self):
        with mock.patch.dict(os.environ, {"QUILL_PARENT_PROVIDER": "openai",
                                          "OPENAI_API_KEY": "sk-t"}), \
             mock.patch("httpx.post", return_value=self._resp()) as post:
            os.environ.pop("QUILL_PARENT_MODEL", None)
            out = pm.complete(model="claude-opus-4-8", system="sys",
                              messages=[{"role": "user", "content": "q"}],
                              max_tokens=64,
                              schema={"type": "object"})
        self.assertEqual(out["text"], "hi")
        self.assertEqual(out["provider"], "openai")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"][0],
                         {"role": "system", "content": "sys"})
        # OpenAI's current flagship takes max_completion_tokens, not max_tokens.
        self.assertEqual(payload["max_completion_tokens"], 64)
        self.assertEqual(payload["response_format"]["type"], "json_schema")

    def test_block_content_is_flattened(self):
        with mock.patch.dict(os.environ, {"QUILL_PARENT_PROVIDER": "google",
                                          "GEMINI_API_KEY": "AIza-t"}), \
             mock.patch("httpx.post", return_value=self._resp()) as post:
            pm.complete(model="claude-haiku-4-5", system="",
                        messages=[{"role": "user", "content": [
                            {"type": "text", "text": "a"},
                            {"type": "text", "text": "b"}]}],
                        max_tokens=8)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"][0]["content"], "a\nb")
        self.assertEqual(payload["max_tokens"], 8)

    def test_missing_key_raises_before_network(self):
        with mock.patch.dict(os.environ, {"QUILL_PARENT_PROVIDER": "xai"}):
            os.environ.pop("XAI_API_KEY", None)
            with mock.patch("httpx.post") as post:
                with self.assertRaises(RuntimeError):
                    pm.complete(model="claude-opus-4-8", system="s",
                                messages=[{"role": "user", "content": "q"}],
                                max_tokens=8)
            post.assert_not_called()


class RouterDetourTests(unittest.TestCase):
    """The router uses its own Anthropic client normally, and detours to
    parent_model only when a foreign provider is active — after the privacy
    gate either way."""

    def test_anthropic_stays_on_router_client(self):
        from app.services.model_router import ModelRouter
        r = ModelRouter()
        block = mock.Mock(type="text", text="ok")
        client = mock.Mock()
        client.messages.create.return_value = mock.Mock(
            content=[block], usage=mock.Mock(input_tokens=1, output_tokens=1))
        r._client = client
        with mock.patch.dict(os.environ, {"QUILL_PARENT_PROVIDER": "anthropic"}):
            out = r._complete_claude("chat", system="s",
                                     messages=[{"role": "user", "content": "q"}])
        self.assertEqual(out, "ok")
        client.messages.create.assert_called_once()

    def test_foreign_provider_detours(self):
        from app.services.model_router import ModelRouter
        r = ModelRouter()
        client = mock.Mock()
        r._client = client
        fake = {"text": "grok says", "input_tokens": 1, "output_tokens": 1,
                "provider": "xai", "model": "grok-4"}
        with mock.patch.dict(os.environ, {"QUILL_PARENT_PROVIDER": "xai"}), \
             mock.patch("app.services.parent_model.complete",
                        return_value=fake) as pc:
            out = r._complete_claude("chat", system="s",
                                     messages=[{"role": "user", "content": "q"}])
        self.assertEqual(out, "grok says")
        pc.assert_called_once()
        client.messages.create.assert_not_called()


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
