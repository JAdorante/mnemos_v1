"""The ASR engine seam — one protocol, a registry, and the incumbent behind it.

Until now `audio.py` called faster-whisper directly: a module-level shared
`WhisperModel`, a transcribe lock, and a call site that knew Whisper's parameter
names, Whisper's segment objects and Whisper's confidence scale. That is fine
with one engine and unworkable with two, because every place that knows the
engine's shape is a place a swap has to be re-litigated.

So the engine is now a thing you can hold: `get_engine()` returns an `ASREngine`
selected by ``QUILL_ASR_ENGINE``, and `audio.py` knows nothing about it beyond
this file's protocol. Adding Parakeet means adding a module and one `register()`
line; rolling back means setting the flag to `whisper`.

Three details are load-bearing, all inherited from the code this replaces:

1. **One model per process, not per pipeline.** Mic and loopback are two
   `AudioPipeline` instances sharing one engine — loading two models doubled RAM
   and made the two pipelines fight for cores, which is how meeting backlog once
   blew past a minute while ASR itself took 5-14 s. `get_engine()` is a lazy
   process-wide singleton per engine name, and engines own their own locking.
2. **Confidence is engine-specific and must not be compared across engines.**
   `ASRResult.avg_confidence` carries a number whose *scale* is named by
   `confidence_kind`. Whisper's is `avg_logprob` (roughly -0.1 crisp to -1.5
   garbage), which is what `ingest_filter`'s thresholds were calibrated against.
   A TDT decoder's confidence is not that number. An engine reporting a
   different `confidence_kind` needs its own thresholds before it decides what
   becomes a memory — the seam makes the mismatch visible instead of letting it
   silently change what gets stored.
3. **Context (ASR bias) is optional.** Whisper's `initial_prompt` biases
   decoding toward known names; not every engine has an equivalent, and forcing
   one on an engine that lacks it either errors or is ignored silently.
   `supports_context` says so, and callers check it rather than assuming.
"""
from __future__ import annotations

import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

import numpy as np

from app.config import settings

# Confidence scales. `ingest_filter`'s thresholds assume AVG_LOGPROB today.
AVG_LOGPROB = "avg_logprob"


@dataclass
class ASRResult:
    """One engine's answer for one utterance.

    `segments` carries whatever per-segment objects the engine produced, duck-
    typed for `ingest_filter.assess`, which reads `.avg_logprob` and
    `.no_speech_prob` and falls back to text-only checks when they are absent.
    That is the entire compatibility contract an engine owes the ingest layer:
    an engine with no per-segment confidence still works, it just gets judged on
    its words rather than its certainty.
    """
    text: str = ""
    avg_confidence: float | None = None
    confidence_kind: str = AVG_LOGPROB
    word_timestamps: list[dict] = field(default_factory=list)
    language: str | None = None
    engine_id: str = ""
    segments: list = field(default_factory=list)


@runtime_checkable
class ASREngine(Protocol):
    engine_id: str          # "whisper:small" — provenance stamped per transcript
    model_id: str           # the model alone, for telemetry
    supports_context: bool  # can it take an ASR-bias prompt?
    confidence_kind: str

    def transcribe(self, samples: np.ndarray, sample_rate: int,
                   context: str | None = None) -> ASRResult: ...


class WhisperEngine:
    """faster-whisper (CTranslate2) — the incumbent, and the rollback target.

    Holds the model and the transcribe lock that used to live at `audio.py`
    module scope. The lock is Whisper's own concern: CTranslate2 supports
    concurrent `transcribe()` only when built with `num_workers > 1`, so with the
    default single worker the two pipelines have to take turns. An engine with
    different concurrency rules simply doesn't have this lock.
    """

    supports_context = True
    confidence_kind = AVG_LOGPROB

    def __init__(self, model: str | None = None,
                 word_timestamps: bool | None = None) -> None:
        from faster_whisper import WhisperModel

        cfg = settings.audio
        self.cfg = cfg
        self.model_id = model or cfg.whisper_model
        self.engine_id = f"whisper:{self.model_id}"
        self.word_timestamps = (cfg.word_timestamps if word_timestamps is None
                                else bool(word_timestamps))
        self._lock = threading.Lock()
        kwargs: dict[str, Any] = dict(device=cfg.device,
                                      compute_type=cfg.compute_type)
        if cfg.cpu_threads > 0:
            kwargs["cpu_threads"] = cfg.cpu_threads
        if cfg.num_workers > 1:
            kwargs["num_workers"] = cfg.num_workers
        print(f"[asr] loading shared Whisper '{self.model_id}' "
              f"({cfg.compute_type}, {cfg.device}, beam={cfg.beam_size}, "
              f"workers={cfg.num_workers}) ...")
        self.model = WhisperModel(self.model_id, **kwargs)
        print("[asr] shared Whisper ready.")

    def transcribe(self, samples: np.ndarray, sample_rate: int,
                   context: str | None = None) -> ASRResult:
        cfg = self.cfg
        lock = nullcontext() if cfg.num_workers > 1 else self._lock
        with lock:
            segments, info = self.model.transcribe(
                samples,
                language=cfg.language,
                vad_filter=False,          # the caller already ran VAD
                beam_size=max(1, cfg.beam_size),
                best_of=max(1, cfg.best_of),
                temperature=cfg.temperature,
                condition_on_previous_text=cfg.condition_on_previous_text,
                initial_prompt=context or None,
                word_timestamps=self.word_timestamps,
            )
            # Materialize under the lock: the generator pulls from the model.
            segs = list(segments)

        logps = [s.avg_logprob for s in segs
                 if getattr(s, "avg_logprob", None) is not None]
        words: list[dict] = []
        if self.word_timestamps:
            for s in segs:
                for w in (getattr(s, "words", None) or []):
                    words.append({"word": w.word,
                                  "start": round(float(w.start), 3),
                                  "end": round(float(w.end), 3),
                                  "probability": getattr(w, "probability", None)})
        return ASRResult(
            text=" ".join(s.text.strip() for s in segs).strip(),
            avg_confidence=(sum(logps) / len(logps)) if logps else None,
            confidence_kind=self.confidence_kind,
            word_timestamps=words,
            language=getattr(info, "language", None),
            engine_id=self.engine_id,
            segments=segs,
        )


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
# Phase A adds "parakeet-onnx" / "parakeet-cpp" here. This dict is the only
# place that has to learn an engine exists — which is the property that makes a
# swap a flag flip and a rollback instant.
ENGINES: dict[str, Callable[..., ASREngine]] = {
    "whisper": WhisperEngine,
}

_shared: dict[str, ASREngine] = {}
_shared_lock = threading.Lock()


def register(name: str, factory: Callable[..., ASREngine]) -> None:
    """Add an engine. Kept public so an engine module can register itself on
    import without this file importing (and therefore requiring) its deps."""
    ENGINES[str(name).strip().lower()] = factory


def available() -> list[str]:
    return sorted(ENGINES)


def configured() -> str:
    """The engine name from config. Read per call rather than cached, so a test
    or an operator can flip it without a reimport of frozen settings."""
    return (settings.audio.asr_engine or "whisper").strip().lower()


def make_engine(name: str | None = None, **kw) -> ASREngine:
    """A fresh, unshared engine instance. For the eval harness, which needs to
    hold two engines at once and to override options the live path takes from
    config."""
    key = (name or configured()).strip().lower()
    factory = ENGINES.get(key)
    if factory is None:
        raise ValueError(
            f"unknown ASR engine {name!r}; available: {', '.join(available())}")
    return factory(**kw)


def get_engine(name: str | None = None) -> ASREngine:
    """The process-wide engine, loaded on first use.

    One instance serves every `AudioPipeline`. Two pipelines asking at once get
    the same object and only one load, which is the behavior the module-level
    `_get_shared_whisper()` provided and the reason it existed.
    """
    key = (name or configured()).strip().lower()
    with _shared_lock:
        engine = _shared.get(key)
        if engine is None:
            engine = make_engine(key)
            _shared[key] = engine
        return engine


def reset_shared() -> None:
    """Drop the cached instances. Tests only — the live path never re-loads."""
    with _shared_lock:
        _shared.clear()


# ---------------------------------------------------------------------------
# eval provenance
# ---------------------------------------------------------------------------
def last_eval_report(report_dir: str | None = None) -> dict:
    """The most recent `scripts/eval_asr.py` report, flattened for the console.

    The Audio Health panel has to answer two questions a bug report turns on:
    which engine is running, and did that engine pass the hallucination probe?
    The first comes from live telemetry; the second only exists offline, in the
    harness's own output. Surfacing it here means a tester's screenshot carries
    the acceptance evidence for the build they are running, instead of someone
    having to go find the report that matched it.

    Stale by nature — it describes the last run, not the current process. The
    returned `ran_at` is how a reader tells the difference, so it is never
    omitted. Best-effort: no report, an unreadable one, or a half-written one
    yields ``{}`` rather than an error, because this is a console decoration and
    the panel must render without it.
    """
    import json
    import os
    from pathlib import Path as _Path

    try:
        base = _Path(report_dir or (
            _Path(os.environ.get("QUILL_DATA_DIR") or settings.storage.data_dir)
            / "eval" / "asr"))
        reports = sorted(base.glob("report_*.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if not reports:
            return {}
        newest = reports[0]
        data = json.loads(newest.read_text(encoding="utf-8"))
        overall = data.get("overall") or {}
        return {
            "tag": data.get("tag"),
            "engine_id": (data.get("config") or {}).get("engine_id"),
            "ran_at": round(newest.stat().st_mtime, 3),
            "report": newest.name,
            "n_clips": overall.get("n_clips"),
            "wer": overall.get("wer"),
            "rtf": overall.get("rtf"),
            # Both rates, because the gap between them is how much work
            # ingest_filter is doing to hide an engine's hallucinations.
            "raw_hallucination_rate": overall.get("raw_hallucination_rate"),
            "post_filter_hallucination_rate":
                overall.get("post_filter_hallucination_rate"),
            "attribution_error_rate": overall.get("attribution_error_rate"),
        }
    except Exception as exc:
        print(f"[asr] eval report unreadable ({exc}).")
        return {}
