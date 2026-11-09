"""The Claude side of the Sparrow 1.0 loop: one structured-output call shape,
spend accounting, and nothing else.

Every call is JSON-schema constrained (output_config.format), streamed (world
generation runs long), and opted into the server-side refusal fallback. Usage
is appended to data/sparrow1/teacher_calls.jsonl so a round's cost is a
number, not a guess.

Inputs here are synthetic worlds and the local model's replies to them —
no user data reaches this module.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOG = ROOT / "data" / "sparrow1" / "teacher_calls.jsonl"

MODEL = os.environ.get("SPARROW1_TEACHER_MODEL", "claude-opus-5-5")

# $/MTok (input, output) for the spend estimate; thinking bills as output.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

_client = None
_lock = threading.Lock()


class TeacherError(RuntimeError):
    pass


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(max_retries=4)
    return _client


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (input_tokens * pin + output_tokens * pout) / 1e6


def _log(row: dict) -> None:
    with _lock:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


def ask_json(system: str, user: str, schema: dict, *, effort: str = "medium",
             max_tokens: int = 16000, label: str = "",
             model: str | None = None) -> dict:
    """One schema-constrained Claude call -> the parsed object. Raises
    TeacherError on a refusal, a truncated reply, or unparseable output."""
    model = model or MODEL
    t0 = time.time()
    with _get_client().beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort,
                       "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        msg = stream.get_final_message()
    usage = msg.usage
    row = {"time": time.time(), "label": label, "model": msg.model,
           "stop": msg.stop_reason, "secs": round(time.time() - t0, 1),
           "input_tokens": usage.input_tokens,
           "output_tokens": usage.output_tokens,
           "usd": round(cost_usd(model, usage.input_tokens,
                                 usage.output_tokens), 5)}
    _log(row)
    if msg.stop_reason == "refusal":
        raise TeacherError(f"{label}: refused ({getattr(msg, 'stop_details', None)})")
    if msg.stop_reason == "max_tokens":
        raise TeacherError(f"{label}: hit max_tokens={max_tokens}")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TeacherError(f"{label}: unparseable output ({exc})") from exc


def spend(since: float = 0.0) -> dict:
    """Calls and estimated USD logged at or after `since`."""
    n, usd = 0, 0.0
    if LOG.is_file():
        for ln in LOG.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("time", 0) >= since:
                n += 1
                usd += float(r.get("usd") or 0)
    return {"calls": n, "usd": round(usd, 2)}
