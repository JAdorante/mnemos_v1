"""Model-call telemetry — the measurement layer under the ModelRouter.

Every model call (local or paid) funnels through `log_call`, which records task
type, provider/model, token or byte sizes, latency, estimated cost, success,
and (plan 6.2) `privacy_max` — the highest privacy_class in the payload that
left (or was refused) for an external call. Records append to a JSONL trail
(data/model_calls.jsonl); `/console/models` + the Egress console tab read the
aggregates / inventory.

Cost is *estimated* from a per-model price table (USD per 1M tokens). Local models
(Ollama) are $0. Prices are point-in-time; update PRICES when they change.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

from app.config import settings

# Providers whose calls leave the machine (egress inventory).
_CLOUD_PROVIDERS = frozenset({
    "claude", "anthropic", "gemini", "openai", "google", "azure",
})

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


def _normalize_privacy(cls: str | None) -> str | None:
    if not cls:
        return None
    try:
        from app.services.privacy_class import normalize
        return normalize(cls)
    except Exception:
        c = str(cls).strip().lower().replace("_", "-")
        return c or None


def _privacy_rank(cls: str | None) -> int:
    order = ("public", "internal", "personal", "sensitive", "never-send")
    try:
        return order.index(_normalize_privacy(cls) or "internal")
    except ValueError:
        return 1


def _is_cloud(provider: str) -> bool:
    return (provider or "").strip().lower() in _CLOUD_PROVIDERS


# --- speculative scope (latency program, Phase 3.3) -----------------------
# Speculative work — pre-generating answers to questions the user has not
# asked — is allowed to burn local inference, because that is electricity on
# hardware the user owns. It must NEVER reach a paid provider: a wasted Claude
# call spends real money on a guess, which breaks the cost story the whole
# program is bound by.
#
# The flag lives here rather than in model_router because this is where every
# call is stamped, so ANY cloud call made anywhere under the scope is caught —
# not only the ones the router knows about.
_spec_tls = threading.local()


def in_speculative_scope() -> bool:
    return bool(getattr(_spec_tls, "on", False))


class SpeculativeCloudCall(RuntimeError):
    """A paid call was attempted inside a speculative scope. Always a bug."""


@contextmanager
def speculative_scope() -> "Iterator[None]":
    """Mark this thread's calls as speculative for their duration."""
    prev = getattr(_spec_tls, "on", False)
    _spec_tls.on = True
    try:
        yield
    finally:
        _spec_tls.on = prev


class ModelLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = Path(settings.storage.data_dir) / "model_calls.jsonl"
        # Aggregates keyed by (task, provider, model).
        self._agg: dict[tuple, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "errors": 0, "latency_s": 0.0, "cost_usd": 0.0,
                     "input_tokens": 0, "output_tokens": 0})
        # Plan 6.2: session rollup of highest class that left (or was refused).
        self._privacy_by_class: dict[str, int] = defaultdict(int)
        self._privacy_max_seen: str | None = None
        self._privacy_cloud_calls: int = 0
        self._privacy_refused: int = 0

    def log_call(self, *, task: str, provider: str, model: str,
                 latency_s: float, ok: bool = True,
                 input_tokens: int = 0, output_tokens: int = 0,
                 input_bytes: int = 0, cost_usd: float | None = None,
                 privacy_max: str | None = None,
                 meta: dict | None = None) -> dict:
        """Record one model call. Returns the row (also appended to the trail).

        `privacy_max` (plan 6.2): highest privacy_class in the outbound payload
        for this call. Also accepted via meta.privacy_class / meta.privacy_max
        for back-compat with the 6.1 router wiring.
        """
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
        # Resolve privacy_max (explicit kwarg wins, then meta).
        pmax = _normalize_privacy(privacy_max)
        if pmax is None and meta:
            pmax = _normalize_privacy(
                meta.get("privacy_max") or meta.get("privacy_class"))
        action = None
        if meta:
            action = meta.get("privacy_action")
        row = {
            # Wall-clock stamp — without it the trail can't answer "what
            # happened after X?" (rows predating 2026-07-17 lack it).
            "time": round(time.time(), 3),
            "task": task, "provider": provider, "model": model,
            "latency_s": round(latency_s, 3), "ok": ok,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "input_bytes": input_bytes, "cost_usd": round(cost_usd, 6),
        }
        if pmax:
            row["privacy_max"] = pmax
        # Stamp speculative rows so the invariant ("no speculative row ever
        # names a cloud provider") is checkable from the trail itself, not
        # only from the code path that produced it.
        if in_speculative_scope():
            row["speculative"] = True
        if action:
            row["privacy_action"] = action
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
            # Egress privacy rollup — cloud only (local never "left").
            if _is_cloud(provider) and pmax:
                self._privacy_cloud_calls += 1
                self._privacy_by_class[pmax] += 1
                if (self._privacy_max_seen is None
                        or _privacy_rank(pmax)
                        > _privacy_rank(self._privacy_max_seen)):
                    self._privacy_max_seen = pmax
                if action == "refuse" or (pmax == "never-send" and not ok):
                    self._privacy_refused += 1
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            except Exception as exc:   # telemetry must never break a call
                print(f"[model_log] write skipped ({exc}).")
        return row

    def stats(self) -> dict[str, Any]:
        """Aggregated view for the console: per (task, provider, model) rollups
        plus privacy egress summary (plan 6.2)."""
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
            privacy = {
                "cloud_calls": self._privacy_cloud_calls,
                "refused": self._privacy_refused,
                "max_seen": self._privacy_max_seen,
                "by_class": dict(self._privacy_by_class),
            }
        return {"rows": rows, "totals": totals, "privacy": privacy}

    def egress_inventory(self, *, recent: int = 40) -> dict[str, Any]:
        """Auditable 'what left the machine' view (plan 6.2).

        Reads the JSONL trail for external providers and returns recent rows
        with privacy_max, plus class histogram (session + trail).
        """
        recent = max(0, min(int(recent), 200))
        trail: list[dict] = []
        by_class: dict[str, int] = defaultdict(int)
        max_seen: str | None = None
        refused = 0
        try:
            if self._path.is_file():
                lines = self._path.read_text(encoding="utf-8").splitlines()
                for line in lines[-800:]:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if not _is_cloud(d.get("provider") or ""):
                        continue
                    pmax = _normalize_privacy(
                        d.get("privacy_max")
                        or (d.get("meta") or {}).get("privacy_class")
                        or (d.get("meta") or {}).get("privacy_max"))
                    if not pmax:
                        continue
                    action = (d.get("privacy_action")
                              or (d.get("meta") or {}).get("privacy_action"))
                    by_class[pmax] += 1
                    if (max_seen is None
                            or _privacy_rank(pmax) > _privacy_rank(max_seen)):
                        max_seen = pmax
                    if action == "refuse" or (
                            pmax == "never-send" and not d.get("ok", True)):
                        refused += 1
                    trail.append({
                        "time": d.get("time"),
                        "task": d.get("task"),
                        "provider": d.get("provider"),
                        "model": d.get("model"),
                        "ok": d.get("ok"),
                        "privacy_max": pmax,
                        "privacy_action": action,
                        "cost_usd": d.get("cost_usd"),
                        "input_tokens": d.get("input_tokens"),
                    })
        except Exception as exc:
            return {"ok": False, "error": str(exc), "recent": [],
                    "by_class": {}, "max_seen": None}
        trail = trail[-recent:]
        trail.reverse()  # newest first
        with self._lock:
            session = {
                "cloud_calls": self._privacy_cloud_calls,
                "refused": self._privacy_refused,
                "max_seen": self._privacy_max_seen,
                "by_class": dict(self._privacy_by_class),
            }
        return {
            "ok": True,
            "title": "What left the machine",
            "recent": trail,
            "by_class": dict(by_class),
            "max_seen": max_seen,
            "refused": refused,
            "session": session,
        }


model_log = ModelLog()
