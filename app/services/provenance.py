"""#12 — the provenance chain: raw -> enhanced -> transcript -> corrections.

The roadmap's capstone for "audio -> trusted evidence": every stored utterance
should carry its full evidence trail, so a downstream fact can be traced back to
the exact sound it came from AND every change made along the way is on the record.

    raw_audio       the original waveform (WAV path) — the ground truth
    enhanced_audio  the denoised copy actually transcribed, if any (#2)
    transcript      what Whisper produced
    asr_prompt      the vocabulary bias applied at ASR time (#3) — a source-side
                    nudge, recorded so a biased spelling is never mistaken for
                    what was plainly said
    corrections     an ordered, APPEND-ONLY log of every change after capture:
                      asr_bias / denoise      seeded at capture
                      recipient_grounding /   applied at the phone approval gate
                        body_cleanup            ("Abby" -> "Abby Nengel", typo fixes)
                      user_edit               a human correcting an extracted fact
                      entity_correction       a resolved-entity fix

The chain lives in `event.meta["provenance"]`. Capture-time fields are written by
audio.py before the event is stored; LATER stages APPEND to it via
`append_correction(event_id, ...)`, so a correction made at the approval gate hours
later still lands on the original utterance's record. Reading it back
(`chain_for`) gives one inspectable answer to "where did this come from, and what
did we change?" — the provenance the Console and any audit need.

Best-effort throughout: provenance must never break capture or an edit. Toggle the
capture-time chain with QUILL_PROVENANCE (default on); it's cheap structured meta.
"""
from __future__ import annotations

import os
import time

# Correction stages — constants so producers and the renderer agree on keys.
ASR_BIAS = "asr_bias"
DENOISE = "denoise"
RECIPIENT_GROUNDING = "recipient_grounding"
BODY_CLEANUP = "body_cleanup"
USER_EDIT = "user_edit"
ENTITY_CORRECTION = "entity_correction"

_STAGE_LABEL = {
    ASR_BIAS: "ASR vocabulary bias",
    DENOISE: "denoised before ASR",
    RECIPIENT_GROUNDING: "recipient grounded",
    BODY_CLEANUP: "message cleaned",
    USER_EDIT: "human edit",
    ENTITY_CORRECTION: "entity corrected",
}


def enabled() -> bool:
    """Stamp the capture-time provenance chain onto transcripts? On by default."""
    return os.environ.get("QUILL_PROVENANCE", "1") not in ("0", "false", "False")


def correction(stage: str, *, before: str = "", after: str = "",
               note: str = "", ts: float | None = None) -> dict:
    """One entry in the correction log. `before`/`after` capture what changed (a
    spelling, a recipient, a fact's text); `note` explains it. Empty fields are
    dropped so the record stays compact."""
    c: dict = {"stage": stage, "ts": round(ts if ts is not None else time.time(), 3)}
    if before:
        c["before"] = before
    if after:
        c["after"] = after
    if note:
        c["note"] = note
    return c


def build(*, raw_audio: str | None = None, enhanced_audio: str | None = None,
          transcript: str = "", asr_prompt: str = "",
          audio_quality: dict | None = None, denoise: dict | None = None,
          captured_at: float | None = None) -> dict:
    """Assemble the capture-time chain for one utterance. Seeds the correction log
    with the source-side changes already applied (ASR bias, denoise) so the log is
    complete from t=0; later stages append to it. Kept lean — the full audio_quality
    dict already lives in meta["audio_quality"], so only its headline is copied here."""
    chain: dict = {
        "captured_at": round(captured_at if captured_at is not None else time.time(), 3),
        "raw_audio": raw_audio or "",
        "enhanced_audio": enhanced_audio or "",
        "transcript": transcript or "",
        "asr_prompt": (asr_prompt or "")[:600],
        "corrections": [],
    }
    if isinstance(audio_quality, dict):
        chain["capture_quality"] = audio_quality.get("quality")
        chain["snr_est"] = audio_quality.get("snr_est")
    corrs: list[dict] = []
    if asr_prompt:
        corrs.append(correction(ASR_BIAS, note="known-vocabulary spelling bias",
                                ts=chain["captured_at"]))
    if isinstance(denoise, dict) and denoise.get("applied"):
        after = ""
        if denoise.get("snr_after") is not None:
            after = f"{chain.get('snr_est')}→{denoise.get('snr_after')}dB"
        corrs.append(correction(
            DENOISE, after=after,
            note=f"backend={denoise.get('backend', '?')}", ts=chain["captured_at"]))
    chain["corrections"] = corrs
    return chain


def append_correction(event_id: int, stage: str, *, before: str = "",
                      after: str = "", note: str = "", ts: float | None = None,
                      store=None) -> bool:
    """Append a correction to a STORED event's provenance chain (keyed by id). Used
    by stages that run after capture — the phone approval gate, a human fact edit.
    Best-effort: returns False (never raises) if the event/store is unavailable."""
    if not event_id:
        return False
    try:
        if store is None:
            from app.storage import get_store
            store = get_store()
        return store.append_provenance_correction(
            int(event_id), correction(stage, before=before, after=after,
                                      note=note, ts=ts))
    except Exception as exc:
        print(f"[provenance] append skipped ({exc}).")
        return False


def chain_for(event_id: int, store=None) -> dict | None:
    """Read the full provenance chain for a stored event, or None if absent."""
    try:
        if store is None:
            from app.storage import get_store
            store = get_store()
        ev = store.by_ids_map([int(event_id)]).get(int(event_id))
        if ev is None:
            return None
        meta = ev.meta if isinstance(ev.meta, dict) else {}
        return meta.get("provenance")
    except Exception as exc:
        print(f"[provenance] read skipped ({exc}).")
        return None


def summary(chain: dict | None) -> dict:
    """Compact headline for a console row: how deep the chain is, at a glance."""
    if not isinstance(chain, dict):
        return {}
    corrs = chain.get("corrections") or []
    return {
        "n_corrections": len(corrs),
        "has_enhanced": bool(chain.get("enhanced_audio")),
        "stages": [c.get("stage") for c in corrs],
    }


def render(chain: dict | None) -> str:
    """Human-readable evidence trail — for the Console / an audit view."""
    if not isinstance(chain, dict):
        return "(no provenance)"
    lines: list[str] = []
    if chain.get("raw_audio"):
        lines.append(f"raw audio: {chain['raw_audio']}")
    if chain.get("enhanced_audio"):
        lines.append(f"enhanced audio: {chain['enhanced_audio']}")
    if chain.get("capture_quality"):
        snr = chain.get("snr_est")
        lines.append(f"capture: {chain['capture_quality']}"
                     + (f" ({snr}dB SNR)" if snr is not None else ""))
    if chain.get("transcript"):
        lines.append(f"transcript: “{chain['transcript']}”")
    corrs = chain.get("corrections") or []
    if corrs:
        lines.append("corrections:")
        for c in corrs:
            label = _STAGE_LABEL.get(c.get("stage"), c.get("stage", "?"))
            change = ""
            if c.get("before") or c.get("after"):
                change = f" “{c.get('before', '')}” → “{c.get('after', '')}”"
            note = f" — {c['note']}" if c.get("note") else ""
            lines.append(f"  • {label}{change}{note}")
    else:
        lines.append("corrections: none (verbatim as captured)")
    return "\n".join(lines)
