"""Evidence playback (plan 3.4 / F2) — fact → event → WAV + span highlight.

Surfaced memories (constellation evidence, Archive fact cards) resolve the
source event's clip and expose a transcript so the UI can "play the moment"
and mark the fact's `source_span` inside that transcript.
"""
from __future__ import annotations

from typing import Any


def clip_from_meta(meta: dict | None) -> dict[str, Any]:
    """Prefer enhanced (what Whisper heard), fall back to raw WAV path."""
    meta = meta if isinstance(meta, dict) else {}
    prov = meta.get("provenance") if isinstance(meta.get("provenance"), dict) else {}
    enhanced = (
        meta.get("enhanced_audio_path")
        or meta.get("enhanced_audio")
        or prov.get("enhanced_audio")
    )
    raw = (
        meta.get("audio_path")
        or meta.get("raw_audio")
        or prov.get("raw_audio")
    )
    play = enhanced or raw
    transcript = (
        prov.get("transcript")
        or meta.get("transcript")
        or ""
    )
    return {
        "audio_path": raw or None,
        "enhanced_audio": enhanced or None,
        "play_path": play or None,
        "transcript": (transcript or "").strip(),
    }


def clip_from_event(ev) -> dict[str, Any]:
    """Resolve clip fields from a stored Event (or event-like object)."""
    if ev is None:
        return {
            "audio_path": None,
            "enhanced_audio": None,
            "play_path": None,
            "transcript": "",
            "modality": None,
            "time": None,
            "event_id": None,
        }
    meta = getattr(ev, "meta", None) or {}
    out = clip_from_meta(meta if isinstance(meta, dict) else {})
    raw = (getattr(ev, "raw", None) or "").strip()
    summary = (getattr(ev, "summary", None) or "").strip()
    if not out["transcript"]:
        out["transcript"] = raw or summary
    mod = getattr(ev, "modality", None)
    out["modality"] = (mod.value if hasattr(mod, "value") else mod)
    out["time"] = getattr(ev, "time", None)
    out["event_id"] = getattr(ev, "id", None)
    return out


def find_span(transcript: str, span: str) -> dict[str, Any] | None:
    """Locate `span` inside transcript (case-insensitive). None if no match."""
    text = transcript or ""
    needle = (span or "").strip()
    if not text or not needle:
        return None
    low = text.lower()
    idx = low.find(needle.lower())
    if idx < 0:
        # Soft: collapse whitespace
        compact_t = " ".join(text.split())
        compact_n = " ".join(needle.split())
        if not compact_n:
            return None
        cidx = compact_t.lower().find(compact_n.lower())
        if cidx < 0:
            return None
        # Map back poorly — return compact form for highlight payload.
        return {
            "start": cidx,
            "end": cidx + len(compact_n),
            "match": compact_t[cidx:cidx + len(compact_n)],
            "before": compact_t[:cidx],
            "after": compact_t[cidx + len(compact_n):],
            "transcript": compact_t,
        }
    return {
        "start": idx,
        "end": idx + len(needle),
        "match": text[idx:idx + len(needle)],
        "before": text[:idx],
        "after": text[idx + len(needle):],
        "transcript": text,
    }


def hydrate_source(
    source: dict,
    ev,
    *,
    source_span: str | None = None,
) -> dict:
    """Attach playback fields onto an evidence source row (mutates + returns)."""
    clip = clip_from_event(ev)
    if clip.get("play_path"):
        source["audio_path"] = clip.get("audio_path")
        source["enhanced_audio"] = clip.get("enhanced_audio")
        source["play_path"] = clip.get("play_path")
    transcript = clip.get("transcript") or source.get("text") or ""
    source["transcript"] = transcript
    span = (source_span or source.get("source_span") or "").strip()
    if span:
        source["source_span"] = span
        hit = find_span(transcript, span)
        if hit:
            source["span_highlight"] = {
                "before": hit["before"],
                "match": hit["match"],
                "after": hit["after"],
            }
    if clip.get("modality") and not source.get("modality"):
        source["modality"] = clip["modality"]
    if clip.get("time") is not None:
        source["time"] = clip["time"]
    source["playable"] = bool(source.get("play_path"))
    return source
