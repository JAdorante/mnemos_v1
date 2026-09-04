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


def _spans(payload: dict) -> None:
    """Fold Ollama's own timings into the active latency trace. Same contract
    as `_log`: telemetry never breaks the text path."""
    try:
        from app.services import latency
        latency.record_ollama_timings(payload)
    except Exception as exc:  # pragma: no cover
        print(f"[ollama_text] spans skipped ({exc}).")


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

# Reasoning-model protocol. Qwen3-family tags (and other 2026-class thinking
# models) prepend a `<think>...</think>` block to the reply unless the
# non-thinking variant is pulled. Left in place it lands BEFORE the
# `CONFIDENCE:` trailer parse, so the block becomes part of the answer: the
# distill row stores reasoning as its target, and embedding similarity scores
# the monologue instead of the reply. Stripping here — not at the call sites —
# means pulling a thinking-default tag can never silently poison the trail.
_THINK_PAIR_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>\s*", re.S | re.I)
_THINK_HEAD_RE = re.compile(r"\A.*?</think(?:ing)?>\s*", re.S | re.I)
_THINK_TAIL_RE = re.compile(r"<think(?:ing)?>.*\Z", re.S | re.I)


def strip_reasoning(text: str) -> str:
    """Remove a model's `<think>` monologue, leaving only the reply.

    Three shapes, in order: a well-formed block anywhere; a stray closing tag
    with no opener (the model was prefilled mid-thought); and an UNTERMINATED
    opener, which means generation hit `num_predict` before the model finished
    thinking. That last case deliberately empties the text — a truncated
    monologue is not an answer, and an empty reply with no CONFIDENCE trailer
    reads as confidence None, i.e. escalate, which is the right outcome.
    """
    t = _THINK_PAIR_RE.sub("", text or "")
    if "</think" in t.lower():
        t = _THINK_HEAD_RE.sub("", t)
    if "<think" in t.lower():
        t = _THINK_TAIL_RE.sub("", t)
    return t.strip()


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


def training_contract(system: str, target: str,
                      conf: float = 0.9) -> tuple[str, str]:
    """The (system, target) a fine-tune pair must carry to match this module's
    INFERENCE contract for plain-text tasks: the system prompt ends with the
    confidence-trailer instruction, and the answer ends with a parseable
    `CONFIDENCE: 0.NN` line. Pairs trained WITHOUT this taught the adapter to
    omit the trailer even when instructed — every answer then parsed as
    confidence None ("unsure") and auto-escalated, which is exactly the
    regression the Aug 18 bench caught. Verified gold targets default to 0.9:
    the trailer's job here is preserving the FORMAT; calibration stays with
    the router's effective-confidence layer. Idempotent on both halves."""
    system = (system or "").rstrip()
    if system and _CONF_TRAILER.strip() not in system:
        system = system + _CONF_TRAILER
    target = (target or "").rstrip()
    if target and not _CONF_RE.search(target):
        target = f"{target}\n\nCONFIDENCE: {max(0.0, min(1.0, conf)):.2f}"
    return system, target


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


def _compose_system(system: str, exemplars: str = "", *,
                    schema: dict | None = None, injected: bool = False) -> str:
    """Build the system prompt STATIC-FIRST, for prefix-cache hits (Phase 1.2).

    Ollama caches the longest common prefix of consecutive prompts, so every
    byte that varies per call invalidates everything after it. The old order
    was `system + exemplars + trailer`, which put a constant trailer *behind*
    the one part that changes every call — the worst possible arrangement:
    the trailer could never be cached, and the exemplars broke the prefix
    immediately after the system prompt.

    Correct order, cheapest-to-vary last::

        [system]  [confidence trailer]  [retrieval exemplars]  → messages

    `system` and the trailer are fixed per task, so a run of calls on the same
    task shares that whole prefix regardless of which exemplars were recalled.
    This changes prompt ORDER only — the same bytes reach the model.
    """
    trailer = ""
    if schema is None:
        trailer = _CONF_TRAILER
    elif injected:
        trailer = _JSON_CONF_TRAILER
    return f"{system}{trailer}{exemplars or ''}"


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

    def warmup(self) -> bool:
        """Load the model now so the first user interaction is a warm call.

        One-token generation against the real model — the cheapest request that
        forces a load. Best-effort and silent on failure: a machine with no
        Ollama must boot exactly as it does today.
        """
        try:
            payload = {
                "model": self.model, "stream": False,
                "keep_alive": settings.text_local.keep_alive,
                "options": {"temperature": 0, "num_predict": 1},
                "messages": [{"role": "user", "content": "ok"}],
            }
            req = urllib.request.Request(
                self.url + "/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.load(r)
            load_ms = float(out.get("load_duration") or 0) / 1e6
            print(f"[ollama_text] warm ({self.model}, load {load_ms:.0f} ms, "
                  f"keep_alive={settings.text_local.keep_alive}).")
            _log("warmup", "ollama", self.model, time.time() - t0, ok=True,
                 cost_usd=0.0)
            return True
        except Exception as exc:
            print(f"[ollama_text] warmup skipped ({exc}).")
            return False

    def _chat(self, task: str, msgs: list[dict], *, num_predict: int,
              fmt: dict | None) -> dict[str, Any]:
        """One raw /api/chat round trip, logged. Raises on transport errors."""
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0, "num_predict": num_predict},
            # Phase 1.1 — keep the weights resident between calls. Ollama
            # unloads after ~5 min idle by default, so without this the first
            # call after a quiet spell pays a full cold load.
            "keep_alive": settings.text_local.keep_alive,
            "messages": msgs,
        }
        if fmt is not None:
            payload["format"] = fmt                  # Ollama structured output
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url + "/api/chat", data=data,
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            out = json.load(r)
        # Stage breakdown at zero cost: Ollama returns its own nanosecond
        # timings (load / prompt_eval / eval) for work it already did, so
        # cold-load, prefill and generation need no extra probe on this path.
        _spans(out)
        # Local model — free; still logged for latency + token throughput.
        _log(task, "ollama", self.model, time.time() - t0, ok=True,
             input_tokens=out.get("prompt_eval_count", 0) or 0,
             output_tokens=out.get("eval_count", 0) or 0, cost_usd=0.0)
        return out

    def complete(self, task: str, *, system: str, messages: list,
                 max_tokens: int = 1024, schema: dict | None = None,
                 exemplars: str = "") -> dict[str, Any]:
        """One local completion (with at most one free local retry). Returns
        {"text": str, "json": dict|None, "confidence": float|None,
         "parse_ok": bool, "truncated": bool}
        — `json`/`parse_ok` only meaningful when a schema was given. Raises on
        transport errors (the router treats that as a local_error escalate).

        The retry is the cheapest possible rescue before a paid escalation:
        a generation that hit `num_predict` (Ollama done_reason "length" — the
        thinking monologue or the JSON ran past the budget) is re-run once
        with double the budget; a non-truncated reply that failed to parse is
        re-asked once with the bad reply and a correction appended (at
        temperature 0 an unchanged prompt would just reproduce the failure).
        `truncated` reports the FINAL attempt so the router can label the
        escalation honestly (local_truncated, not low_confidence).

        `exemplars` is the retrieval few-shot block, passed separately rather
        than pre-concatenated onto `system` so THIS function owns the ordering
        (see _compose_system): the static prefix has to come first or the
        prefix cache never hits.
        """
        injected = False
        fmt = None
        if schema is not None:
            fmt, injected = with_confidence(schema)
        sys_prompt = _compose_system(system, exemplars,
                                     schema=schema, injected=injected)
        msgs = (
            [{"role": "system", "content": sys_prompt}]
            + [{"role": m.get("role", "user"), "content": _flatten(m.get("content"))}
               for m in messages]
        )
        num_predict = max_tokens
        retried = False
        while True:
            out = self._chat(task, msgs, num_predict=num_predict, fmt=fmt)
            raw = ((out.get("message") or {}).get("content") or "")
            text = strip_reasoning(raw)
            truncated = str(out.get("done_reason") or "") == "length"

            if schema is None:
                clean, conf = split_confidence(text)
                if truncated and conf is None and not retried:
                    # The budget ran out before the reply (or its CONFIDENCE
                    # trailer) finished — worth one wider local attempt.
                    num_predict = max_tokens * 2
                    retried = True
                    continue
                return {"text": clean, "json": None, "confidence": conf,
                        "parse_ok": True, "truncated": truncated}

            parsed = _parse_json(text)
            if parsed is None and not retried:
                retried = True
                if truncated:
                    num_predict = max_tokens * 2
                else:
                    msgs = msgs + [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content":
                         "That reply was not valid JSON for the required "
                         "schema. Answer again with ONLY the JSON object — "
                         "no prose, no code fences."}]
                continue
            if parsed is None:
                return {"text": text, "json": None, "confidence": 0.0,
                        "parse_ok": False, "truncated": truncated}
            conf = parsed.get("confidence")
            conf = float(conf) if isinstance(conf, (int, float)) else None
            if injected:
                parsed.pop("confidence", None)
            return {"text": text, "json": parsed, "confidence": conf,
                    "parse_ok": True, "truncated": truncated}
