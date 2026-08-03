"""Machine-specific audio calibration — turn hand-tuned constants into values
DERIVED from THIS machine's own audio.

Several audio thresholds (audio_quality's silence/SNR floors, the speaker-env
cutoffs) were tuned to one developer's mic/rooms. That is machine-specificity in
code — against the invariant. This module lets those values instead come from a
`calibration.json` derived from the user's OWN stored utterances (see
scripts/calibrate_audio.py), so the same code self-tunes on any machine.

Precedence (config.py wires this): explicit env var  >  calibration.json  >  the
shipped literal default. So an operator's explicit QUILL_AQ_* always wins, an
auto/derived value fills in next, and the code literal is the last-resort floor.

IMPORT-SAFE by construction: app.config builds `settings = Settings()` at import
and reads calibration through `cal()`, so nothing here may import app.config or
raise — a missing/broken calibration.json degrades to the caller's default.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_cache: dict | None = None
_cache_path: str | None = None


def _data_dir() -> str:
    # Read the env directly (NOT app.config) to stay import-safe / cycle-free.
    return os.environ.get("QUILL_DATA_DIR", "data")


def calibration_path() -> Path:
    return Path(_data_dir()) / "calibration.json"


def load_calibration(force: bool = False) -> dict:
    """The calibration dict (cached), or {} if none/unreadable. Never raises."""
    global _cache, _cache_path
    with _lock:
        p = str(calibration_path())
        if not force and _cache is not None and _cache_path == p:
            return _cache
        data: dict = {}
        try:
            path = calibration_path()
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
        except Exception:
            data = {}
        _cache, _cache_path = data, p
        return data


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def cal(dotted: str, default):
    """Look up a calibrated value by dotted path (e.g. 'audio_quality.bad_snr_db'),
    returning `default` if it's absent or the file is missing. Import-safe."""
    node = load_calibration()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if isinstance(node, (int, float, str)) else default


def write_calibration(payload: dict) -> Path:
    """Persist a calibration dict to $QUILL_DATA_DIR/calibration.json and refresh
    the cache. Stamps generated_at when the caller didn't."""
    payload = dict(payload)
    payload.setdefault("generated_at", time.time())
    path = calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    invalidate()
    return path


def maybe_autocalibrate() -> dict | None:
    """Idempotent startup hook: derive + adopt a calibration ONCE, only when it's
    safe to. Returns the written payload, or None when it declines (the common
    case). Best-effort — never raises, never blocks startup.

    Guards (all must hold):
      * QUILL_AUTO_CALIBRATE is on (default on),
      * no calibration.json already exists (idempotent — never overwrites a manual
        or prior run; a mic change is handled by deleting the file + re-running),
      * the machine has >= QUILL_CALIBRATE_MIN_N stored utterances (else the
        percentiles aren't trustworthy — cold start keeps the shipped defaults),
      * the derived values pass the internal safety bounds in calibrate_audio
        (the cheap always-available half of the eval gate: a calibration that
        would start dropping real speech is clamped, never adopted raw)."""
    try:
        if os.environ.get("QUILL_AUTO_CALIBRATE", "1") in ("0", "false", "False"):
            return None
        if calibration_path().is_file():
            return None
        min_n = int(os.environ.get("QUILL_CALIBRATE_MIN_N", "300"))
        # Lazy import so a normal run never pays for the calibration code path.
        from scripts.calibrate_audio import derive_calibration, count_clips
        if count_clips() < min_n:
            return None
        payload = derive_calibration(limit=None, source="auto")
        if not payload:
            return None
        return write_calibration(payload)
    except Exception as exc:
        print(f"[calibration] auto-calibrate skipped ({exc}).")
        return None
