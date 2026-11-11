"""Opt-in, full-fidelity capture of local-model calls — the Sparrow 1.0 seam.

OFF unless ``SPARROW_CAPTURE_PATH`` is set. With it unset every function here
is a no-op and nothing about a call changes. With it set, every /api/chat
round trip `OllamaText` makes appends exactly ONE JSON line to that file:

    call_id         unique id of this HTTP round trip
    task            router task (extract, project_route, chat, ...)
    prompt_version  hash of the prompt TEMPLATE that produced the call: the
                    caller's system prompt with the per-call clock line
                    masked, plus the schema and confidence-trailer mode.
                    Exemplars and retrieved context are not part of it.
    messages        the messages exactly as sent (system prompt, exemplars,
                    retrieved context — nothing clipped)
    format          the JSON schema Ollama constrained the reply with, or null
    options         sampling options sent
    response        the model's raw output, before any parsing or stripping
    done_reason     Ollama's stop reason ("length" = hit num_predict)
    model           {"tag": ..., "digest": ...}
    world_id, item_id, phase
                    what the driver says triggered the call (see `context`)
    retrieved_ids   {"facts": [...], "events": [...]} that retrieval passed
                    into this call's prompt (see `note_retrieved`), or null
    attempt         1, or 2 for OllamaText's single local retry
    latency_ms, tokens_in, tokens_out
    error           set (and response null) when the round trip raised

This is NOT telemetry. `model_log` keeps sizes and latency only and is safe to
leave on; this file holds whole prompts. It exists to record synthetic worlds
for base-model training (sparrow-train) and must never be pointed at a real
user's traffic.

A write failure never breaks the call; it is counted in `stats()` so a driver
can refuse a capture that lost records.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

ENV = "SPARROW_CAPTURE_PATH"

_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "sparrow_capture_ctx", default=None)
_lock = threading.Lock()
_digests: dict[tuple[str, str], str | None] = {}
_stats = {"written": 0, "failed": 0}

# The context anchor's clock line ("RIGHT NOW (user's local time): Sunday,
# November 8 2026, 11:09 PM") is the only per-call part of a task's system
# prompt; masking it is what makes prompt_version a property of the template.
_CLOCK_LINE = re.compile(r"^(RIGHT NOW\b[^:\n]*:).*$", re.M)


def path() -> str | None:
    return os.environ.get(ENV) or None


def enabled() -> bool:
    return bool(path())


def stats() -> dict[str, int]:
    return dict(_stats)


def prompt_version(task: str, system: str, schema: dict | None,
                   trailer: str) -> str:
    """Stable id of the prompt template behind a call."""
    norm = _CLOCK_LINE.sub(r"\1 <clock>", system or "")
    blob = json.dumps([task, norm, schema, trailer], sort_keys=True,
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@contextmanager
def context(**fields: Any) -> Iterator[None]:
    """Label every call made inside this block, e.g.
    ``with model_capture.context(world_id="w1", item_id="e07"): ...``.
    Nested blocks inherit and override. Each block starts with no
    retrieved ids of its own."""
    if not enabled():
        yield
        return
    cur = dict(_ctx.get() or {})
    cur.update(fields)
    cur["retrieved_ids"] = None
    token = _ctx.set(cur)
    try:
        yield
    finally:
        _ctx.reset(token)


def note_retrieved(*, facts: Any = (), events: Any = ()) -> None:
    """Retrieval reports the fact / event ids it put into a prompt. They are
    attached to every later call in the same `context` block. Outside a block
    this is a no-op: ids with no scope would leak into unrelated calls."""
    if not enabled():
        return
    cur = _ctx.get()
    if cur is None:
        return

    def ints(xs: Any) -> list[int]:
        out: list[int] = []
        for x in xs or ():
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out

    have = cur.get("retrieved_ids") or {"facts": [], "events": []}
    cur["retrieved_ids"] = {
        "facts": sorted(set(have["facts"]) | set(ints(facts))),
        "events": sorted(set(have["events"]) | set(ints(events))),
    }


def _digest(url: str, model: str) -> str | None:
    key = (url, model)
    if key not in _digests:
        dg = None
        try:
            import urllib.request
            with urllib.request.urlopen(url + "/api/tags", timeout=3) as r:
                tags = json.load(r)
            for m in tags.get("models", []):
                if m.get("name") == model or m.get("model") == model:
                    dg = m.get("digest")
                    break
        except Exception:
            dg = None
        _digests[key] = dg
    return _digests[key]


def _write(rec: dict) -> None:
    p = path()
    if not p:
        return
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        with _lock:
            os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                try:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX)
                except Exception:
                    pass
                f.write(line)
                f.flush()
        _stats["written"] += 1
    except Exception as exc:  # pragma: no cover - disk full, permissions
        _stats["failed"] += 1
        print(f"[model_capture] record lost ({exc}).")


class Pending:
    """One in-flight round trip. `begin` returns None when capture is off."""

    def __init__(self, *, url: str, task: str, payload: dict,
                 meta: dict | None) -> None:
        self.url, self.task, self.payload = url, task, payload
        self.meta = dict(meta or {})
        self.ctx = dict(_ctx.get() or {})
        self.t0 = time.time()

    def _base(self) -> dict:
        p = self.payload
        return {
            "call_id": uuid.uuid4().hex,
            "time": self.t0,
            "task": self.task,
            "prompt_version": self.meta.get("prompt_version"),
            "messages": p.get("messages"),
            "format": p.get("format"),
            "options": p.get("options"),
            "model": {"tag": p.get("model"),
                      "digest": _digest(self.url, str(p.get("model")))},
            "world_id": self.ctx.get("world_id"),
            "item_id": self.ctx.get("item_id"),
            "phase": self.ctx.get("phase"),
            "retrieved_ids": self.ctx.get("retrieved_ids"),
            "attempt": self.meta.get("attempt", 1),
            "had_exemplars": bool(self.meta.get("had_exemplars")),
            "latency_ms": round((time.time() - self.t0) * 1000, 1),
        }

    def done(self, out: dict) -> None:
        rec = self._base()
        rec.update(response=((out.get("message") or {}).get("content")),
                   done_reason=out.get("done_reason"),
                   tokens_in=out.get("prompt_eval_count"),
                   tokens_out=out.get("eval_count"), error=None)
        _write(rec)

    def fail(self, exc: BaseException) -> None:
        rec = self._base()
        rec.update(response=None, done_reason=None, tokens_in=None,
                   tokens_out=None, error=f"{type(exc).__name__}: {exc}")
        _write(rec)


def begin(*, url: str, task: str, payload: dict,
          meta: dict | None = None) -> Pending | None:
    if not enabled():
        return None
    try:
        return Pending(url=url, task=task, payload=payload, meta=meta)
    except Exception as exc:  # pragma: no cover
        _stats["failed"] += 1
        print(f"[model_capture] begin failed ({exc}).")
        return None
