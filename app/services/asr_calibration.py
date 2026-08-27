"""Per-engine ingest thresholds — so a confidence scale change cannot silently
change what becomes a memory.

`ingest_filter`'s numbers are not abstract. `min_avg_logprob = -1.0` means "a
faster-whisper utterance whose mean token log-probability is below -1.0 has no
reliable text". That sentence is about Whisper. A TDT decoder emits confidences
on its own scale, and feeding them to Whisper's thresholds does not fail loudly
— it silently moves the line between "kept as memory" and "discarded". For a
memory product that is the worst possible failure mode: it is invisible, and it
deletes.

So thresholds become a property of the engine, not of the process. This module
is the lookup: `cfg_for(engine_id)` returns the ingest config that engine was
calibrated with, or the shipped defaults when it has no calibration. The
calibration itself is fitted offline by `scripts/calibrate_asr_confidence.py`
against the eval fixtures and written to ``data/asr_calibration.json``.

Design rules, all of them about not being clever:

* **Absent calibration is not an error.** Whisper has none and needs none — it
  *is* the scale the defaults were written for. Any engine without an entry gets
  the defaults, which is also what happens before anyone has run the fit.
* **Only threshold fields can be overridden**, never `enabled`, never
  `dedup_window_s`. A calibration file is not a place to turn the filter off.
* **The file is reloaded when it changes on disk**, so a re-fit takes effect on
  the next utterance rather than the next restart, and a torn write falls back
  to the last good value rather than to no filter at all.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.config import settings

# The fields a calibration may set: exactly the ones whose meaning depends on
# the engine's confidence scale. `min_chars`, `review_min_words` and
# `dedup_window_s` are about words and time, which no engine swap changes.
CALIBRATABLE = (
    "min_avg_logprob",       # below this: no reliable text
    "phrase_avg_logprob",    # a denylist phrase this weak is a ghost
    "low_conf_logprob",      # kept, but flagged for the Console
    "max_no_speech_prob",    # engine says it was probably silence
    "phrase_no_speech_prob",
    "low_conf_no_speech",
)

_lock = threading.Lock()
_cache: dict[str, Any] = {"path": None, "mtime": None, "data": {}}


def path() -> Path:
    data = os.environ.get("QUILL_DATA_DIR") or settings.storage.data_dir
    return Path(data) / "asr_calibration.json"


def load(force: bool = False) -> dict:
    """The calibration table, reloaded when the file changes.

    Returns ``{}`` for a missing, unreadable or half-written file — the caller
    then gets shipped defaults, which is the safe direction: an unreadable
    calibration must not widen or narrow what is stored.
    """
    p = path()
    try:
        mtime = p.stat().st_mtime if p.is_file() else None
    except OSError:
        mtime = None
    with _lock:
        if (not force and _cache["path"] == str(p)
                and _cache["mtime"] == mtime):
            return _cache["data"]
        data: dict = {}
        if mtime is not None:
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                engines = raw.get("engines")
                if isinstance(engines, dict):
                    data = engines
            except Exception as exc:
                print(f"[asr-calib] {p.name} unreadable ({exc}); "
                      f"using shipped ingest thresholds.")
                data = _cache["data"] if _cache["path"] == str(p) else {}
        _cache.update(path=str(p), mtime=mtime, data=data)
        return data


def for_engine(engine_id: str | None) -> dict:
    """The raw calibration entry for an engine id, or ``{}``.

    Matching is exact on the full id (``parakeet-onnx:tdt-0.6b-v2``) and then on
    the family (``parakeet-onnx``), so a calibration fitted for one model does
    not silently claim to cover a different checkpoint unless it was written at
    family level on purpose.
    """
    if not engine_id:
        return {}
    table = load()
    entry = table.get(engine_id)
    if entry is None and ":" in engine_id:
        entry = table.get(engine_id.split(":", 1)[0])
    return entry if isinstance(entry, dict) else {}


def thresholds_for(engine_id: str | None) -> dict:
    """Just the threshold overrides, filtered to the calibratable fields."""
    entry = for_engine(engine_id)
    raw = entry.get("thresholds") if isinstance(entry, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {k: float(v) for k, v in raw.items()
            if k in CALIBRATABLE and isinstance(v, (int, float))}


def cfg_for(engine_id: str | None):
    """The ingest config to judge this engine's transcripts with.

    Returns the shipped `settings.ingest` untouched when the engine has no
    calibration — which is the case for Whisper, forever, because the defaults
    were written against Whisper's scale in the first place.
    """
    over = thresholds_for(engine_id)
    if not over:
        return settings.ingest
    try:
        return replace(settings.ingest, **over)
    except Exception as exc:      # a bad field name in a hand-edited file
        print(f"[asr-calib] ignoring calibration for {engine_id} ({exc}).")
        return settings.ingest


def describe(engine_id: str | None) -> dict:
    """What the Console shows: whether this engine's transcripts are being
    judged on calibrated thresholds, and where those came from."""
    entry = for_engine(engine_id)
    if not entry:
        return {"calibrated": False, "engine_id": engine_id}
    return {
        "calibrated": True,
        "engine_id": engine_id,
        "fitted_at": entry.get("fitted_at"),
        "fitted_from": entry.get("fitted_from"),
        "confidence_kind": entry.get("confidence_kind"),
        "n_utterances": entry.get("n_utterances"),
        "thresholds": thresholds_for(engine_id),
    }


def reset_cache() -> None:
    """Tests only."""
    with _lock:
        _cache.update(path=None, mtime=None, data={})
