"""Sparrow 1.0 capture hook (app/services/model_capture.py).

Pins the contract sparrow-train depends on: OFF unless SPARROW_CAPTURE_PATH is
set; when on, exactly one full record per /api/chat round trip (the local
retry is its own record), nothing clipped, failures recorded, labels scoped
to the driver's context block, and a prompt_version that names the template
rather than the call.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import model_capture as mc
from app.services.ollama_text import OllamaText


class _FakeOllama:
    """urlopen stand-in: /api/chat returns `replies` in order, /api/tags a
    one-model listing. Records every chat payload."""

    def __init__(self, replies, *, model="m:latest", digest="sha256:abc"):
        self.replies = list(replies)
        self.chats: list[dict] = []
        self.model, self.digest = model, digest

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/api/tags"):
            body = {"models": [{"name": self.model, "digest": self.digest}]}
        else:
            self.chats.append(json.loads(req.data.decode("utf-8")))
            body = self.replies.pop(0)
            if isinstance(body, BaseException):
                raise body
        raw = json.dumps(body).encode()

        class R:
            def read(self, *a):
                return raw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()


def _reply(text, **kw):
    return {"message": {"content": text}, "prompt_eval_count": 11,
            "eval_count": 7, "done_reason": "stop", **kw}


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "cap" / "calls.jsonl"
        mc._digests.clear()

    def tearDown(self) -> None:
        self.dir.cleanup()

    def _run(self, replies, *, env=True, **kw):
        fake = _FakeOllama(replies)
        env_patch = {mc.ENV: str(self.path)} if env else {}
        with patch.dict(os.environ, env_patch, clear=False), \
                patch("urllib.request.urlopen", fake):
            if not env:
                os.environ.pop(mc.ENV, None)
            client = OllamaText(model="m:latest")
            out = client.complete(kw.pop("task", "chat"),
                                  system=kw.pop("system", "SYS"),
                                  messages=kw.pop("messages", [
                                      {"role": "user", "content": "hi"}]),
                                  **kw)
        return out, fake

    def _records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x]

    # -- off by default ------------------------------------------------------
    def test_off_writes_nothing_and_sends_the_same_payload(self) -> None:
        _, off = self._run([_reply("ok\nCONFIDENCE: 0.9")], env=False)
        self.assertFalse(self.path.exists())
        _, on = self._run([_reply("ok\nCONFIDENCE: 0.9")])
        self.assertEqual(off.chats, on.chats)

    def test_helpers_are_noops_when_off(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(mc.ENV, None)
            self.assertIsNone(mc.begin(url="u", task="t", payload={}))
            with mc.context(world_id="w"):
                mc.note_retrieved(facts=[1])
            self.assertIsNone(mc._ctx.get())

    # -- one full record per round trip --------------------------------------
    def test_one_record_with_every_field(self) -> None:
        self._run([_reply("answer\nCONFIDENCE: 0.8")], task="chat",
                  system="SYS PROMPT")
        recs = self._records()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        for field in ("call_id", "task", "prompt_version", "messages",
                      "response", "model", "world_id", "item_id",
                      "retrieved_ids", "latency_ms", "tokens_in",
                      "tokens_out"):
            self.assertIn(field, r)
        self.assertEqual(r["task"], "chat")
        self.assertEqual(r["response"], "answer\nCONFIDENCE: 0.8")
        self.assertEqual(r["model"], {"tag": "m:latest",
                                      "digest": "sha256:abc"})
        self.assertEqual((r["tokens_in"], r["tokens_out"]), (11, 7))
        self.assertEqual(r["attempt"], 1)
        self.assertTrue(r["messages"][0]["content"].startswith("SYS PROMPT"))

    def test_nothing_is_clipped(self) -> None:
        long_in = "x" * 200_000
        long_out = "y" * 150_000 + "\nCONFIDENCE: 0.9"
        self._run([_reply(long_out)],
                  messages=[{"role": "user", "content": long_in}])
        r = self._records()[0]
        self.assertEqual(r["messages"][1]["content"], long_in)
        self.assertEqual(r["response"], long_out)

    def test_the_local_retry_is_its_own_record(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}},
                  "required": ["a"]}
        out, fake = self._run([_reply("not json"),
                               _reply('{"a": "x", "confidence": 0.9}')],
                              task="extract", schema=schema)
        self.assertEqual(out["json"], {"a": "x"})
        recs = self._records()
        self.assertEqual(len(recs), len(fake.chats))
        self.assertEqual([r["attempt"] for r in recs], [1, 2])
        self.assertEqual(recs[0]["prompt_version"], recs[1]["prompt_version"])
        self.assertIsNotNone(recs[0]["format"])
        self.assertEqual(len({r["call_id"] for r in recs}), 2)

    def test_a_failed_round_trip_is_recorded_and_still_raises(self) -> None:
        with self.assertRaises(TimeoutError):
            self._run([TimeoutError("slow")])
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertIsNone(recs[0]["response"])
        self.assertIn("TimeoutError", recs[0]["error"])

    # -- labels ----------------------------------------------------------------
    def test_context_labels_and_scoped_retrieved_ids(self) -> None:
        fake = _FakeOllama([_reply("a\nCONFIDENCE: 0.9"),
                            _reply("b\nCONFIDENCE: 0.9")])
        with patch.dict(os.environ, {mc.ENV: str(self.path)}), \
                patch("urllib.request.urlopen", fake):
            mc.note_retrieved(facts=[99])           # no block: ignored
            with mc.context(world_id="w1", phase="scenario"):
                with mc.context(item_id="s01"):
                    mc.note_retrieved(facts=[3, 1], events=["7"])
                    mc.note_retrieved(facts=[3, 2])
                    OllamaText(model="m:latest").complete(
                        "chat", system="S", messages=[{"role": "user",
                                                      "content": "q"}])
                with mc.context(item_id="s02"):
                    OllamaText(model="m:latest").complete(
                        "chat", system="S", messages=[{"role": "user",
                                                      "content": "q"}])
        a, b = self._records()
        self.assertEqual((a["world_id"], a["item_id"], a["phase"]),
                         ("w1", "s01", "scenario"))
        self.assertEqual(a["retrieved_ids"], {"facts": [1, 2, 3],
                                              "events": [7]})
        self.assertEqual(b["item_id"], "s02")
        self.assertIsNone(b["retrieved_ids"])

    # -- prompt_version ----------------------------------------------------------
    def test_prompt_version_ignores_the_clock_line_only(self) -> None:
        a = "Rules.\nRIGHT NOW (user's local time): Sunday, November 8 2026, 11:09 PM\nMore."
        b = "Rules.\nRIGHT NOW (user's local time): Monday, October 19 2026, 10:31 AM\nMore."
        c = "Rules changed.\nRIGHT NOW (user's local time): Sunday, November 8 2026, 11:09 PM\nMore."
        v = mc.prompt_version
        self.assertEqual(v("extract", a, None, "text"),
                         v("extract", b, None, "text"))
        self.assertNotEqual(v("extract", a, None, "text"),
                            v("extract", c, None, "text"))
        self.assertNotEqual(v("extract", a, None, "text"),
                            v("chat", a, None, "text"))
        self.assertNotEqual(v("extract", a, {"type": "object"}, "json"),
                            v("extract", a, None, "text"))

    def test_exemplars_do_not_change_prompt_version(self) -> None:
        self._run([_reply("a\nCONFIDENCE: 0.9")])
        self._run([_reply("a\nCONFIDENCE: 0.9")], exemplars="\n\nEX ONE")
        a, b = self._records()
        self.assertEqual(a["prompt_version"], b["prompt_version"])
        self.assertFalse(a["had_exemplars"])
        self.assertTrue(b["had_exemplars"])


if __name__ == "__main__":
    unittest.main()
