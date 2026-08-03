"""GeminiVLM — a Gemini Flash vision provider parallel to ClaudeVLM/OllamaVLM.

Same describe(jpeg)->dict interface and _SCHEMA as the other providers, so it
drops into the VLMRouter or the benchmark harness unchanged. Uses the Gemini REST
API (no extra SDK dep). Requires GOOGLE_API_KEY (or GEMINI_API_KEY); `available()`
is False without one, so Benchmark A skips it cleanly.

Model + pricing are env-configurable; cost logging uses the model_log price table
(add a Gemini entry there when running paid comparisons).
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from typing import Any

from app.services.vlm import _SCHEMA, _SYSTEM, _USER, _parse_json


class GeminiVLM:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("QUILL_GEMINI_MODEL", "gemini-2.0-flash")
        self.key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        self.timeout = float(os.environ.get("QUILL_GEMINI_TIMEOUT_S", "60"))

    def available(self) -> bool:
        return bool(self.key)

    def describe(self, jpeg_bytes: bytes) -> dict[str, Any]:
        b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")
        # Two credential shapes: an AI Studio API key (AIza…) goes in ?key=; an
        # OAuth / ephemeral token (AQ.…) goes in an Authorization: Bearer header.
        base = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent")
        headers = {"Content-Type": "application/json"}
        if self.key and self.key.startswith("AIza"):
            url = f"{base}?key={self.key}"
        else:
            url = base
            headers["Authorization"] = f"Bearer {self.key}"
        payload = {
            "systemInstruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                {"text": _USER},
            ]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": _to_gemini_schema(_SCHEMA),
            },
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers)
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            out = json.load(r)
        latency = time.time() - t0
        usage = out.get("usageMetadata", {})
        try:
            from app.services.model_log import model_log
            model_log.log_call(task="bench_vision", provider="gemini",
                               model=self.model, latency_s=latency, ok=True,
                               input_tokens=usage.get("promptTokenCount", 0) or 0,
                               output_tokens=usage.get("candidatesTokenCount", 0) or 0,
                               input_bytes=len(jpeg_bytes))
        except Exception:
            pass
        cands = out.get("candidates") or [{}]
        parts = (cands[0].get("content") or {}).get("parts") or [{}]
        text = parts[0].get("text", "{}")
        return _parse_json(text)


def _to_gemini_schema(schema: dict) -> dict:
    """Gemini's responseSchema rejects a couple of JSON-Schema keys Anthropic
    accepts (additionalProperties, number-vs-integer nuances). Strip them."""
    def clean(node):
        if isinstance(node, dict):
            return {k: clean(v) for k, v in node.items()
                    if k not in ("additionalProperties",)}
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node
    return clean(schema)
