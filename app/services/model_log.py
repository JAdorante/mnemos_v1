"""Model-call telemetry — the measurement layer under the ModelRouter.

Every model call (local or paid) funnels through `log_call`, which records task
type, provider/model, token or byte sizes, latency, estimated cost, and success.
Records append to a JSONL trail (data/model_calls.jsonl) and a rolling in-memory
aggregate the console reads via /console/models. This is what makes the local-vs-
Claude routing measurable — and the substrate the Gemini/local benchmarks (roadmap
steps 4-5) score against.

Cost is *estimated* from a per-model price table (USD per 1M tokens). Local models
(Ollama) are $0. Prices are point-in-time; update PRICES when they change.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.config import settings

# USD per 1M tokens, (input, output). Source: Claude pricing, 2026-07.
# Local/Ollama models are free — anything not listed is treated as $0.
# De-duplicated: numbers live in data/model_prices.json (shared with
# browser_agent/config.RATES) so the two can't drift; override the file with
# QUILL_MODEL_PRICES. The literal below is only a fail-safe fallback; a parity
# test pins the two tables together.
_PRICES_FALLBACK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),      # $2/$10 intro through 2026-08-31
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def _load_prices(fallback: dict) -> dict[str, tuple[float, float]]:
    """Load {model_id: (in, out)} from data/model_prices.json (or QUILL_MODEL_PRICES).
    Fails safe to `fallback` — telemetry pricing must never break a call."""
    import os
    raw = os.environ.get("QUILL_MODEL_PRICES")
    path = (Path(raw) if raw
            else Path(__file__).resolve().parent.parent.parent / "data" / "model_prices.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = {}
        for k, v in data.items():
            if k.startswith("_") or not isinstance(v, (list, tuple)) or len(v) != 2:
                continue
            out[k] = (float(v[0]), float(v[1]))
        return out or dict(fallback)
    except Exception:
        return dict(fallback)


PRICES: dict[str, tuple[float, float]] = _load_prices(_PRICES_FALLBACK)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = PRICES.get(model, (0.0, 0.0))
    return (input_tokens * inp + output_tokens * out) / 1_000_000


class ModelLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = Path(settings.storage.data_dir) / "model_calls.jsonl"
        # Aggregates keyed by (task, provider, model).
        self._agg: dict[tuple, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "errors": 0, "latency_s": 0.0, "cost_usd": 0.0,
                     "input_tokens": 0, "output_tokens": 0})

    def log_call(self, *, task: str, provider: str, model: str,
                 latency_s: float, ok: bool = True,
                 input_tokens: int = 0, output_tokens: int = 0,
                 input_bytes: int = 0, cost_usd: float | None = None,
                 meta: dict | None = None) -> dict:
        """Record one model call. Returns the row (also appended to the trail)."""
        if cost_usd is None:
            cost_usd = estimate_cost(model, input_tokens, output_tokens)
        # Spend metering (SECURITY #2): cloud spend on ambient tasks feeds the
        # USD/day ledger the enforcement seams (vlm / model_router) check
        # BEFORE calling. Recording here — the one place cost is computed —
        # means no cloud path can silently bypass the meter. Best-effort:
        # telemetry must never break the call it measures.
        if ok and cost_usd > 0 and provider not in ("ollama", "local", "none"):
            try:
                from app.perception.spend_cap import spend_cap
                if spend_cap.is_ambient(task):
                    spend_cap.record(cost_usd, task)
            except Exception:
                pass
        row = {
            # Wall-clock stamp — without it the trail can't answer "what
            # happened after X?" (rows predating 2026-07-17 lack it).
            "time": round(time.time(), 3),
            "task": task, "provider": provider, "model": model,
            "latency_s": round(latency_s, 3), "ok": ok,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "input_bytes": input_bytes, "cost_usd": round(cost_usd, 6),
        }
        if meta:
            row["meta"] = meta
        key = (task, provider, model)
        with self._lock:
            a = self._agg[key]
            a["calls"] += 1
            a["errors"] += 0 if ok else 1
            a["latency_s"] += latency_s
            a["cost_usd"] += cost_usd
            a["input_tokens"] += input_tokens
            a["output_tokens"] += output_tokens
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            except Exception as exc:   # telemetry must never break a call
                print(f"[model_log] write skipped ({exc}).")
        return row

    def stats(self) -> dict[str, Any]:
        """Aggregated view for the console: per (task, provider, model) rollups."""
        with self._lock:
            rows = []
            totals = {"calls": 0, "cost_usd": 0.0, "errors": 0}
            for (task, provider, model), a in sorted(self._agg.items()):
                calls = a["calls"] or 1
                rows.append({
                    "task": task, "provider": provider, "model": model,
                    "calls": a["calls"], "errors": a["errors"],
                    "avg_latency_s": round(a["latency_s"] / calls, 3),
                    "cost_usd": round(a["cost_usd"], 4),
                    "input_tokens": a["input_tokens"],
                    "output_tokens": a["output_tokens"],
                })
                totals["calls"] += a["calls"]
                totals["errors"] += a["errors"]
                totals["cost_usd"] += a["cost_usd"]
            totals["cost_usd"] = round(totals["cost_usd"], 4)
        return {"rows": rows, "totals": totals}


model_log = ModelLog()
