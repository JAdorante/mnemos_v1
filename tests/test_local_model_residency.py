"""Latency program, Phase 1.1 + 1.2 — residency and prompt-prefix ordering.

Both are mechanical changes whose whole value is measurable, so the tests pin
the mechanism rather than a timing: `keep_alive` is actually sent, and the
static part of the prompt actually precedes the part that varies per call.

Measured on the reference machine (qwen2.5:7b-instruct):
  cold load 3,571 ms vs warm 163 ms   -> what keep_alive removes
  prefill 77 ms first vs 28 ms repeat -> what the prefix ordering buys
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.config import settings
from app.services import ollama_text
from app.services.ollama_text import (
    OllamaText, _CONF_TRAILER, _JSON_CONF_TRAILER, _compose_system,
)


class PrefixOrderTests(unittest.TestCase):
    """Static prefix first, per-call content last — or the cache never hits."""

    def test_the_confidence_trailer_precedes_the_exemplars(self) -> None:
        out = _compose_system("SYSTEM", "\n\nEXEMPLARS", schema=None)
        self.assertLess(out.index(_CONF_TRAILER), out.index("EXEMPLARS"))
        self.assertLess(out.index("SYSTEM"), out.index(_CONF_TRAILER))

    def test_the_json_trailer_precedes_the_exemplars(self) -> None:
        out = _compose_system("SYSTEM", "\n\nEXEMPLARS",
                              schema={"type": "object"}, injected=True)
        self.assertLess(out.index(_JSON_CONF_TRAILER), out.index("EXEMPLARS"))

    def test_no_trailer_is_added_when_the_schema_already_has_confidence(self) -> None:
        out = _compose_system("SYSTEM", "\n\nEX", schema={"type": "object"},
                              injected=False)
        self.assertEqual(out, "SYSTEM\n\nEX")

    def test_the_prefix_is_identical_across_different_exemplars(self) -> None:
        """This is the property the cache keys on: two calls on the same task
        must share every byte up to where the exemplars start."""
        a = _compose_system("SYSTEM", "\n\nEXEMPLAR A", schema=None)
        b = _compose_system("SYSTEM", "\n\nTOTALLY DIFFERENT B", schema=None)
        shared = len(_CONF_TRAILER) + len("SYSTEM")
        self.assertEqual(a[:shared], b[:shared])
        self.assertNotEqual(a, b)

    def test_no_exemplars_yields_the_bare_static_prefix(self) -> None:
        self.assertEqual(_compose_system("SYSTEM", "", schema=None),
                         "SYSTEM" + _CONF_TRAILER)
        self.assertEqual(_compose_system("SYSTEM", None, schema=None),
                         "SYSTEM" + _CONF_TRAILER)

    def test_the_same_bytes_still_reach_the_model(self) -> None:
        """Ordering change only — nothing added, nothing dropped."""
        out = _compose_system("SYSTEM", "\n\nEXEMPLARS", schema=None)
        self.assertEqual(sorted(out), sorted("SYSTEM" + _CONF_TRAILER
                                             + "\n\nEXEMPLARS"))


class _Capture:
    """Stand-in for urlopen that records the request body."""

    def __init__(self, reply: dict | None = None):
        self.payloads: list[dict] = []
        self.reply = reply or {"message": {"content": "ok\nCONFIDENCE: 0.9"},
                               "prompt_eval_count": 10, "eval_count": 5}

    def __call__(self, req, timeout=None):
        self.payloads.append(json.loads(req.data.decode("utf-8")))
        outer = self

        class R:
            def read(self, *a):
                return json.dumps(outer.reply).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return R()


class KeepAliveTests(unittest.TestCase):
    def _run(self, **kw):
        cap = _Capture()
        with patch("urllib.request.urlopen", cap), \
                patch("json.load", lambda r: cap.reply):
            OllamaText().complete("chat", system="S",
                                  messages=[{"role": "user", "content": "hi"}],
                                  **kw)
        return cap.payloads[0]

    def test_keep_alive_is_sent_on_every_completion(self) -> None:
        """Without it Ollama unloads after ~5 min idle and the next call pays
        a full cold load (measured: 3.4 s on the reference machine)."""
        self.assertEqual(self._run()["keep_alive"],
                         settings.text_local.keep_alive)

    def test_the_duration_is_configurable(self) -> None:
        # Settings is a frozen dataclass, so swap the whole object the module
        # holds rather than assigning a field on it.
        import dataclasses
        patched = dataclasses.replace(
            settings,
            text_local=dataclasses.replace(settings.text_local,
                                           keep_alive="90m"))
        with patch.object(ollama_text, "settings", patched):
            self.assertEqual(self._run()["keep_alive"], "90m")

    def test_the_system_prompt_goes_out_static_first(self) -> None:
        cap = _Capture()
        with patch("urllib.request.urlopen", cap), \
                patch("json.load", lambda r: cap.reply):
            OllamaText().complete("chat", system="SYSTEM",
                                  messages=[{"role": "user", "content": "hi"}],
                                  exemplars="\n\nEXEMPLARS")
        sent = cap.payloads[0]["messages"][0]["content"]
        self.assertLess(sent.index(_CONF_TRAILER), sent.index("EXEMPLARS"))

    def test_a_schema_call_still_carries_the_format_grammar(self) -> None:
        """The reordering must not disturb structured output."""
        payload = self._run(schema={"type": "object", "properties": {}})
        self.assertIn("format", payload)
        self.assertIn("confidence", payload["format"]["properties"])


class WarmupTests(unittest.TestCase):
    def test_warmup_issues_one_tiny_request_with_keep_alive(self) -> None:
        cap = _Capture({"message": {"content": "ok"}, "load_duration": 3.5e9})
        with patch("urllib.request.urlopen", cap), \
                patch("json.load", lambda r: cap.reply):
            self.assertTrue(OllamaText().warmup())
        p = cap.payloads[0]
        self.assertEqual(p["options"]["num_predict"], 1)
        self.assertEqual(p["keep_alive"], settings.text_local.keep_alive)

    def test_warmup_is_silent_when_ollama_is_absent(self) -> None:
        """A machine with no Ollama must boot exactly as it does today."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            self.assertFalse(OllamaText().warmup())

    def test_warmup_is_off_by_default(self) -> None:
        self.assertFalse(settings.text_local.warmup)


class RouterWiringTests(unittest.TestCase):
    def test_the_router_passes_exemplars_separately(self) -> None:
        """Pre-concatenating onto `system` is what broke the ordering; the
        router must hand the block over and let ollama_text place it."""
        from app.services.model_router import router
        seen = {}

        class Local:
            def complete(self, task, *, system, messages, max_tokens=1024,
                         schema=None, exemplars=""):
                seen["system"] = system
                seen["exemplars"] = exemplars
                return {"text": "ok", "json": None, "confidence": 0.99,
                        "parse_ok": True}

        with patch.object(router, "_use_local", return_value=True), \
                patch.object(router, "_ensure_local", return_value=Local()):
            router.complete("chat", system="CLEAN SYSTEM",
                            messages=[{"role": "user", "content": "hi"}],
                            speculative=True)
        # The system prompt handed down stays the clean task prompt.
        self.assertEqual(seen["system"], "CLEAN SYSTEM")
        self.assertIn("exemplars", seen)


if __name__ == "__main__":
    unittest.main()
