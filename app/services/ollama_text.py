"""Local text-model client — the free first pass for router-served text calls.

Mirror of `OllamaVLM` (vlm.py) for TEXT: a small instruct model served by Ollama
answers routine completions (chat / extract / summarize) on the GPU for free;
Claude stays the *paid parent*, invoked by the ModelRouter's escalate policy
when this model errors, its output doesn't parse, it self-reports low
confidence, or the task is high-stakes. Enable with QUILL_TEXT_LOCAL=1.

Two output modes:
  * plain text — the model is asked to end with one `CONFIDENCE: 0.NN` line,
    which is parsed off and returned separately so the router can gate on it.
    A missing line reads as "unsure" (confidence None -> escalate).
  * structured — the caller's JSON Schema is enforced via Ollama's `format`;
    when the schema has no `confidence` field one is injected (and stripped
    from the result afterwards). A parse failure surfaces as parse_ok=False /
    confidence 0.0 — an escalate trigger, same as vlm._parse_json.

Out of scope on purpose: the browser agent's executor escalation (Sonnet ->
Opus on a stalled step, see browser_agent/config.py) is a separate,
Claude-internal ladder and does NOT route through this client.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any

from app.config import settings


def _log(task, provider, model, latency_s, **kw) -> None:
    """Record a model call; never let telemetry break the text path."""
    try:
        from app.services.model_log import model_log
        model_log.log_call(task=task, provider=provider, model=model,
                           latency_s=latency_s, **kw)
    except Exception as exc:  # pragma: no cover
        print(f"[ollama_text] telemetry skipped ({exc}).")


_CONF_TRAILER = (
    "\n\nAfter your reply, end with ONE final line of exactly the form "
    "'CONFIDENCE: 0.NN' — your 0.0-1.0 confidence that the reply is correct "
    "and complete. Use LOW values when unsure. Nothing after that line."
)

_CONF_RE = re.compile(r"\n?\s*CONFIDENCE:\s*([01](?:\.\d+)?)\s*$", re.I)

# Ollama's `format` enforces the schema as a grammar but the model never SEES
# the property descriptions — an injected `confidence` field must be explained
# in the prompt or small models emit 0 for it and force a needless escalation.
_JSON_CONF_TRAILER = (
    "\n\nAlso set the `confidence` field: your 0.0-1.0 confidence that the rest "
    "of the output is correct and complete. A clean, unambiguous input is ~0.9; "
    "use values below 0.6 only when genuinely unsure."
)


def split_confidence(text: str) -> tuple[str, float | None]:
    """Strip the trailing `CONFIDENCE: 0.NN` line; return (clean_text, conf).

    conf is None when the model ignored the instruction — the router treats
    that as "unsure" rather than trusting an unlabeled answer."""
    t = (text or "").rstrip()
    m = _CONF_RE.search(t)
    if not m:
        return t, None
    conf = max(0.0, min(1.0, float(m.group(1))))
    return t[:m.start()].rstrip(), conf


def with_confidence(schema: dict) -> tuple[dict, bool]:
    """Return (schema', injected): schema' always carries a `confidence`
    property. injected=True means the caller's schema lacked one, so the field
    must be stripped from the parsed result before handing it back."""
    props = schema.get("properties") or {}
    if "confidence" in props:
        return schema, False
    s = json.loads(json.dumps(schema))          # deep copy — never mutate the caller's
    s.setdefault("properties", {})["confidence"] = {
        "type": "number",
        "description": "0.0-1.0: how sure you are this output is correct and "
                       "complete. Use LOW values (< 0.6) when unsure.",
    }
    req = s.get("required")
    if isinstance(req, list) and "confidence" not in req:
        req.append("confidence")
    return s, True


def _parse_json(text: str) -> dict[str, Any] | None:
    """Best-effort parse of a model's JSON reply (tolerates ```json fences).
    Returns None when unparseable — the router's parse-failure escalate trigger."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    try:
        out = json.loads(t)
        return out if isinstance(out, dict) else None
    except Exception:
        start, end = t.find("{"), t.rfind("}")
        if 0 <= start < end:
            try:
                out = json.loads(t[start:end + 1])
                return out if isinstance(out, dict) else None
            except Exception:
                pass
    return None


def _flatten(content) -> str:
    """Anthropic-style message content may be a list of blocks; Ollama wants a string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return str(content)


class OllamaText:
    """The free local first-pass — a text model served by Ollama."""

    def __init__(self, model: str | None = None) -> None:
        self.url = settings.text_local.ollama_url.rstrip("/")
        self.model = model or settings.text_local.local_model
        self.timeout = settings.text_local.local_timeout_s

    def available(self) -> bool:
        """True if Ollama is up and the configured model is present."""
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=3) as r:
                tags = json.load(r)
        except Exception:
            return False
        names = {m.get("name", "") for m in tags.get("models", [])}
        base = self.model.split(":")[0]
        return any(n == self.model or n.split(":")[0] == base for n in names)

    def complete(self, task: str, *, system: str, messages: list,
                 max_tokens: int = 1024, schema: dict | None = None) -> dict[str, Any]:
        """One local completion. Returns
        {"text": str, "json": dict|None, "confidence": float|None, "parse_ok": bool}
        — `json`/`parse_ok` only meaningful when a schema was given. Raises on
        transport errors (the router treats that as a local_error escalate)."""
        injected = False
        sys_prompt = system
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        if schema is not None:
            fmt, injected = with_confidence(schema)
            payload["format"] = fmt                  # Ollama structured output
            if injected:
                sys_prompt = system + _JSON_CONF_TRAILER
        else:
            sys_prompt = system + _CONF_TRAILER
        payload["messages"] = (
            [{"role": "system", "content": sys_prompt}]
            + [{"role": m.get("role", "user"), "content": _flatten(m.get("content"))}
               for m in messages]
        )
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url + "/api/chat", data=data,
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            out = json.load(r)
        # Local model — free; still logged for latency + token throughput.
        _log(task, "ollama", self.model, time.time() - t0, ok=True,
             input_tokens=out.get("prompt_eval_count", 0) or 0,
             output_tokens=out.get("eval_count", 0) or 0, cost_usd=0.0)
        text = ((out.get("message") or {}).get("content") or "").strip()

        if schema is None:
            clean, conf = split_confidence(text)
            return {"text": clean, "json": None, "confidence": conf, "parse_ok": True}

        parsed = _parse_json(text)
        if parsed is None:
            return {"text": text, "json": None, "confidence": 0.0, "parse_ok": False}
        conf = parsed.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else None
        if injected:
            parsed.pop("confidence", None)
        return {"text": text, "json": parsed, "confidence": conf, "parse_ok": True}
