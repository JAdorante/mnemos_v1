"""Stage-level latency spans — the measurement layer for the latency program.

`model_log` already records per-call wall time, which answers "was that slow?"
and nothing else. This answers "slow *where*": queue wait, cold model load,
prefill, generation, retrieval, post-processing — per request, per task.

Three rules, all inherited from the existing telemetry discipline
(see the `_log` wrapper in ollama_text.py):

1. **Never break the serving path.** Every public function here swallows its
   own exceptions. A broken timer must cost a measurement, never a request.
2. **Never add a probe to the hot path.** Where a stage timing can be read
   from work that already happened, read it — Ollama returns `load_duration`,
   `prompt_eval_duration` and `eval_duration` on every response, so cold-load,
   prefill and generation are free. Do not poll `/api/ps` to ask whether a
   model is resident; the answer is already in the reply you just got.
3. **Zero dependencies.** `time.perf_counter` and stdlib. No OpenTelemetry.

Usage — a trace is one user-visible unit of work, stages are its parts::

    with latency.trace("chat", task="chat") as tr:
        with tr.stage("retrieval"):
            hits = memory.search(q)
        with tr.stage("generation"):
            answer = router.complete("chat", ...)

Code far from the trace's creation can add stages without threading an object
through, because the active trace is thread-local::

    with latency.stage("prefill"):      # no-op when no trace is active
        ...

One JSON row per completed trace lands in ``data/latency_spans.jsonl``;
:func:`percentiles` aggregates it for ``GET /console/latency``.

**Off by default** (``QUILL_LATENCY_SPANS=1`` to enable), because a program
that begins by instrumenting must not begin by changing behavior.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import settings

_tls = threading.local()
_write_lock = threading.Lock()

# Ollama reports `load_duration` on EVERY call, warm or cold — it is not zero
# when the model is resident. Measured on the reference machine
# (qwen2.5:7b-instruct, RTX-class GPU):
#
#     truly cold (after an explicit unload) ..... ~3,800 ms
#     warm (model resident, keep_alive live) ....   ~140-165 ms
#
# So the discriminator is an order of magnitude, not "greater than zero". The
# first draft of this file used 100 ms and classified every warm call as cold,
# which would have reported a 100% cold-start rate and sent Phase 1 chasing a
# problem that did not exist. 1 s sits ~6x above the warm figure and ~4x below
# the cold one on this hardware; override per machine if the model differs
# wildly in size.
#
# The raw value is always kept as the `load_ms` mark, so the census can be
# recomputed from the existing trail at a different threshold — a wrong
# threshold must never be baked irreversibly into the data.
COLD_LOAD_MS = float(os.environ.get("QUILL_LATENCY_COLD_LOAD_MS", "1000"))

# A trace is one unit of user-visible work. Kinds are enum-ish so the console
# can group them; add one by using it.
KIND_CHAT = "chat"
KIND_CAPTURE = "capture"
KIND_MODEL = "model"


def enabled() -> bool:
    """Env-first at call time so tests and operators can toggle without a
    reimport of frozen settings (the `vector_gc` precedent)."""
    raw = os.environ.get("QUILL_LATENCY_SPANS")
    if raw is not None:
        return raw not in ("0", "false", "False")
    return bool(getattr(settings, "latency", None) and settings.latency.enabled)


def _path() -> Path:
    data = os.environ.get("QUILL_DATA_DIR") or settings.storage.data_dir
    return Path(data) / "latency_spans.jsonl"


class Trace:
    """One unit of work and its stage timings. Not thread-safe by design: a
    trace belongs to the thread that opened it (see `current()`)."""

    __slots__ = ("kind", "task", "t0", "stages", "marks", "_open")

    def __init__(self, kind: str, task: str | None = None) -> None:
        self.kind = kind
        self.task = task
        self.t0 = time.perf_counter()
        # stage -> total ms. Summed rather than overwritten: a request that
        # calls the model twice should report the total time it spent there.
        self.stages: dict[str, float] = {}
        self.marks: dict[str, Any] = {}
        self._open = True

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t = time.perf_counter()
        try:
            yield
        finally:
            try:
                self.add(name, (time.perf_counter() - t) * 1000.0)
            except Exception:
                pass

    def add(self, name: str, ms: float) -> None:
        """Add milliseconds to a stage directly — for durations measured
        elsewhere (Ollama's own timings) rather than wrapped in a context."""
        try:
            self.stages[str(name)] = self.stages.get(str(name), 0.0) + float(ms)
        except Exception:
            pass

    def mark(self, key: str, value: Any) -> None:
        """Record a non-duration fact about this trace (cold/warm, tokens,
        cache hit). Marks are dimensions to slice by, not timings."""
        try:
            self.marks[str(key)] = value
        except Exception:
            pass

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000.0

    def row(self) -> dict[str, Any]:
        total = self.elapsed_ms()
        stages = {k: round(v, 2) for k, v in self.stages.items()}
        return {
            "time": round(time.time(), 3),
            "kind": self.kind,
            "task": self.task,
            "total_ms": round(total, 2),
            "stages": stages,
            # What the stage timers did NOT account for. A large unaccounted
            # share means the instrumentation is missing the real cost, which
            # is the failure mode that makes optimization guesswork.
            "unaccounted_ms": round(max(0.0, total - sum(stages.values())), 2),
            **({"marks": self.marks} if self.marks else {}),
        }


class _NullTrace(Trace):
    """What callers get when spans are off: same API, no work, never written."""

    def __init__(self) -> None:
        super().__init__("null", None)
        self._open = False

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        yield

    def add(self, name: str, ms: float) -> None:
        return None

    def mark(self, key: str, value: Any) -> None:
        return None


_NULL = _NullTrace()


def current() -> Trace | None:
    """The trace active on this thread, or None."""
    return getattr(_tls, "trace", None)


@contextmanager
def trace(kind: str, task: str | None = None) -> Iterator[Trace]:
    """Open a trace for one unit of work. Nested traces are not created — an
    inner `trace()` yields the outer one, so a helper that opens a trace does
    not fragment a request that already had one."""
    if not enabled():
        yield _NULL
        return
    existing = current()
    if existing is not None and existing._open:
        yield existing          # already inside a trace: contribute to it
        return
    tr = Trace(kind, task)
    _tls.trace = tr
    try:
        yield tr
    finally:
        tr._open = False
        _tls.trace = None
        _emit(tr)


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Time a stage on the active trace. A no-op when none is active, so it is
    safe to leave in code that runs both inside and outside a request."""
    tr = current()
    if tr is None or not tr._open:
        yield
        return
    with tr.stage(name):
        yield


def add(name: str, ms: float) -> None:
    tr = current()
    if tr is not None and tr._open:
        tr.add(name, ms)


def mark(key: str, value: Any) -> None:
    tr = current()
    if tr is not None and tr._open:
        tr.mark(key, value)


def record_ollama_timings(payload: dict[str, Any]) -> None:
    """Fold an Ollama response's own nanosecond timings into the active trace.

    Free measurement: these fields describe work that already happened, so
    reading them costs nothing on the hot path. `load_duration` above
    COLD_LOAD_MS means the model was not resident — that is the cold-start
    census, with no `/api/ps` probe.
    """
    tr = current()
    if tr is None or not tr._open or not isinstance(payload, dict):
        return
    try:
        def ms(key: str) -> float | None:
            v = payload.get(key)
            return (float(v) / 1e6) if isinstance(v, (int, float)) else None

        load_ms = ms("load_duration")
        prefill_ms = ms("prompt_eval_duration")
        gen_ms = ms("eval_duration")
        if load_ms is not None:
            tr.add("model_load", load_ms)
            tr.mark("cold_load", bool(load_ms >= COLD_LOAD_MS))
            tr.mark("load_ms", round(load_ms, 2))
        if prefill_ms is not None:
            tr.add("prefill", prefill_ms)
        if gen_ms is not None:
            tr.add("generation", gen_ms)
        for key, mark_as in (("prompt_eval_count", "input_tokens"),
                             ("eval_count", "output_tokens")):
            v = payload.get(key)
            if isinstance(v, (int, float)):
                tr.mark(mark_as, int(v))
        # Tokens per second on the generation phase — the number a quantization
        # or draft-model change has to move.
        if gen_ms and payload.get("eval_count"):
            tr.mark("tok_per_s",
                    round(float(payload["eval_count"]) / (gen_ms / 1000.0), 1))
    except Exception as exc:  # pragma: no cover - telemetry never raises
        print(f"[latency] ollama timings skipped ({exc}).")


def record(kind: str, task: str | None = None, *, total_ms: float,
           stages: dict[str, float] | None = None,
           marks: dict[str, Any] | None = None) -> None:
    """Write one completed trace assembled from timings measured elsewhere.

    Rule 2 in reverse: some paths already time their own stages for another
    consumer (the audio pipeline stamps every stage into `audio_telemetry` so
    the Audio Health console can show it) and opening a `trace()` around them
    would time the same work twice. Those callers hand the numbers over here
    instead, so the span trail is complete without a second set of probes.

    `total_ms` is the caller's own end-to-end figure, not a wall-clock read at
    call time — the difference between it and the stage sum is what shows up as
    `unaccounted_ms`, which is the whole point of the row.
    """
    if not enabled():
        return
    try:
        st = {str(k): round(float(v), 2) for k, v in (stages or {}).items()
              if isinstance(v, (int, float))}
        row: dict[str, Any] = {
            "time": round(time.time(), 3),
            "kind": str(kind),
            "task": task,
            "total_ms": round(float(total_ms), 2),
            "stages": st,
            "unaccounted_ms": round(max(0.0, float(total_ms) - sum(st.values())), 2),
        }
        if marks:
            row["marks"] = {str(k): v for k, v in marks.items() if v is not None}
        _write(row)
    except Exception as exc:  # pragma: no cover - telemetry never raises
        print(f"[latency] span record skipped ({exc}).")


def _write(row: dict[str, Any]) -> None:
    p = _path()
    with _write_lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


def _emit(tr: Trace) -> None:
    try:
        _write(tr.row())
    except Exception as exc:  # pragma: no cover - telemetry never raises
        print(f"[latency] span write skipped ({exc}).")


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------
def _pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile: the smallest value at or above rank
    ceil(p/100 * N). Not interpolated — with the sample sizes a single laptop
    produces, interpolation invents precision that isn't there.

    `ceil`, not `round`: Python rounds halves to even, so round(99.5) is 100
    and p99 of 1..100 came back as 100 instead of 99.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    k = math.ceil(p / 100.0 * len(ordered)) - 1
    return ordered[max(0, min(len(ordered) - 1, k))]


def read_rows(*, limit: int = 20_000, since: float | None = None,
              path: Path | None = None) -> list[dict[str, Any]]:
    """Recent trace rows, oldest-first. Tolerates a partially written tail."""
    p = Path(path) if path is not None else _path()
    rows: list[dict[str, Any]] = []
    try:
        if not p.is_file():
            return []
        with p.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-int(limit):]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue            # a torn last line, not a reason to fail
            if not isinstance(row, dict):
                continue
            if since is not None and float(row.get("time") or 0) < since:
                continue
            rows.append(row)
    except Exception as exc:
        print(f"[latency] read skipped ({exc}).")
    return rows


def percentiles(rows: list[dict[str, Any]] | None = None, *,
                since: float | None = None,
                cold_load_ms: float | None = None) -> dict[str, Any]:
    """p50/p90/p99 per stage per (kind, task), plus the cold-start census.

    Pure over `rows` so the console, the benchmark and the tests all compute
    the same numbers from the same function.

    `cold_load_ms` recomputes the cold/warm split from each row's raw
    `load_ms` mark instead of trusting the `cold_load` flag written at
    capture time — so a threshold that turns out to be wrong (it did once;
    see COLD_LOAD_MS) can be corrected over data already collected.
    """
    rows = read_rows(since=since) if rows is None else rows
    threshold = float(cold_load_ms) if cold_load_ms is not None else COLD_LOAD_MS
    groups: dict[tuple, dict[str, list[float]]] = {}
    totals: dict[tuple, list[float]] = {}
    cold: dict[tuple, list[bool]] = {}

    for row in rows:
        key = (str(row.get("kind") or "?"), str(row.get("task") or "-"))
        totals.setdefault(key, []).append(float(row.get("total_ms") or 0.0))
        stages = row.get("stages") or {}
        if isinstance(stages, dict):
            bucket = groups.setdefault(key, {})
            for name, ms in stages.items():
                try:
                    bucket.setdefault(str(name), []).append(float(ms))
                except (TypeError, ValueError):
                    continue
        una = row.get("unaccounted_ms")
        if isinstance(una, (int, float)):
            groups.setdefault(key, {}).setdefault("unaccounted", []).append(float(una))
        marks = row.get("marks") or {}
        if isinstance(marks, dict):
            raw = marks.get("load_ms")
            if isinstance(raw, (int, float)):
                cold.setdefault(key, []).append(bool(float(raw) >= threshold))
            elif "cold_load" in marks:      # rows written before load_ms existed
                cold.setdefault(key, []).append(bool(marks["cold_load"]))

    out_rows = []
    for key in sorted(totals):
        kind, task = key
        samples = totals[key]
        stage_rows = []
        for name, vals in sorted(groups.get(key, {}).items()):
            stage_rows.append({
                "stage": name, "n": len(vals),
                "p50": round(_pct(vals, 50), 1),
                "p90": round(_pct(vals, 90), 1),
                "p99": round(_pct(vals, 99), 1),
                "mean": round(sum(vals) / len(vals), 1) if vals else 0.0,
                # Share of the group's total time this stage accounts for —
                # the column that says where to optimize next.
                "share_pct": round(
                    100.0 * sum(vals) / max(1e-9, sum(samples)), 1),
            })
        stage_rows.sort(key=lambda r: -r["share_pct"])
        flags = cold.get(key) or []
        out_rows.append({
            "kind": kind, "task": task, "n": len(samples),
            "total": {"p50": round(_pct(samples, 50), 1),
                      "p90": round(_pct(samples, 90), 1),
                      "p99": round(_pct(samples, 99), 1)},
            "stages": stage_rows,
            "cold_load_pct": (round(100.0 * sum(flags) / len(flags), 2)
                              if flags else None),
            "cold_load_n": sum(flags) if flags else 0,
        })

    all_cold = [c for vals in cold.values() for c in vals]
    return {
        "ok": True,
        "enabled": enabled(),
        "traces": len(rows),
        "rows": out_rows,
        # The Phase 0 cold-start census, in one number.
        "cold_start": {
            "calls": len(all_cold),
            "cold": sum(all_cold),
            "pct": (round(100.0 * sum(all_cold) / len(all_cold), 2)
                    if all_cold else None),
            "threshold_ms": threshold,
        },
    }


def capture_stages(window_s: float = 86400.0, store=None) -> dict[str, Any]:
    """The audio path's stage breakdown, read from `audio_telemetry`.

    Deliberately NOT a second set of timers. The capture thread already records
    `queue_wait_ms`, `asr_latency_ms` and `total_latency_ms` per utterance, and
    adding a parallel span writer to the hottest thread in the system to
    re-measure the same quantities would be the exact probe this module's rule 2
    forbids. So the aggregator reads what capture already wrote.

    `post_asr` is the residual (total - queue_wait - asr): everything between a
    finished transcript and a published event — dedup, echo suppression,
    speaker ID, storage. It is the share Phase 3.1 would pipeline away, and the
    only way to know whether that is worth doing is to see it here first. The
    capture thread also records `post_ms` directly now; the residual is kept as
    the reported stage because it closes the budget by construction, while the
    measured column additionally covers utterances that were *dropped* late.

    `vad_ms` is reported beside the stages rather than as one of them: Silero
    runs during speech, before the speech-end the budget starts at, so folding
    it in would make the shares sum to more than the whole. It is a cost
    figure — the always-on CPU that Phase B's two-tier ladder has to beat.

    `by_channel` splits mic from loopback. They have different audio, different
    timing and different failure modes; a single p90 across both is an average
    of two distributions and moves for reasons nobody can act on.
    """
    out: dict[str, Any] = {"window_s": window_s, "n": 0, "stages": [],
                           "capture_to_published": {}, "vad_ms": {},
                           "by_channel": {}}
    _WHERE = ("FROM audio_telemetry WHERE ts >= ? AND outcome = 'kept' "
              "AND total_latency_ms IS NOT NULL")
    _BASE = "queue_wait_ms, asr_latency_ms, total_latency_ms"
    try:
        if store is None:
            from app.storage import get_store
            store = get_store()
        cutoff = time.time() - float(window_s)
        try:
            rows = [dict(r) for r in store._conn.execute(
                f"SELECT {_BASE}, vad_ms, post_ms, channel, engine {_WHERE}",
                (cutoff,)).fetchall()]
        except Exception:
            # A store predating the stage-timer columns: fall back to the
            # original three so an old DB reports rather than erroring.
            rows = [dict(r) for r in store._conn.execute(
                f"SELECT {_BASE} {_WHERE}", (cutoff,)).fetchall()]
    except Exception as exc:
        return {**out, "error": str(exc)}

    def col(name: str) -> list[float]:
        return [float(r[name]) for r in rows
                if isinstance(r.get(name), (int, float))]

    totals = col("total_latency_ms")
    if not totals:
        return out
    queue, asr = col("queue_wait_ms"), col("asr_latency_ms")
    post = [max(0.0, float(r["total_latency_ms"])
                - float(r.get("queue_wait_ms") or 0.0)
                - float(r.get("asr_latency_ms") or 0.0))
            for r in rows if isinstance(r.get("total_latency_ms"), (int, float))]

    for name, vals in (("queue_wait", queue), ("asr", asr), ("post_asr", post)):
        if not vals:
            continue
        out["stages"].append({
            "stage": name, "n": len(vals),
            "p50": round(_pct(vals, 50), 1),
            "p90": round(_pct(vals, 90), 1),
            "p99": round(_pct(vals, 99), 1),
            "share_pct": round(100.0 * sum(vals) / max(1e-9, sum(totals)), 1),
        })
    out["stages"].sort(key=lambda r: -r["share_pct"])
    out["n"] = len(totals)
    out["capture_to_published"] = {
        "p50": round(_pct(totals, 50), 1),
        "p90": round(_pct(totals, 90), 1),
        "p99": round(_pct(totals, 99), 1),
    }
    vad = col("vad_ms")
    if vad:
        out["vad_ms"] = {"n": len(vad), "p50": round(_pct(vad, 50), 1),
                         "p90": round(_pct(vad, 90), 1),
                         "p99": round(_pct(vad, 99), 1)}
    channels: dict[str, list[dict]] = {}
    for r in rows:
        ch = r.get("channel")
        if ch:
            channels.setdefault(str(ch), []).append(r)
    for ch, crows in sorted(channels.items()):
        ctot = [float(r["total_latency_ms"]) for r in crows
                if isinstance(r.get("total_latency_ms"), (int, float))]
        casr = [float(r["asr_latency_ms"]) for r in crows
                if isinstance(r.get("asr_latency_ms"), (int, float))]
        if not ctot:
            continue
        out["by_channel"][ch] = {
            "n": len(ctot),
            "total_p50": round(_pct(ctot, 50), 1),
            "total_p90": round(_pct(ctot, 90), 1),
            "asr_p90": round(_pct(casr, 90), 1) if casr else None,
        }
    engines = sorted({str(r["engine"]) for r in rows if r.get("engine")})
    if engines:
        out["engines"] = engines
    return out


def reset(path: Path | None = None) -> None:
    """Drop the trail — used by the benchmark to measure one clean run."""
    try:
        p = Path(path) if path is not None else _path()
        if p.is_file():
            p.unlink()
    except Exception as exc:
        print(f"[latency] reset skipped ({exc}).")
