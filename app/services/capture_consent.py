"""Capture consent — nothing records until the user opts in in the UI.

Fresh installs must not silently open the mic, webcam, screen, or system
loopback. Consent is persisted under data/ so a returning session can
re-arm only the sources the user already approved. Kill/pause is separate
(runtime stop); this module is the durable allow-list.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

# Per-source keys the UI and start_all agree on.
# "clicks" is a sub-switch of desktop capture (mouse events); independent of
# screen frames so testers can watch the display without flooding click rows.
SOURCES = ("mic", "webcam", "screen", "system_audio", "save_audio", "clicks")

_lock = threading.RLock()
_cached: dict[str, Any] | None = None


def _path() -> Path:
    from app.config import settings
    return Path(settings.storage.data_dir) / "capture_consent.json"


def _blank() -> dict[str, Any]:
    return {
        "consented": False,
        "consented_at": None,
        "updated_at": None,
        "sources": {s: False for s in SOURCES},
    }


def load(*, force: bool = False) -> dict[str, Any]:
    """Return the consent record (never raises)."""
    global _cached
    with _lock:
        if _cached is not None and not force:
            return dict(_cached)
        out = _blank()
        try:
            p = _path()
            if p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    out["consented"] = bool(raw.get("consented"))
                    out["consented_at"] = raw.get("consented_at")
                    out["updated_at"] = raw.get("updated_at")
                    src = raw.get("sources") or {}
                    if isinstance(src, dict):
                        for s in SOURCES:
                            out["sources"][s] = bool(src.get(s))
        except Exception as exc:
            print(f"[capture_consent] load skipped ({exc}).")
        _cached = dict(out)
        return dict(out)


def allows(source: str) -> bool:
    """True only when the user has consented AND this source is on."""
    state = load()
    if not state.get("consented"):
        return False
    return bool((state.get("sources") or {}).get(source))


def any_recording_source() -> bool:
    state = load()
    if not state.get("consented"):
        return False
    src = state.get("sources") or {}
    return any(bool(src.get(s))
               for s in ("mic", "webcam", "screen", "system_audio", "clicks"))


def save(sources: dict[str, bool] | None = None, *,
         consented: bool | None = None) -> dict[str, Any]:
    """Persist consent. Passing consented=False clears the allow-list."""
    global _cached
    now = time.time()
    with _lock:
        cur = load(force=True)
        if consented is False:
            cur = _blank()
            cur["updated_at"] = now
        else:
            if sources:
                for s in SOURCES:
                    if s in sources:
                        cur["sources"][s] = bool(sources[s])
            # First save that turns anything on (or explicit consented=True)
            # stamps consented_at; later edits only bump updated_at.
            turning_on = consented is True or any(
                cur["sources"].get(s) for s in SOURCES)
            if turning_on:
                if not cur.get("consented"):
                    cur["consented"] = True
                    cur["consented_at"] = now
                elif consented is True:
                    cur["consented"] = True
            cur["updated_at"] = now
        try:
            from app.atomic_json import write_json
            write_json(_path(), cur, sort_keys=True)
        except Exception as exc:
            print(f"[capture_consent] save failed ({exc}).")
        _cached = dict(cur)
        _apply_save_audio(bool(cur["sources"].get("save_audio")))
        _apply_capability_flags(cur["sources"])
        return dict(cur)


def _apply_save_audio(on: bool) -> None:
    """Hot-patch storage.save_audio so WAV persistence matches consent."""
    try:
        from app.config import settings
        object.__setattr__(settings.storage, "save_audio", bool(on))
        import os
        os.environ["QUILL_SAVE_AUDIO"] = "1" if on else "0"
    except Exception as exc:
        print(f"[capture_consent] save_audio patch skipped ({exc}).")


def _apply_capability_flags(sources: dict[str, bool]) -> None:
    """Hot-patch env + settings so pipeline .enabled checks match consent."""
    import os
    try:
        from app.config import settings
        desktop_on = bool(sources.get("screen") or sources.get("clicks"))
        pairs = (
            ("webcam", "QUILL_VISION", settings.vision, "enabled"),
            ("system_audio", "QUILL_SYSTEM_AUDIO", settings.system_audio, "enabled"),
        )
        for src, env, obj, attr in pairs:
            on = bool(sources.get(src))
            os.environ[env] = "1" if on else "0"
            object.__setattr__(obj, attr, on)
        # Master desktop pipeline on when screen frames and/or clicks are allowed.
        os.environ["QUILL_DESKTOP_CAPTURE"] = "1" if desktop_on else "0"
        object.__setattr__(settings.desktop_capture, "enabled", desktop_on)
        object.__setattr__(settings.desktop_capture, "screen",
                           bool(sources.get("screen")))
        clicks_on = bool(sources.get("clicks"))
        os.environ["QUILL_DESKTOP_CAPTURE_CLICKS"] = "1" if clicks_on else "0"
        object.__setattr__(settings.desktop_capture, "clicks", clicks_on)
        # Keep screen sub-flag aligned when only clicks is consented.
        os.environ["QUILL_DESKTOP_CAPTURE_SCREEN"] = (
            "1" if sources.get("screen") else "0")
    except Exception as exc:
        print(f"[capture_consent] capability patch skipped ({exc}).")


def apply_saved_to_runtime() -> None:
    """Re-apply consent flags after process boot (settings already loaded)."""
    state = load(force=True)
    if state.get("consented"):
        src = state.get("sources") or {}
        _apply_save_audio(bool(src.get("save_audio")))
        _apply_capability_flags(src)


def status() -> dict[str, Any]:
    state = load()
    return {
        "consented": bool(state.get("consented")),
        "consented_at": state.get("consented_at"),
        "updated_at": state.get("updated_at"),
        "sources": dict(state.get("sources") or {}),
        "path": str(_path()),
    }
