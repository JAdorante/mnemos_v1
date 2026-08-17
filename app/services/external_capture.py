"""Omi / phone-as-mic ingest (Workstream 6).

Authenticated localhost/LAN endpoint. Reuses phone-channel pairing tokens.
Ingested events are observed-tier context — they can never satisfy or trigger
an approval. Commitments extracted from external audio are flagged
``external_source=true``.
"""
from __future__ import annotations

import os
import time
from typing import Any

from app.events import Event, Modality, bus
from app.services import confidence as _conf
from app.services import phone_channel


def enabled() -> bool:
    return os.environ.get("QUILL_EXTERNAL_CAPTURE", "0") not in (
        "0", "false", "False")


def authenticate(authorization: str | None) -> dict | None:
    """Same bearer tokens as the phone channel."""
    return phone_channel.authenticate(authorization)


def ingest_transcript(device: dict, payload: dict) -> dict[str, Any]:
    """Finished transcript segment → Event(modality=audio, source=omi: or external:)."""
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    started = float(payload.get("started_at") or time.time())
    ended = float(payload.get("ended_at") or started)
    speaker = str(payload.get("speaker_hint") or "").strip()
    device_id = str(payload.get("device_id") or device.get("device_id") or "unknown")
    kind = str(payload.get("kind") or "omi").strip().lower()
    if kind not in ("omi", "phone", "external"):
        kind = "external"
    source = f"{kind}:{device_id}"
    meta = {
        "device_id": device_id,
        "speaker_hint": speaker or None,
        "started_at": started,
        "ended_at": ended,
        "external_source": True,
        "epistemic": _conf.OBSERVED if hasattr(_conf, "OBSERVED") else "observed",
        "never_authorizes": True,
    }
    ev = Event(
        time=ended, modality=Modality.AUDIO, raw=text,
        summary=f"[{kind}] {text[:200]}",
        source=source, confidence=float(payload.get("confidence") or 0.7),
        people=[speaker] if speaker else [],
        meta=meta,
    )
    try:
        ev = _conf.attach(ev, epistemic="observed")
    except Exception:
        pass
    bus.publish_nowait(ev)
    return {"ok": True, "source": source, "never_authorizes": True}


def ingest_audio_bytes(device: dict, payload: dict, blob: bytes) -> dict[str, Any]:
    """WAV/Opus bytes → existing VAD→whisper path when available; else refuse."""
    # September: transcript segments are the supported path. Audio bytes are
    # accepted only when the in-process audio pipeline can decode them.
    try:
        from app.services import audio as audio_svc
    except Exception:
        audio_svc = None
    if audio_svc is None or not hasattr(audio_svc, "transcribe_bytes"):
        return {"ok": False, "error": "audio-chunk ingest requires transcript segments in v1",
                "hint": "POST {text, started_at, ended_at, device_id}"}
    try:
        text = audio_svc.transcribe_bytes(blob)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return ingest_transcript(device, {**payload, "text": text})


def never_authorizes_event(event: Event | dict) -> bool:
    meta = event.meta if isinstance(event, Event) else (event.get("meta") or {})
    src = (event.source if isinstance(event, Event) else event.get("source") or "")
    if meta.get("never_authorizes") or meta.get("external_source"):
        return True
    return src.startswith("omi:") or src.startswith("external:") or src.startswith("phone:")
