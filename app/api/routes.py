"""HTTP surface — the API your eventual pen will talk to.

    POST /audio/start   start the live audio pipeline
    POST /audio/stop    stop it
    GET  /memory        Memory Console (HTML)
    GET  /memory/events dump the timeline (JSON)
    GET  /memory/search search memory
    GET  /chat          Chat UI (HTML); POST /chat dispatches a turn
    POST /chat          dispatch a turn to the browser agent (hear -> act)
    POST /chat/attach   upload a document/photo into memory (+ context snippet)
    GET  /chat/poll     tail agent progress / results / approval prompts
    POST /chat/answer   answer a pending ask_human / approval prompt
    POST /chat/new      archive live chat (if any) and start a fresh conversation
    GET  /chat/sessions list archived chat conversations (newest first)
    GET  /chat/sessions/{id}  load one archived conversation
    POST /chat/outcome  label an escalated chat answer (👍/👎/✏️ → distill row)
    POST /speak         TTS
    GET  /speak/status  TTS mute / enabled
    POST /speak/mute    mute or unmute AI voice
    GET  /vision, ...   (stubs)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import (APIRouter, File, Form, Header, HTTPException, Query, Request,
                     UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               Response)
from pydantic import BaseModel

from app.api.mnemos_theme import apply as _mnemos
from app.config import settings
from app.services import agent_bridge as agent
from app.services import llm, voice
from app.services.audio import AudioPipeline
from app.services.desktop_capture import DesktopCapturePipeline
from app.services.memory import memory
from app.services.notifications import NotificationPipeline
from app.services.vision import VisionPipeline

router = APIRouter()


def _agent_worker():
    if _agent_disabled():
        return None
    try:
        return agent.worker
    except Exception:
        return None


def _html_with_approval(page: str, *, next_url: str) -> HTMLResponse:
    """SSR approval banner into a themed HTML page (no-JS Yes/No forms)."""
    from app.api.approval_partial import inject_page
    return HTMLResponse(
        inject_page(page, next_url=next_url, agent_worker=_agent_worker())
    )


def _agent_disabled() -> bool:
    """Escape hatch: QUILL_AGENT=0 keeps /chat as the memory-only retriever."""
    import os

    return os.environ.get("QUILL_AGENT") in ("0", "false", "False")

# One shared pipeline instance for the server process.
_audio = AudioPipeline()
_audio_running = False
# System-audio (loopback) twin: meeting/call audio, provenanced as
# source=audio.system. Created lazily after consent — it records the other
# participants, so it must never start from a bare import-time flag alone.
_system_audio: AudioPipeline | None = None
_system_audio_running = False
_vision = VisionPipeline()
_vision_running = False
_notifications = NotificationPipeline()
_notifications_running = False
_desktop_capture = DesktopCapturePipeline()
_desktop_capture_running = False


def _ensure_system_audio() -> AudioPipeline:
    """Lazily build the loopback pipeline once consent/env allows it."""
    global _system_audio
    if _system_audio is None:
        _system_audio = AudioPipeline(
            capture="loopback", source="audio.system",
            skip_source="audio.system.skipped",
            device=settings.system_audio.device,
        )
    return _system_audio


def _consent_allows(source: str) -> bool:
    from app.services import capture_consent
    return capture_consent.allows(source)


def _require_consent(source: str) -> None:
    if not _consent_allows(source):
        raise HTTPException(
            status_code=403,
            detail=(f"Capture source '{source}' is off until Privacy consent. "
                    "Open the recording controls and opt in."),
        )


def _l0():
    """The perception L0 metadata monitor, or None when disabled/broken. L0
    rides the 'screen' consent + pause plumbing: it records window METADATA
    (titles + input counts, never contents), so it must obey exactly the same
    user controls as the pixels."""
    if not settings.perception.enabled:
        return None
    try:
        from app.perception.l0_meta import monitor
        return monitor
    except Exception as exc:
        print(f"[perception] L0 unavailable ({exc}).")
        return None


def start_all(audio: bool = False, vision: bool = False, notifications: bool = True,
              desktop_capture: bool = False, system_audio: bool = False) -> dict:
    """Start capture pipelines in-process (launcher / consent apply).

    Defaults are OFF for A/V — callers must pass explicit True for each source.
    """
    global _audio_running, _system_audio_running, _vision_running, \
        _notifications_running, _desktop_capture_running
    if audio and not _audio_running:
        try:
            _audio.start()
            _audio_running = True
        except Exception as exc:
            print(f"[launch] audio failed to start: {exc}")
    if system_audio and not _system_audio_running:
        try:
            _ensure_system_audio().start()
            _system_audio_running = True
        except Exception as exc:
            print(f"[launch] system audio failed to start ({exc}). "
                  "pip install soundcard, or leave system_audio off.")
    if vision and not _vision_running:
        try:
            _vision.start()
            _vision_running = True
        except Exception as exc:
            print(f"[launch] vision failed to start ({exc}). "
                  "Set QUILL_CAMERA_INDEX or leave webcam off.")
    if notifications and settings.notifications.enabled and not _notifications_running:
        try:
            _notifications.start()
            _notifications_running = True
        except Exception as exc:
            print(f"[launch] notifications failed to start: {exc}")
    if desktop_capture and not _desktop_capture_running:
        try:
            _desktop_capture.start()
            _desktop_capture_running = True
        except Exception as exc:
            print(f"[launch] desktop capture failed to start ({exc}). "
                  "Install pynput/mss, or leave screen capture off.")
    if desktop_capture:
        mon = _l0()
        if mon is not None:
            try:
                mon.start()
            except Exception as exc:
                print(f"[launch] perception L0 failed to start ({exc}).")
    return {
        "audio_running": _audio_running,
        "system_audio_running": _system_audio_running,
        "vision_running": _vision_running,
        "notifications_running": _notifications_running,
        "desktop_capture_running": _desktop_capture_running,
    }


def stop_all() -> None:
    global _audio_running, _system_audio_running, _vision_running, \
        _notifications_running, _desktop_capture_running
    if _audio_running:
        _audio.stop(); _audio_running = False
    if _system_audio_running and _system_audio is not None:
        _system_audio.stop(); _system_audio_running = False
    if _vision_running:
        _vision.stop(); _vision_running = False
    if _notifications_running:
        _notifications.stop(); _notifications_running = False
    if _desktop_capture_running:
        _desktop_capture.stop(); _desktop_capture_running = False
    mon = _l0()
    if mon is not None and mon.running():
        mon.stop()


def _running_map() -> dict:
    return {
        "mic": _audio_running,
        "webcam": _vision_running,
        "screen": _desktop_capture_running,
        "system_audio": _system_audio_running,
        "notifications": _notifications_running,
    }


def _pause_source(source: str) -> dict:
    """Stop one capture source (one-click pause). Consent stays on disk."""
    global _audio_running, _system_audio_running, _vision_running, \
        _desktop_capture_running
    if source == "mic":
        if _audio_running:
            _audio.stop(); _audio_running = False
    elif source == "system_audio":
        if _system_audio_running and _system_audio is not None:
            _system_audio.stop(); _system_audio_running = False
    elif source == "webcam":
        if _vision_running:
            _vision.stop(); _vision_running = False
    elif source == "screen":
        if _desktop_capture_running:
            _desktop_capture.stop(); _desktop_capture_running = False
        mon = _l0()
        if mon is not None and mon.running():
            # pause() (not stop): writes the gap(reason='user_pause') row so
            # the timeline says WHY the stream went quiet.
            mon.pause()
    else:
        raise HTTPException(400, detail=f"unknown source: {source}")
    return {"paused": source, "running": _running_map()}


def _resume_source(source: str) -> dict:
    """Resume one source if consent still allows it."""
    _require_consent(source)
    global _audio_running, _system_audio_running, _vision_running, \
        _desktop_capture_running
    if source == "mic":
        if not _audio_running:
            _audio.start(); _audio_running = True
    elif source == "system_audio":
        if not _system_audio_running:
            _ensure_system_audio().start(); _system_audio_running = True
    elif source == "webcam":
        if not _vision_running:
            _vision.start(); _vision_running = True
    elif source == "screen":
        if not _desktop_capture_running:
            _desktop_capture.start(); _desktop_capture_running = True
        mon = _l0()
        if mon is not None and not mon.running():
            mon.resume()          # closes the user_pause gap
    else:
        raise HTTPException(400, detail=f"unknown source: {source}")
    return {"resumed": source, "running": _running_map()}


def _apply_consent_runtime(sources: dict) -> dict:
    """Start consented sources / stop ones turned off after a consent save."""
    global _desktop_capture_running
    # Mic
    if sources.get("mic"):
        if not _audio_running:
            try:
                _resume_source("mic")
            except Exception as exc:
                print(f"[capture] mic start failed: {exc}")
    else:
        _pause_source("mic")
    # System audio
    if sources.get("system_audio"):
        if not _system_audio_running:
            try:
                _resume_source("system_audio")
            except Exception as exc:
                print(f"[capture] system_audio start failed: {exc}")
    else:
        _pause_source("system_audio")
    # Webcam
    if sources.get("webcam"):
        if not _vision_running:
            try:
                _resume_source("webcam")
            except Exception as exc:
                print(f"[capture] webcam start failed: {exc}")
    else:
        _pause_source("webcam")
    # Screen frames and/or mouse clicks share the desktop capture pipeline.
    want_desktop = bool(sources.get("screen") or sources.get("clicks"))
    if want_desktop:
        # Restart so screen/clicks sub-flags from consent take effect.
        if _desktop_capture_running:
            try:
                _desktop_capture.stop()
            except Exception:
                pass
            _desktop_capture_running = False
        try:
            _desktop_capture.start()
            _desktop_capture_running = True
        except Exception as exc:
            print(f"[capture] desktop start failed: {exc}")
    else:
        _pause_source("screen")
    return _running_map()


@router.get("/health")
def health() -> dict:
    from app.services import capture_consent
    from app.services import score_v2
    from app.services import source_policy
    return {
        "status": "ok",
        "source_policies": {
            "loaded": source_policy.policies_loaded(),
            "version": source_policy.policy_version(),
        },
        "score_v2": score_v2.health(),
        "audio_running": _audio_running,
        "system_audio_running": _system_audio_running,
        "vision_running": _vision_running,
        "notifications_running": _notifications_running,
        "desktop_capture_running": _desktop_capture_running,
        "desktop_capture": _desktop_capture.running(),
        "capture_consent": capture_consent.status(),
        "running": _running_map(),
    }


class CaptureConsentBody(BaseModel):
    mic: bool | None = None
    webcam: bool | None = None
    screen: bool | None = None
    clicks: bool | None = None
    system_audio: bool | None = None
    save_audio: bool | None = None
    # True = stamp consent (default when any source is set). False = revoke all.
    consented: bool | None = None


class CaptureSourceBody(BaseModel):
    source: str  # mic | webcam | screen | system_audio


@router.get("/capture/status")
def capture_status() -> dict:
    """Recording indicator payload: consent + live sources."""
    from app.services import capture_consent
    meeting = {}
    meeting_session = {}
    try:
        from app.services import meeting_mode as _mm
        meeting = _mm.status()
    except Exception:
        meeting = {}
    try:
        from app.services import meeting_session as _ms
        meeting_session = _ms.status()
    except Exception:
        meeting_session = {}
    return {
        "consent": capture_consent.status(),
        "running": _running_map(),
        "save_audio": bool(settings.storage.save_audio),
        "meeting_mode": meeting,
        "meeting_session": meeting_session,
    }


class MeetingSessionDecideBody(BaseModel):
    choice: str  # skip | transcript_only | keep_receipts
    session_id: int | None = None


@router.post("/meeting/session/decide")
def meeting_session_decide(body: MeetingSessionDecideBody) -> dict:
    """Per-meeting consent without going through the yes/no offer queue."""
    from app.services import meeting_session as _ms
    return _ms.decide(body.choice, session_id=body.session_id)


@router.get("/capture/consent")
def capture_consent_get() -> dict:
    from app.services import capture_consent
    return capture_consent.status()


@router.post("/capture/consent")
def capture_consent_set(body: CaptureConsentBody) -> dict:
    """Explicit in-UI opt-in. Starts/stops pipelines to match the allow-list."""
    from app.services import capture_consent
    patch = {}
    for key in capture_consent.SOURCES:
        val = getattr(body, key, None)
        if val is not None:
            patch[key] = bool(val)
    if body.consented is False:
        state = capture_consent.save(consented=False)
        stop_all()
        # Keep notifications if they were up — stop_all clears them; restart.
        if settings.notifications.enabled:
            try:
                start_all(notifications=True)
            except Exception:
                pass
        return {"consent": state, "running": _running_map()}
    state = capture_consent.save(
        patch or None,
        consented=True if body.consented is None else bool(body.consented),
    )
    running = _apply_consent_runtime(state.get("sources") or {})
    return {"consent": state, "running": running}


@router.post("/capture/pause")
def capture_pause(body: CaptureSourceBody) -> dict:
    """One-click per-source pause (indicator). Consent is unchanged."""
    return _pause_source(body.source)


@router.post("/capture/resume")
def capture_resume(body: CaptureSourceBody) -> dict:
    """Resume a previously consented source."""
    return _resume_source(body.source)


# ---------------------------------------------------------------------------
# Perception Phase A — live indicator, recent view, erasure, blocklist
# ---------------------------------------------------------------------------

class PerceptionEraseBody(BaseModel):
    """Erase desktop perception traces in [ts_start_ms, ts_end_ms)."""
    ts_start_ms: int
    ts_end_ms: int


class PerceptionBlocklistBody(BaseModel):
    """Add/remove a user privacy-blocklist rule."""
    kind: str          # titles | apps | domains
    value: str


@router.get("/perception/status")
def perception_status() -> dict:
    """Live indicator: capturing / paused + L0 + spend + coverage snapshot."""
    from app.services import capture_consent
    mon = _l0()
    l0 = (mon.status() if mon is not None
          else {"running": False, "paused": False, "session_id": "",
                "seq": 0, "pending": 0, "last_emit_ms": None})
    capturing = bool(_desktop_capture_running or l0.get("running"))
    paused = bool(l0.get("paused")) or (
        _consent_allows("screen") and not capturing)
    spend = {}
    coverage = {}
    counts = {}
    try:
        from app.perception.spend_cap import spend_cap
        spend = spend_cap.status()
    except Exception as exc:
        spend = {"ok": False, "error": str(exc)}
    try:
        from app.perception.schemas import now_ms
        from app.perception.store import get_pstore
        ps = get_pstore()
        end = now_ms()
        coverage = ps.coverage(end - 24 * 3600 * 1000, end)
        counts = ps.counts()
    except Exception as exc:
        coverage = {"error": str(exc)}
    return {
        "enabled": bool(settings.perception.enabled),
        "capturing": capturing,
        "paused": paused,
        "consent_screen": _consent_allows("screen"),
        "consent": capture_consent.status(),
        "running": _running_map(),
        "l0": l0,
        "spend": spend,
        "coverage_24h": coverage,
        "counts": counts,
    }


@router.get("/perception/recent")
def perception_recent(
    minutes: int = Query(30, ge=1, le=24 * 60),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    """What was captured in the last N minutes (meta + exclusions + gaps)."""
    from app.perception.schemas import now_ms
    from app.perception.store import get_pstore
    since = now_ms() - minutes * 60 * 1000
    ps = get_pstore()
    return {
        "since_ms": since,
        "minutes": minutes,
        "meta_events": ps.recent_meta(since, limit=limit),
        "captures": ps.recent_captures(since, limit=limit),
        "gaps": ps.list_gaps(since_ms=since, limit=limit),
    }


@router.post("/perception/erase")
def perception_erase(body: PerceptionEraseBody) -> dict:
    """Cascading erasure across SQLite / LanceDB / frames / distill / Parquet."""
    if body.ts_end_ms <= body.ts_start_ms:
        raise HTTPException(400, detail="ts_end_ms must be > ts_start_ms")
    # Guard against accidental full-history wipes from a bad client clock.
    if body.ts_end_ms - body.ts_start_ms > 90 * 24 * 3600 * 1000:
        raise HTTPException(400, detail="erase window may not exceed 90 days")
    from app.perception.erasure import erase_window
    try:
        manifest = erase_window(body.ts_start_ms, body.ts_end_ms)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"ok": True, "manifest": manifest}


@router.get("/perception/blocklist")
def perception_blocklist_get() -> dict:
    """Builtin + user-editable privacy blocklist rules."""
    from app.perception.privacy_gate import gate
    return gate.list_rules()


@router.post("/perception/blocklist")
def perception_blocklist_add(body: PerceptionBlocklistBody) -> dict:
    from app.perception.privacy_gate import gate
    try:
        return gate.add_user_rule(body.kind, body.value)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.delete("/perception/blocklist")
def perception_blocklist_remove(
    kind: str = Query(..., pattern="^(titles|apps|domains)$"),
    value: str = Query(..., min_length=1),
) -> dict:
    from app.perception.privacy_gate import gate
    try:
        return gate.remove_user_rule(kind, value)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


class PerceptionPinBody(BaseModel):
    capture_id: str


@router.post("/perception/pin")
def perception_pin(body: PerceptionPinBody) -> dict:
    """Promote a capture so its full frame survives the 72h age drop."""
    from app.perception.schemas import SupervisionEvent, now_ms
    from app.perception.store import get_pstore
    ps = get_pstore()
    if ps.get_capture(body.capture_id) is None:
        raise HTTPException(404, detail="unknown capture_id")
    ps.set_promoted(body.capture_id, True)
    ps.add_salience(body.capture_id, 1.0,
                    features={"source": "pin"}, model_version="pin-v1")
    ps.add_supervision(SupervisionEvent(
        ts_utc=now_ms(), kind="pin", target_type="capture",
        target_id=body.capture_id))
    return {"ok": True, "capture_id": body.capture_id, "promoted": True}


@router.post("/perception/unpin")
def perception_unpin(body: PerceptionPinBody) -> dict:
    from app.perception.schemas import SupervisionEvent, now_ms
    from app.perception.store import get_pstore
    ps = get_pstore()
    if ps.get_capture(body.capture_id) is None:
        raise HTTPException(404, detail="unknown capture_id")
    ps.set_promoted(body.capture_id, False)
    ps.add_supervision(SupervisionEvent(
        ts_utc=now_ms(), kind="unpin", target_type="capture",
        target_id=body.capture_id))
    return {"ok": True, "capture_id": body.capture_id, "promoted": False}


@router.post("/perception/compact")
def perception_compact() -> dict:
    """Run the L2 age/budget compactor now (also scheduled as a worker job)."""
    from app.perception.compactor import compact
    return {"ok": True, "manifest": compact()}


@router.post("/audio/start")
def audio_start() -> dict:
    """Start mic (and system-audio only if that source is also consented)."""
    _require_consent("mic")
    global _audio_running, _system_audio_running
    if not _audio_running:
        _audio.start()
        _audio_running = True
    if _consent_allows("system_audio") and not _system_audio_running:
        try:
            _ensure_system_audio().start()
            _system_audio_running = True
        except Exception as exc:
            print(f"[audio] system audio start skipped ({exc})")
    return {"audio_running": _audio_running,
            "system_audio_running": _system_audio_running}


@router.post("/audio/stop")
def audio_stop() -> dict:
    global _audio_running, _system_audio_running
    if _audio_running:
        _audio.stop()
        _audio_running = False
    if _system_audio_running and _system_audio is not None:
        _system_audio.stop()
        _system_audio_running = False
    return {"audio_running": _audio_running,
            "system_audio_running": _system_audio_running}


@router.post("/system-audio/start")
def system_audio_start() -> dict:
    _require_consent("system_audio")
    global _system_audio_running
    if not _system_audio_running:
        _ensure_system_audio().start()
        _system_audio_running = True
    return {"system_audio_running": _system_audio_running}


@router.post("/system-audio/stop")
def system_audio_stop() -> dict:
    global _system_audio_running
    if _system_audio_running and _system_audio is not None:
        _system_audio.stop()
        _system_audio_running = False
    return {"system_audio_running": _system_audio_running}


@router.post("/vision/start")
def vision_start() -> dict:
    _require_consent("webcam")
    global _vision_running
    if not _vision_running:
        _vision.start()
        _vision_running = True
    return {"vision_running": _vision_running}


@router.post("/vision/stop")
def vision_stop() -> dict:
    global _vision_running
    if _vision_running:
        _vision.stop()
        _vision_running = False
    return {"vision_running": _vision_running}


@router.post("/notifications/start")
def notifications_start() -> dict:
    """Start Windows toast capture (Phone Link / iPhone notifications)."""
    global _notifications_running
    if not _notifications_running:
        _notifications.start()
        _notifications_running = True
    return {"notifications_running": _notifications_running}


@router.post("/notifications/stop")
def notifications_stop() -> dict:
    global _notifications_running
    if _notifications_running:
        _notifications.stop()
        _notifications_running = False
    return {"notifications_running": _notifications_running}


@router.post("/desktop-capture/start")
def desktop_capture_start() -> dict:
    """Start passive desktop screen + click observation (requires consent)."""
    _require_consent("screen")
    global _desktop_capture_running
    if not _desktop_capture_running:
        _desktop_capture.start()
        _desktop_capture_running = True
    return {"desktop_capture_running": _desktop_capture_running,
            "desktop_capture": _desktop_capture.running()}


@router.post("/desktop-capture/stop")
def desktop_capture_stop() -> dict:
    global _desktop_capture_running
    if _desktop_capture_running:
        _desktop_capture.stop()
        _desktop_capture_running = False
    return {"desktop_capture_running": _desktop_capture_running}


@router.get("/memory/events")
def memory_all() -> dict:
    """JSON timeline dump (formerly GET /memory — that path is now the HTML console)."""
    return {"events": memory.all()}


@router.get("/memory/search")
def memory_search(q: str = "", limit: int = 20, modality: str | None = None) -> dict:
    return {"query": q, "results": memory.search(q, limit, modality)}


# --- Memory Console --------------------------------------------------------
# A read-only window onto the timeline: recent captures, search, speaker labels,
# confidence, and — critically — a link from every memory back to its source
# audio clip or frame (provenance). This is the trust/training layer: you can
# see what Mnemos heard and saw, and judge what's good, low-confidence, or junk.

def _console_row(d: dict) -> dict:
    """Flatten an event dict into the compact shape the console renders."""
    meta = d.get("meta") or {}
    quality = meta.get("quality") or {}
    speaker = ""
    spk = meta.get("speaker")
    spk_profile = spk.get("environment_profile") if isinstance(spk, dict) else None
    if isinstance(spk, dict):
        speaker = spk.get("name") or spk.get("label") or ""
    if not speaker and d.get("people"):
        speaker = d["people"][0]
    text = d.get("summary") or d.get("raw") or ""
    # Audio-only events (#7 store_audio_only) carry no trusted text — show the
    # shaky transcript, if any, so the clip is still inspectable.
    if not text and meta.get("asr_text"):
        text = meta["asr_text"]
    # #6: utterance type (command/dictation) — only surface the non-default types
    # as a tag; conversation/noise are the norm and don't need a badge.
    ut = meta.get("utterance_type") or {}
    ut_type = ut.get("type") if ut.get("type") in ("command", "dictation") else None
    # #12: provenance chain — surface the evidence trail inline (summary + rendered
    # detail + the enhanced-audio path, so the console can play what Whisper heard).
    prov = meta.get("provenance")
    prov_sum = None
    prov_detail = None
    if isinstance(prov, dict):
        from app.services import provenance as _prov
        prov_sum = _prov.summary(prov)
        prov_detail = _prov.render(prov)
    # Vision routing telemetry: which VLM produced this description and why the
    # router picked it — lets the console validate local-first routing at a glance.
    vis = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    route = vis.get("_route") if isinstance(vis.get("_route"), dict) else {}
    return {
        "id": d.get("id"),
        "time": d.get("time"),
        "modality": d.get("modality"),
        "source": d.get("source"),
        "window": meta.get("window"),
        "text": text,
        "speaker": speaker,
        "speaker_profile": spk_profile,
        "utterance_type": ut_type,
        "provenance": prov_sum,
        "provenance_detail": prov_detail,
        "enhanced_audio": meta.get("enhanced_audio_path"),
        "confidence": d.get("confidence"),
        "low_confidence": bool(quality.get("low_confidence")),
        "needs_review": bool(quality.get("needs_review")) or bool(meta.get("needs_review")),
        "skipped": meta.get("skipped"),
        "quality_reason": quality.get("reason"),
        "no_speech_prob": quality.get("no_speech_prob"),
        "audio_path": meta.get("audio_path"),
        "frame_path": meta.get("frame_path"),
        "vision_provider": vis.get("_provider"),
        "vision_route": route.get("reason") or (
            f"route conf {route.get('confidence')}"
            if route.get("confidence") is not None else None),
        "score": d.get("score"),
    }


@router.get("/console/events")
def console_events(
    q: str = "",
    limit: int = 200,
    modality: str | None = None,
    source: str | None = None,
    low_only: bool = False,
) -> dict:
    """Feed for the console: newest first, with optional search / modality /
    source-prefix / low-confidence filters. `source` is a prefix match so
    e.g. source=desktop. spans desktop.screen and desktop.click."""
    if q.strip():
        rows = memory.search(q, limit=limit, modality=modality)
        if source:
            rows = [r for r in rows if (r.get("source") or "").startswith(source)]
    else:
        rows = memory.all()
        if modality:
            rows = [r for r in rows if r.get("modality") == modality]
        if source:
            rows = [r for r in rows if (r.get("source") or "").startswith(source)]
        rows = rows[-limit:]
    out = [_console_row(r) for r in rows]
    if low_only:
        out = [r for r in out if r["low_confidence"]]
    out.sort(key=lambda r: r["time"] or 0, reverse=True)
    return {"count": len(out), "total": len(memory.all()), "events": out}


@router.get("/artifact")
def artifact(path: str = Query(..., description="absolute path to a stored clip/frame")):
    """Serve a stored audio clip or frame. Path is confined to the data dir so a
    crafted `path` can't read arbitrary files off disk."""
    data_root = Path(settings.storage.data_dir).resolve()
    try:
        target = Path(path).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="bad path")
    if data_root not in target.parents and target != data_root:
        raise HTTPException(status_code=403, detail="outside data dir")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media = {".wav": "audio/wav", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".png": "image/png"}.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(str(target), media_type=media)


@router.get("/console/models")
def console_models() -> dict:
    """Model-call telemetry: per (task, provider, model) calls, latency, and
    estimated cost — the measure of what local-first vision saves vs Claude.
    Includes privacy egress summary (plan 6.2)."""
    from app.services.model_log import model_log

    return model_log.stats()


@router.get("/console/egress")
def console_egress(recent: int = 40) -> dict:
    """Plan 6.2 — auditable 'what left the machine' inventory.

    Recent external model calls with privacy_max (highest class sent or
    refused), plus class histogram. Complements /console/models spend view.
    """
    from app.services.model_log import model_log

    return model_log.egress_inventory(recent=max(0, min(int(recent), 200)))


@router.get("/console/escalate")
def console_escalate(recent: int = 20) -> dict:
    """Local→parent VLM escalation distill trail (counts by reason + recent rows).

    Written when Claude is invoked after a local attempt (or local unavailable).
    See data/escalate_distill.jsonl / QUILL_ESCALATE_LOG.
    """
    from app.services.escalate_log import escalate_log

    return escalate_log.stats(recent=max(0, min(int(recent), 100)))


# --- learning loop (Workstream A): the user-trust surface -------------------
@router.get("/learning/pairs")
def learning_pairs_list(task_type: str = "", limit: int = 200) -> dict:
    """Recent learning pairs, newest first, optionally filtered by task_type.
    This is the Learning tab's table — what Mnemos harvested and from where."""
    store = memory._ensure_store()
    rows = store.list_learning_pairs(
        task_type=task_type or None, limit=max(1, min(int(limit), 500)))
    return {"count": len(rows), "pairs": rows}


@router.delete("/learning/pairs/{pair_id}")
def learning_pair_delete(pair_id: str) -> dict:
    """Hard delete one pair (cascades to its exemplar). Invariant 4 applied to
    data: everything harvested is user-visible and user-deletable."""
    from app.services import learning_store
    ok = learning_store.delete(pair_id, store=memory._ensure_store())
    if not ok:
        raise HTTPException(status_code=404, detail=f"no learning pair {pair_id}")
    return {"ok": True, "pair_id": pair_id}


@router.post("/learning/pairs/{pair_id}/confirm")
def learning_pair_confirm(pair_id: str) -> dict:
    """Promote a shadow-derived pair to human-confirmed (B.4 review card)."""
    from app.services import learning_store
    ok = learning_store.confirm(pair_id, store=memory._ensure_store())
    if not ok:
        raise HTTPException(status_code=404, detail=f"no learning pair {pair_id}")
    return {"ok": True, "pair_id": pair_id, "human_confirmed": True}


@router.get("/learning/stats")
def learning_stats() -> dict:
    """Counter widget: pairs collected this week / total, by task_type."""
    from app.services import learning_store
    from app.config import settings as _settings
    return {"enabled": bool(_settings.learning.enabled),
            **learning_store.counts(store=memory._ensure_store())}


@router.get("/learning/router")
def learning_router_report() -> dict:
    """Escalation-router weekly report (D.5): router vs heuristic — the
    evidence shown before offering QUILL_ROUTER=active."""
    from app.services.escalation_router import escalation_router
    return escalation_router.report()


@router.get("/learning/shadow")
def learning_shadow(days: int = 7) -> dict:
    """Shadow-eval weekly rollup (B.5): agreement rate by task, top reason
    codes, budget spent — the measurement that justifies/kills per-type LoRA."""
    from app.services import shadow_eval
    return shadow_eval.report(days=max(1, min(int(days), 30)))


@router.post("/learning/shadow/run")
def learning_shadow_run() -> dict:
    """Manual trigger (the nightly path is the idle scheduler)."""
    from app.services import shadow_eval
    if not shadow_eval.enabled():
        raise HTTPException(status_code=404,
                            detail="shadow eval is disabled (QUILL_SHADOW_EVAL=1)")
    return shadow_eval.run_nightly()


@router.get("/learning/exemplars")
def learning_exemplars(task_type: str = "", limit: int = 200) -> dict:
    """'What Mnemos has learned' — exemplars by type, with use counts (C.6)."""
    from app.services.exemplar_store import exemplar_store
    return {"stats": exemplar_store.stats(),
            "rows": exemplar_store.list_rows(
                task_type=task_type or None,
                limit=max(1, min(int(limit), 500)))}


@router.delete("/learning/exemplars/{exemplar_id}")
def learning_exemplar_delete(exemplar_id: str) -> dict:
    """Delete one exemplar (its learning pair stays; re-confirming re-mints)."""
    from app.services.exemplar_store import exemplar_store
    exemplar_store.delete(exemplar_id)
    return {"ok": True, "exemplar_id": exemplar_id}


class ExemplarGateBody(BaseModel):
    task_type: str          # a task_type, or "_all" for the kill switch
    off: bool


@router.post("/learning/exemplars/gate")
def learning_exemplar_gate(body: ExemplarGateBody) -> dict:
    """Console gate: per-type off switch; task_type='_all' mirrors
    QUILL_EXEMPLARS=0 at runtime without editing .env (invariant 4)."""
    from app.services.exemplar_store import exemplar_store
    if not (body.task_type or "").strip():
        raise HTTPException(status_code=400, detail="task_type required")
    gates = exemplar_store.set_gate(body.task_type.strip(), bool(body.off),
                                    reason="console")
    return {"ok": True, "gates": gates}


@router.get("/console/readiness")
def console_readiness(limit: int = 100) -> dict:
    """Unified action-readiness (#10) across the open-task board: each task's
    single risk-aware score + decision band (auto/offer/review/hold), plus the
    band histogram. One consistent view of 'what's ready to act on' vs 'keep as a
    reviewable item' — the same score the offer gate keys off."""
    from app.services.readiness import for_fact

    store = memory._ensure_store()
    facts = store.list_facts(kind="task", status="open", limit=limit,
                             actionable=True)
    bands = {"auto": 0, "offer": 0, "review": 0, "hold": 0}
    items = []
    for f in facts:
        v = for_fact(f)
        bands[v.band] = bands.get(v.band, 0) + 1
        items.append({"fact_id": f.get("fact_id"), "text": f.get("text"),
                      "score": v.score, "band": v.band, "risk": v.risk,
                      "confidence": f.get("confidence"), "review": f.get("review")})
    items.sort(key=lambda x: x["score"], reverse=True)
    return {"bands": bands, "count": len(items), "items": items}


@router.get("/console/cognition")
def console_cognition() -> dict:
    """Cognition telemetry (#9): the trust rates model/audio logs don't capture —
    fact-faithfulness (hallucinated-span rate), source-grounding (packets citing
    a real DB fact vs a model paraphrase), and the proactive-offer surfaced rate
    ('getting chatty'). Session aggregate; the durable trail is data/cognition.jsonl."""
    from app.services.cog_telemetry import cog_telemetry

    return cog_telemetry.rates()


@router.get("/triggers", response_class=HTMLResponse)
def triggers_ui() -> HTMLResponse:
    """Standing-triggers management page (pause/resume/retire/adopt)."""
    from app.api.triggers_page import TRIGGERS_PAGE

    return HTMLResponse(TRIGGERS_PAGE)


@router.get("/triggers/list")
def triggers_list() -> dict:
    """Standing triggers: every row (active/suggested/paused/retired) with its
    per-trigger stats (fires/offers/accepts/dismisses), the signal catalog, and
    the last engine pass — the JSON behind /triggers."""
    from app.services import triggers

    return triggers.status(memory._ensure_store())


@router.post("/triggers/run")
def triggers_run(surface: bool = False) -> dict:
    """One engine pass now. Default is a dry-run (returns the would-be offer
    without interrupting); surface=true actually offers."""
    from app.services import triggers

    return triggers.run_once(memory._ensure_store(), surface=surface)


@router.post("/triggers/{trigger_id}/status")
def triggers_set_status(trigger_id: int, status: str) -> dict:
    """Pause / resume / retire / adopt a trigger. `status` is one of
    active|paused|retired (suggested rows adopt by setting active)."""
    from app.services import triggers as _t  # noqa: F401 (feature gate lives there)
    import time as _time

    store = memory._ensure_store()
    if store.get_trigger(trigger_id) is None:
        raise HTTPException(status_code=404, detail=f"no trigger {trigger_id}")
    try:
        ok = store.set_trigger_status(trigger_id, status, _time.time())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": ok, "trigger": store.get_trigger(trigger_id)}


@router.post("/triggers/backtest")
def triggers_backtest(body: dict) -> dict:
    """Dry-run a condition against the trailing week: {signal, condition?} ->
    would-have-fired moments. The console twin of the chat draft card."""
    from app.services.triggers import authoring
    from app.services.triggers.signals import CATALOG

    if (body or {}).get("signal") not in CATALOG:
        raise HTTPException(status_code=400,
                            detail=f"signal must be one of {sorted(CATALOG)}")
    return authoring.backtest(memory._ensure_store(), body)


@router.get("/console/provenance/{event_id}")
def console_provenance(event_id: int) -> dict:
    """The full provenance chain for one utterance (#12): raw -> enhanced ->
    transcript -> the ordered correction log. Addressable by event id — reachable
    from a fact's source_event_id or a turn's event_ids — so any fact can be traced
    to the exact sound it came from and every fix applied along the way."""
    from app.services import provenance as _prov

    chain = _prov.chain_for(event_id, store=memory._ensure_store())
    if chain is None:
        raise HTTPException(status_code=404,
                            detail=f"no provenance for event {event_id}")
    return {"event_id": event_id, "chain": chain, "summary": _prov.summary(chain),
            "rendered": _prov.render(chain)}


@router.get("/console/turns")
def console_turns(limit: int = 200) -> dict:
    """Consolidated conversational turns (adjacent utterances merged), newest
    first. Rebuilds lazily the first time if the table is empty."""
    from app.services import consolidation

    store = memory._ensure_store()
    if store.turn_count() == 0 and settings.consolidation.enabled:
        try:
            consolidation.rebuild(store)
        except Exception as exc:
            print(f"[consolidation] rebuild failed: {exc}")
    turns = store.recent_turns(limit)
    # #5: annotate each turn with the shared settled/settle_at (final vs still-live)
    # so the console distinguishes a turn that can still grow from a final one.
    import time as _time
    now = _time.time()
    gap = settings.consolidation.max_gap_s
    for t in turns:
        end = t.get("end")
        if isinstance(end, (int, float)):
            t["settle_at"] = round(end + gap, 3)
            t["settled"] = now > end + gap
    return {"count": len(turns), "turns": turns}


@router.post("/console/consolidate")
def console_consolidate() -> dict:
    """Recompute all turns from the current audio timeline."""
    from app.services import consolidation

    n = consolidation.rebuild(memory._ensure_store())
    return {"ok": True, "turns": n}


@router.get("/console/sessions")
def console_sessions(limit: int = 100) -> dict:
    """Sessions: coherent conversation/work blocks (turns grouped by a long-gap
    boundary), newest first. Rebuilds lazily the first time if empty."""
    from app.services import sessions as _sessions

    store = memory._ensure_store()
    if store.session_count() == 0 and settings.consolidation.enabled:
        try:
            _sessions.rebuild(store)
        except Exception as exc:
            print(f"[sessions] rebuild failed: {exc}")
    rows = store.recent_sessions(limit)
    return {"count": len(rows), "sessions": rows}


@router.post("/console/sessions/rebuild")
def console_sessions_rebuild() -> dict:
    """Recompute all sessions from the current turns."""
    from app.services import sessions as _sessions

    n = _sessions.rebuild(memory._ensure_store())
    return {"ok": True, "sessions": n}


@router.get("/console/activity")
def console_activity(limit: int = 100) -> dict:
    """Desktop activities: "what was I doing?" blocks (desktop.screen +
    desktop.click folded per app focus stretch), newest first. Rebuilds lazily
    the first time if the table is empty."""
    from app.services import activity as _activity

    store = memory._ensure_store()
    if store.activity_count() == 0 and settings.consolidation.enabled:
        try:
            _activity.rebuild(store)
        except Exception as exc:
            print(f"[activity] rebuild failed: {exc}")
    rows = store.recent_activities(limit)
    return {"count": len(rows), "activities": rows}


@router.post("/console/activity/rebuild")
def console_activity_rebuild() -> dict:
    """Recompute all activities from the current desktop events."""
    from app.services import activity as _activity

    n = _activity.rebuild(memory._ensure_store())
    return {"ok": True, "activities": n}


@router.get("/console/activity/events")
def console_activity_events(ids: str = "") -> dict:
    """Expand one activity block: its linked events rendered as console rows,
    oldest first — the screens/clicks evidence behind the summary."""
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be comma-separated ints")
    emap = memory._ensure_store().by_ids_map(id_list[:100])
    rows = [_console_row(emap[i].to_dict()) for i in id_list[:100] if i in emap]
    rows.sort(key=lambda r: r["time"] or 0)
    return {"count": len(rows), "events": rows}


@router.get("/console/jobs")
def console_jobs(limit: int = 20) -> dict:
    """Background worker status: job counts by state, recent jobs, and the
    dead-letter queue (poisoned jobs parked after max attempts — plan 0.10)."""
    store = memory._ensure_store()
    from app.services.worker import worker

    return {
        "stats": store.job_stats(),
        "recent": store.recent_jobs(limit),
        "dead": store.dead_jobs(limit),
        "last_error": worker.last_error,
        "max_attempts": settings.worker.max_attempts,
    }


@router.get("/console/camera-health")
def console_camera_health(limit: int = 20) -> dict:
    """Camera health (#6): the live frame-usability state + recent transitions —
    tells 'the camera is broken' (green/uniform/dead placeholder frames) apart
    from 'the VLM failed', and confirms we stopped paying to describe garbage."""
    live = _vision.health()
    recent = [r for r in memory.all()
              if r.get("source") == "vision.camera_health"][-limit:]
    return {"live": live, "recent": recent}


@router.get("/console/audio-health")
def console_audio_health(window_s: float | None = None) -> dict:
    """Audio pipeline health (#9): throughput, drop reasons, quality mix, ASR /
    end-to-end latency, and low-confidence / unknown-speaker rates over a window
    — separates 'the audio was bad' from 'Whisper failed'."""
    store = memory._ensure_store()
    w = window_s if window_s and window_s > 0 else settings.telemetry.window_s
    return store.audio_health(w)


# --- Phase 5: agent activity (runs / packets / verdicts) --------------------
@router.get("/console/agent-runs")
def console_agent_runs(limit: int = 20) -> dict:
    """Personal Agent Layer activity: run outcomes + the human-verdict rates
    (approval / edit / cancel) that measure whether actions are useful, plus the
    most recent runs. This is Sprint 2's inspectability — you can't improve what
    you can't see."""
    store = memory._ensure_store()
    return {"stats": store.agent_run_stats(),
            "recent": store.recent_agent_runs(limit)}


@router.get("/console/agent-runs/{run_id}")
def console_agent_run(run_id: int) -> dict:
    """One run fully hydrated: its action packets, steps, and feedback."""
    store = memory._ensure_store()
    run = store.agent_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such agent run")
    return run


@router.get("/console/trace/{correlation_id}")
def console_trace(correlation_id: str, request: Request,
                  format: str | None = None) -> Response:
    """The full audit chain for one correlation_id (plan 1.5/1.6): the source
    events, the raw fact_candidates rows, materialized facts, and any tagged
    agent_runs — so a fact (or an agent action) traces back to the exact
    utterance that produced it. JSON by default; HTML with `?format=html` or
    an Accept: text/html request (`?format=json` always forces JSON)."""
    from fastapi.responses import JSONResponse

    chain = memory._ensure_store().trace_chain(correlation_id)
    fmt = (format or "").strip().lower()
    wants_html = fmt == "html" or (
        fmt != "json" and "text/html" in (request.headers.get("accept") or ""))
    if wants_html:
        from app.api.trace_page import render_trace_page
        return HTMLResponse(render_trace_page(correlation_id, chain))
    return JSONResponse(chain)


# --- knowledge graph (v1): traversal over people/facts/events ---------------
@router.get("/graph/context")
def graph_context(name: str) -> dict:
    """Traverse the graph around a person: their linked facts (by edge type) and
    who they're discussed with. Builds the graph lazily on first use."""
    from app.services import graph

    store = memory._ensure_store()
    if store.relation_count() == 0:
        try:
            graph.rebuild(store)
        except Exception as exc:
            print(f"[graph] rebuild failed: {exc}")
    return graph.context_for_person(name, store)


@router.post("/graph/rebuild")
def graph_rebuild() -> dict:
    """Recompute all edges from the current facts/turns."""
    from app.services import graph

    return {"ok": True, **graph.rebuild(memory._ensure_store())}


@router.get("/graph/stats")
def graph_stats() -> dict:
    store = memory._ensure_store()
    return {"relations": store.relation_count(), "people": len(store.all_people())}


@router.get("/graph/version")
def graph_version() -> dict:
    """Cheap change token for live UIs: the constellation polls this and only
    refetches the full graph when the token moves (new fact / person / entity /
    edge — including from typed chat)."""
    return {"version": memory._ensure_store().memory_version()}


@router.get("/graph/constellation")
def graph_constellation(limit: int = 48, explain: bool = False) -> dict:
    """Thin adapter over /field/state (A3 Phase 3).

    Kept for back-compat bookmarks and older clients; canonical field payload
    (nodes + context + wm + selection) lives on /field/state.
    """
    return field_state(limit=max(12, min(limit, 40)), explain=bool(explain))


@router.get("/graph/constellation/evidence")
def graph_constellation_evidence(id: str) -> dict:
    """Provenance + why-visible for a constellation node."""
    from app.services import graph
    from app.services.attention_ledger import attention_ledger

    nid = (id or "").strip()
    if not nid:
        raise HTTPException(status_code=400, detail="id required")
    res = graph.constellation_evidence(memory._ensure_store(), nid)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error") or "not found")
    # Opening evidence is engagement — close the node's open field impression.
    attention_ledger.outcome(nid, "click", store=memory._ensure_store())
    return res


@router.get("/kg/explain")
def kg_explain(subj_type: str, subj_id: int, predicate: str,
               obj_type: str, obj_id: int) -> dict:
    """KG-A: why we believe a typed affiliation (evidence bag + confidence)."""
    from app.services import kg_beliefs
    return kg_beliefs.explain_edge(
        memory._ensure_store(),
        subj_type=subj_type, subj_id=subj_id, predicate=predicate,
        obj_type=obj_type, obj_id=obj_id)


@router.get("/graph/org-people")
def graph_org_people(name: str) -> dict:
    """Change 6: current + former affiliates of an org, labeled with intervals."""
    from app.services import graph
    return graph.people_for_entity(memory._ensure_store(), name)


@router.post("/kg/evidence/{evidence_id}/verdict")
def kg_evidence_verdict(evidence_id: int, verdict: str) -> dict:
    """Change 4: evidence-drawer confirm/reject → kg_adjudications flywheel."""
    from app.services import kg_beliefs
    store = memory._ensure_store()
    out = kg_beliefs.evidence_verdict(store, int(evidence_id), verdict)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "bad request")
    # Workstream A: claim adjudications are learning pairs (best-effort).
    try:
        from app.services import learning_store
        ev = store.get_kg_evidence(int(evidence_id)) or {}
        pred = store.get_kg_predicate(int(ev.get("predicate_id") or 0)) or {}
        learning_store.record_kg_evidence_verdict(ev, pred, verdict, store=store)
    except Exception as exc:
        print(f"[learning_store] kg harvest skipped ({exc}).")
    return out


@router.post("/kg/conflicts/both-true")
def kg_conflict_both_true(pred_a: int, pred_b: int) -> dict:
    """Change 3: adjudicate a simultaneous conflict as 'both true' —
    clears flags and restores unpenalized posteriors."""
    from app.services import kg_beliefs
    return kg_beliefs.resolve_conflict_both_true(
        memory._ensure_store(), int(pred_a), int(pred_b))


@router.post("/kg/split")
def kg_split(payload: dict) -> dict:
    """Change 7: manual node split — reassign chosen beliefs (with their
    evidence bags) to a freshly minted node."""
    from app.services import kg_beliefs
    out = kg_beliefs.manual_split(
        memory._ensure_store(),
        node_type=str(payload.get("node_type") or ""),
        node_id=int(payload.get("node_id") or 0),
        new_name=str(payload.get("new_name") or ""),
        predicate_ids=[int(p) for p in (payload.get("predicate_ids") or [])])
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "bad request")
    return out


@router.post("/kg/backfill")
def kg_backfill_enqueue() -> dict:
    """M1: backfill legacy asserted/user relations into the belief store
    (idempotent). Runs on the worker thread; parity diff follows."""
    from app.services.worker import worker
    worker.enqueue("kg_backfill", unique=True)
    return {"ok": True, "queued": True,
            "check": "/kg/parity after the job completes"}


@router.get("/kg/parity")
def kg_parity_status(run: bool = False) -> dict:
    """Change 8: latest dual-write parity reports + M3 cutover gate.

    Plan 2.6: `read_v2` is true when constellation/grounding primary-read
    kg_beliefs (7 clean reports, or QUILL_KG_READ_V2 override).
    """
    from app.services import kg_parity
    store = memory._ensure_store()
    if run:
        kg_parity.run(store)
    st = kg_parity.status(store)
    return {
        "gate": st["gate"],
        "read_v2": st["read_v2"],
        "shadow": st["shadow"],
        "interval_s": st["interval_s"],
        "env_override": st["env_override"],
        "reports_needed": st["reports_needed"],
        "reports": kg_parity.latest_reports(store),
    }


@router.get("/kg/adjudications")
def kg_adjudications(kind: str | None = None, limit: int = 100) -> dict:
    """Change 4: the local-only adjudication log (never syncs/exports)."""
    return {"rows": memory._ensure_store().list_adjudications(
        kind=kind, limit=max(1, min(limit, 500)))}


@router.get("/kg/predicates/{predicate_id}/explain")
def kg_explain_predicate(predicate_id: int) -> dict:
    """KG-A: explain one belief by id."""
    from app.services import kg_beliefs
    out = kg_beliefs.explain_predicate(memory._ensure_store(), int(predicate_id))
    if not out.get("ok"):
        raise HTTPException(status_code=404, detail=out.get("error") or "not found")
    return out


class GraphEdgeBody(BaseModel):
    source: str
    target: str


class GraphPinBody(BaseModel):
    id: str
    pinned: bool = True


@router.post("/graph/edge")
def graph_edge_link(body: GraphEdgeBody) -> dict:
    """Manually connect two constellation nodes (user-asserted link)."""
    from app.services import graph

    res = graph.link_constellation_edge(
        memory._ensure_store(), body.source.strip(), body.target.strip())
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "link failed")
    return res


@router.delete("/graph/edge")
@router.post("/graph/edge/remove")
def graph_edge_unlink(body: GraphEdgeBody) -> dict:
    """Remove a constellation link and hide it from automatic rebuilds."""
    from app.services import graph

    res = graph.unlink_constellation_edge(
        memory._ensure_store(), body.source.strip(), body.target.strip())
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "unlink failed")
    return res


@router.post("/graph/constellation/pin")
def graph_constellation_pin(body: GraphPinBody) -> dict:
    """Pin a node into the constellation field (survives gravity churn)."""
    from app.services import graph
    from app.services.attention_ledger import attention_ledger

    res = graph.pin_constellation_node(
        memory._ensure_store(), body.id.strip(), bool(body.pinned))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "pin failed")
    attention_ledger.outcome(body.id.strip(),
                             "pin" if body.pinned else "unpin",
                             store=memory._ensure_store())
    return res


class GraphReclassifyBody(BaseModel):
    id: str
    kind: str


@router.post("/graph/constellation/reclassify")
def graph_constellation_reclassify(body: GraphReclassifyBody) -> dict:
    """Correct a constellation node's category (person/project/commitment/…)."""
    from app.services import graph
    from app.services.attention_ledger import attention_ledger

    res = graph.reclassify_constellation_node(
        memory._ensure_store(), body.id.strip(), body.kind.strip())
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "reclassify failed")
    attention_ledger.outcome(body.id.strip(), "reclassify",
                             detail={"kind": body.kind.strip()},
                             store=memory._ensure_store())
    return res


class FieldFeedbackBody(BaseModel):
    id: str
    outcome: str            # dwell | dismiss
    dwell_ms: int | None = None


@router.post("/field/feedback")
def field_feedback(body: FieldFeedbackBody) -> dict:
    """Close an attention impression with a UI reaction — evidence dwell or
    horizon dismissal (strong negative for learned ranking)."""
    from app.services.attention_ledger import attention_ledger
    from app.services import horizon as _horizon

    outcome = (body.outcome or "").strip().lower()
    if outcome not in ("dwell", "dismiss"):
        raise HTTPException(status_code=400, detail="outcome must be dwell|dismiss")
    store = memory._ensure_store()
    detail = {"dwell_ms": int(body.dwell_ms)} if body.dwell_ms else None
    ok = attention_ledger.outcome(body.id.strip(), outcome, detail=detail,
                                  store=store)
    if outcome == "dismiss":
        _horizon.dismiss(store, body.id.strip())
    return {"ok": bool(ok)}


@router.get("/field/predictions")
def field_predictions() -> dict:
    """Horizon strip — ≤3 predicted-next items with reasons (Track A4)."""
    from app.services import horizon as _horizon
    return _horizon.strip(memory._ensure_store(), refresh_first=True)


@router.get("/console/attention")
def console_attention(days: float = 7.0) -> dict:
    """Attention harness (P0 + A1): ledger aggregates, fulfillment, golden
    corpus, and priors-continuity replay status — one round-trip for the
    /console Attention tab."""
    from app.services import attention_corpus, attention_replay, fulfillment
    from app.services.attention_ledger import attention_ledger

    store = memory._ensure_store()
    out = attention_ledger.stats(days=max(0.1, min(days, 90.0)), store=store)
    # Surface the weekly check-in nudge where the metrics live.
    last = store.last_self_report_ts()
    import time as _time
    out["self_report_due"] = bool(last is None
                                  or _time.time() - last > 7 * 86400.0)
    out["self_report_last_ts"] = last
    facts = (store.list_facts(kind="task", limit=5000)
             + store.list_facts(kind="commitment", limit=5000))
    out["fulfillment"] = fulfillment.summarize(facts)
    try:
        out["fulfillment"] = fulfillment.with_baseline(out["fulfillment"])
    except Exception:
        pass
    out["corpus"] = attention_corpus.status()
    out["a1"] = attention_replay.status(store=store)
    try:
        from app.services import context_feeder
        from app.services.activation import activation_field
        from app.config import settings as _settings
        edge_n = 0
        try:
            edge_n = len(store.conductive_edges())
        except Exception:
            pass
        out["a2"] = {
            "field_v2": bool(_settings.attention.field_v2),
            "feeder": context_feeder.status(),
            "conductive_edges": edge_n,
            "activation_cache": bool(activation_field._cache),
            "endpoints": {
                "state": "/field/state",
                "stream": "/field/stream",
                "observe": "POST /field/context/observe",
                "mode": "POST /field/mode",
            },
        }
        from app.services import working_memory as _wm
        from app.services import attention_mode as _amode
        out["a3"] = _wm.status(store)
        try:
            out["a3"]["mode"] = _amode.current(store=store)
        except Exception:
            pass
        from app.services import ranking_learn, horizon as _horizon, meta_memory
        from app.services import ranking_promote
        out["a4"] = {
            "learn": ranking_learn.explain(store),
            "horizon": _horizon.strip(store, refresh_first=False),
            "promote": ranking_promote.status(store),
            "meta": {
                "at_risk": len(meta_memory.scan_at_risk(store)),
                "stale": len(meta_memory.scan_stale_facts(store)),
                "forget": len(meta_memory.scan_forget_candidates(store)),
                "dropped": len(meta_memory.scan_dropped_threads(store)),
                "fading": len(meta_memory.scan_fading_ideas(store)),
                "questions": len(meta_memory.scan_open_questions(store)),
                "weakening": len(meta_memory.scan_weakening_relationships(store)),
            },
            "endpoints": {
                "predictions": "/field/predictions",
                "learn_revert": "POST /console/attention/learn/revert",
                "meta_run": "POST /console/attention/meta",
                "promote": "POST /console/attention/promote",
                "reasoners": "GET /console/reasoners",
                "reasoners_run": "POST /console/reasoners/run",
            },
        }
        try:
            from app.services import reasoners as _reasoners
            rs = _reasoners.status(store)
            out["a4"]["reasoners"] = {
                "enabled": rs.get("enabled"),
                "daily_remaining": rs.get("daily_remaining"),
                "last": rs.get("last"),
                "fulfillment_delta": (rs.get("fulfillment") or {}).get(
                    "fulfillment_delta"),
            }
        except Exception:
            pass
        try:
            from app.services import memory_economy
            out["c"] = memory_economy.status(store)
        except Exception as exc:
            out["c"] = {"error": str(exc)}
        try:
            from app.services import predictor_bench, hardening
            out["f"] = {
                "predictors": predictor_bench.status(store),
                "hardening": hardening.status(store),
            }
        except Exception as exc:
            out["f"] = {"error": str(exc)}
    except Exception as exc:
        out["a2"] = {"error": str(exc)}
        out["a3"] = {"error": str(exc)}
        out["a4"] = {"error": str(exc)}
        out["c"] = {"error": str(exc)}
        out["f"] = {"error": str(exc)}
    return out


@router.post("/console/attention/replay")
def console_attention_replay(days: float = 7.0) -> dict:
    """Run the A1 priors-continuity gate now and persist the result."""
    from app.services import attention_replay

    return attention_replay.run(days=days, store=memory._ensure_store())


@router.post("/console/attention/backfill")
def console_attention_backfill() -> dict:
    """Seed missing node_dynamics rows (idempotent A1 backfill)."""
    from app.services import traces_backfill

    return traces_backfill.run(store=memory._ensure_store())


@router.post("/console/attention/feed")
def console_attention_feed() -> dict:
    """Force one Now-Context feeder pass (speech / activity / calendar)."""
    from app.services import context_feeder

    return context_feeder.feed_once(store=memory._ensure_store())


@router.post("/console/attention/learn/revert")
def console_attention_learn_revert() -> dict:
    """Revert β to shipped GRAVITY priors (A4 kill-switch companion)."""
    from app.services import ranking_learn
    return ranking_learn.revert_to_prior(memory._ensure_store())


@router.post("/console/attention/meta")
def console_attention_meta() -> dict:
    """Run meta-memory audits (at-risk urgency + stale/forget review items)."""
    from app.services import meta_memory
    return meta_memory.run(memory._ensure_store(), write_reflections=True)


@router.post("/console/attention/promote")
def console_attention_promote(days: float = 14.0) -> dict:
    """Run the A4 β promote-or-hold gate now and persist the result."""
    from app.services import ranking_promote
    return ranking_promote.run(days=days, store=memory._ensure_store())


@router.get("/console/economy")
def console_economy() -> dict:
    """Track C: lifecycle counts, retention sweep state, storage-growth curve,
    compaction candidates, and the forgotten-this-month review list."""
    from app.services import memory_economy
    return memory_economy.status(memory._ensure_store())


@router.post("/console/economy/sweep")
def console_economy_sweep() -> dict:
    """Run the retention sweep now (scores + lifecycle metadata; compaction
    only if QUILL_COMPACTION is on)."""
    from app.services import memory_economy
    return memory_economy.sweep(memory._ensure_store())


@router.post("/console/economy/compact")
def console_economy_compact(event_id: int) -> dict:
    """Compact ONE event on explicit user request (review-first path — works
    even while QUILL_COMPACTION is off). Original is archived; open citing
    facts still refuse."""
    from app.services import memory_economy
    return memory_economy.compact_one(memory._ensure_store(), event_id)


@router.post("/console/economy/restore")
def console_economy_restore(event_id: int) -> dict:
    """Undo a compaction — the archived original raw comes back verbatim."""
    from app.services import memory_economy
    ok = memory_economy.restore(memory._ensure_store(), event_id)
    if not ok:
        raise HTTPException(status_code=404,
                            detail="no archived original for that event")
    return {"ok": True, "event_id": int(event_id)}


@router.post("/console/economy/lance/optimize")
def console_economy_lance_optimize() -> dict:
    """Force Lance compact + version prune (recovery from version backlog)."""
    from app.vectorstore import get_vectorstore
    return get_vectorstore().force_optimize()


@router.post("/console/economy/vector-gc")
def console_economy_vector_gc() -> dict:
    """Plan 6.6: drop Lance rows for dismissed/superseded/evidence_removed
    facts past the grace window, then optimize."""
    from app.services import memory as memory_svc
    return memory_svc.vector_gc(memory._ensure_store())


@router.get("/console/predictors")
def console_predictors() -> dict:
    """Track F: per-task active model, latest bench metrics, and a preview of
    what each heuristic would predict right now (console-only — no offers)."""
    from app.services import predictor_bench
    return predictor_bench.status(memory._ensure_store())


@router.post("/console/predictors/bench")
def console_predictors_bench(task: str | None = None) -> dict:
    """Run the walk-forward bench now (one task, or all)."""
    from app.services import predictor_bench
    return predictor_bench.run(task, memory._ensure_store())


@router.post("/console/predictors/promote")
def console_predictors_promote(task: str) -> dict:
    """Promote-or-hold: a candidate model activates only if it beats the
    active one on the held-out window."""
    from app.services import predictor_bench
    return predictor_bench.promote(task, memory._ensure_store())


@router.post("/console/predictors/rollback")
def console_predictors_rollback(task: str) -> dict:
    """Re-activate the previously active model for a task."""
    from app.services import predictor_bench
    return predictor_bench.rollback(task, memory._ensure_store())


@router.get("/console/hardening")
def console_hardening() -> dict:
    """Kill-switch audit (current vs shipped defaults), battery, last drill."""
    from app.services import hardening
    return hardening.status(memory._ensure_store())


class KillSwitchBody(BaseModel):
    env: str
    on: bool


@router.post("/console/hardening/kill-switch")
def console_hardening_kill_switch(body: KillSwitchBody) -> dict:
    """Flip a Track-F kill switch from the console (persists + hot-patches)."""
    from app.services import hardening
    try:
        row = hardening.set_kill_switch(body.env, bool(body.on))
    except KeyError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"ok": True, "switch": row, "kill_switches": hardening.kill_switches()}


@router.post("/console/hardening/drill")
def console_hardening_drill() -> dict:
    """Run the restore drill now: backup -> reopen -> verify -> clean up."""
    from app.services import hardening
    return hardening.restore_drill(memory._ensure_store())


@router.get("/console/reasoners")
def console_reasoners() -> dict:
    """Track D reasoner status (commitment / relationship / scheduling)."""
    from app.services import reasoners
    return reasoners.status(memory._ensure_store())


@router.post("/console/reasoners/run")
def console_reasoners_run(surface: bool = False) -> dict:
    """Run one reasoner pass. Default dry-run (surface=false) so the console
    never accidentally interrupts chat; pass surface=true to offer."""
    from app.services import reasoners
    return reasoners.run_once(memory._ensure_store(), surface=bool(surface))


@router.get("/console/fulfillment")
def console_fulfillment() -> dict:
    """Commitment-fulfillment baseline (Phase 0): of the work that closed, how
    much closed by getting DONE; on-time rate; overdue and aging open items;
    weekly created-vs-resolved. The before-number the attention track must beat."""
    from app.services import fulfillment

    store = memory._ensure_store()
    facts = (store.list_facts(kind="task", limit=5000)
             + store.list_facts(kind="commitment", limit=5000))
    return fulfillment.with_baseline(fulfillment.summarize(facts))


@router.post("/console/fulfillment/baseline")
def console_fulfillment_baseline() -> dict:
    """Stamp today's fulfillment numbers as the wedge baseline to beat."""
    from app.services import fulfillment

    store = memory._ensure_store()
    facts = (store.list_facts(kind="task", limit=5000)
             + store.list_facts(kind="commitment", limit=5000))
    summary = fulfillment.summarize(facts)
    stamped = fulfillment.stamp_baseline(summary, note="console")
    return {"ok": True, "baseline": stamped, "current": summary}


# ------------------------------ field v2 (A2) --------------------------------

@router.get("/field/state")
def field_state(limit: int = 40, explain: bool = False) -> dict:
    """Canonical field payload: ranked nodes + Now-Context + Working Memory.

    /graph/constellation is a thin adapter over this route (A3 Phase 3).

    Pass explain=true to include a `breakdowns` map (ScoreBreakdown per
    surfaced node) for auditable rank — omitted by default to keep the
    payload small.
    """
    from app.config import settings as _settings
    from app.services import graph
    from app.services import working_memory as _wm
    from app.services.now_context import now_context

    store = memory._ensure_store()
    field = graph.constellation(
        store, limit=max(12, min(limit, 40)),
        record_impressions=True,
        explain=bool(explain),
    )
    seeds = sorted(now_context.seeds().items(), key=lambda kv: -kv[1])[:10]
    field["context"] = {
        "generation": now_context.generation,
        "seeds": [{"id": f"{t}:{i}", "weight": round(w, 3)}
                  for (t, i), w in seeds],
    }
    field["wm"] = _wm.status(store)
    field["v2"] = bool(_settings.attention.field_v2)
    field["wm_enabled"] = bool(_settings.attention.wm)
    try:
        from app.services import attention_mode as _amode
        field["mode"] = _amode.current(store=store)
        field["modes"] = _amode.registry()
    except Exception:
        pass
    try:
        from app.services import horizon as _horizon
        field["horizon"] = _horizon.strip(store, refresh_first=True)
    except Exception as exc:
        field["horizon"] = {"enabled": False, "items": [], "error": str(exc)}
    try:
        from app.services import ranking_learn
        field["learn"] = {
            "enabled": ranking_learn._learn_enabled(),
            "n_updates": ranking_learn.explain(store).get("n_updates"),
        }
    except Exception:
        pass
    # WS3: persist a lightweight snapshot when memory_version moved — feeds
    # /field/diff. Ring-buffer retention; event log remains the archive.
    try:
        from app.services import field_history as _fh
        _fh.maybe_persist_snapshot(store, field)
    except Exception as exc:
        print(f"[field/state] snapshot skipped ({exc}).")
    return field


@router.get("/field/diff")
def field_diff(since: str | None = None) -> dict:
    """Temporal field delta: entered/left focus, rising/falling, aging.

    Default `since` = start of today (user-local). Pass a unix ts, ISO date,
    or a prior memory_version string.
    """
    from app.services import field_history as _fh
    from app.services import graph

    store = memory._ensure_store()
    # Ensure we have a current snapshot to diff against.
    try:
        field = graph.constellation(store, limit=28, record_impressions=False)
        _fh.maybe_persist_snapshot(store, field)
    except Exception:
        field = None
    return _fh.diff(store, since=since, current=field)


class ContextObserveBody(BaseModel):
    id: str                 # "person:12" | "entity:5" | "fact:88"
    weight: float = 1.0


@router.post("/field/context/observe")
def field_context_observe(body: ContextObserveBody) -> dict:
    """Explicitly seed the Now-Context — the UI's focus handle (double-tap a
    node, or anything that knows what the user is looking at)."""
    from app.services import working_memory as _wm
    from app.services.attention_ledger import _parse_node
    from app.services.now_context import now_context

    parsed = _parse_node((body.id or "").strip())
    if not parsed:
        raise HTTPException(status_code=400,
                            detail="id must be person:<n> | entity:<n> | fact:<n>")
    now_context.observe([parsed], weight=body.weight, source="explicit")
    refreshed = _wm.ensure_fresh(memory._ensure_store(), force=True)
    return {"ok": True, "generation": now_context.generation,
            "wm_refreshed": refreshed}


class FieldModeBody(BaseModel):
    mode: str | None = None   # registry id, or "auto"/"clear" to release manual


@router.get("/field/mode")
def field_mode_get() -> dict:
    from app.services import attention_mode as _amode
    store = memory._ensure_store()
    return {"mode": _amode.current(store=store), "modes": _amode.registry()}


@router.post("/field/mode")
def field_mode_set(body: FieldModeBody) -> dict:
    """Manual attention mode (chip). TTL 2h; 'auto' clears override."""
    from app.services import attention_mode as _amode
    from app.services import working_memory as _wm

    try:
        mode = _amode.set_manual(body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Mode change should reshuffle WM under the new multipliers.
    refreshed = _wm.ensure_fresh(memory._ensure_store(), force=True)
    return {"ok": True, "mode": mode, "wm_refreshed": refreshed}


@router.get("/field/stream")
async def field_stream():
    """SSE change feed: version/wave tokens plus WM enter/exit deltas (§15).

    Clients may refetch on `version`/`wave`, or apply `wm` enter/exit without
    a full redraw. The 4s poll remains as the fallback transport.
    """
    import asyncio
    import json as _json

    from fastapi.responses import StreamingResponse
    from app.services import attention_mode as _amode
    from app.services import working_memory as _wm
    from app.services.now_context import now_context

    store = memory._ensure_store()

    async def _events():
        last_version = None
        last_gen = None
        last_wm_ts = None
        last_mode = None
        beats = 0
        for _ in range(3600):          # cap one hour per connection; client reconnects
            try:
                version = store.memory_version()
                gen = now_context.generation
                delta = _wm.last_delta()
                wm_ts = delta.get("ts")
                try:
                    mode = _amode.current(store=store)
                    mode_id = mode.get("id")
                except Exception:
                    mode, mode_id = None, None
                mode_changed = (mode_id is not None and mode_id != last_mode
                                and last_mode is not None)
                if version != last_version or gen != last_gen:
                    kind = "wave" if (version == last_version
                                      and last_gen is not None) else "version"
                    last_version, last_gen, beats = version, gen, 0
                    last_wm_ts = wm_ts
                    last_mode = mode_id
                    yield ("event: change\ndata: "
                           + _json.dumps({
                               "type": kind,
                               "version": version,
                               "generation": gen,
                               "enter": delta.get("enter") or [],
                               "exit": delta.get("exit") or [],
                               "wm": delta.get("wm") or [],
                               "mode": mode_id,
                           }) + "\n\n")
                elif (wm_ts is not None and wm_ts != last_wm_ts
                      and (delta.get("enter") or delta.get("exit"))):
                    last_wm_ts = wm_ts
                    beats = 0
                    yield ("event: change\ndata: "
                           + _json.dumps({
                               "type": "wm",
                               "version": version,
                               "generation": gen,
                               "enter": delta.get("enter") or [],
                               "exit": delta.get("exit") or [],
                               "wm": delta.get("wm") or [],
                               "mode": mode_id,
                           }) + "\n\n")
                elif mode_changed:
                    last_mode = mode_id
                    beats = 0
                    yield ("event: change\ndata: "
                           + _json.dumps({
                               "type": "mode",
                               "version": version,
                               "generation": gen,
                               "mode": mode_id,
                               "enter": [],
                               "exit": [],
                               "wm": delta.get("wm") or [],
                           }) + "\n\n")
                else:
                    if last_mode is None:
                        last_mode = mode_id
                    beats += 1
                    if beats >= 15:    # heartbeat every ~15s of quiet
                        beats = 0
                        yield "event: ping\ndata: {}\n\n"
            except Exception:
                yield "event: ping\ndata: {}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(_events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ------------------------------ weekly self-report ---------------------------

@router.get("/selfreport", response_class=HTMLResponse)
def selfreport_ui() -> HTMLResponse:
    """Weekly check-in form — the subjective half of the Phase 0 harness."""
    from app.api.selfreport_page import SELFREPORT_PAGE

    return HTMLResponse(SELFREPORT_PAGE)


@router.get("/selfreport/status")
def selfreport_status() -> dict:
    import time as _time
    last = memory._ensure_store().last_self_report_ts()
    days = ((_time.time() - last) / 86400.0) if last else None
    return {"due": bool(last is None or days > 7.0),
            "last_ts": last, "days_since": days}


@router.get("/selfreport/list")
def selfreport_list(limit: int = 26) -> dict:
    return {"reports":
            memory._ensure_store().list_self_reports(max(1, min(limit, 100)))}


class SelfReportIn(BaseModel):
    load: int | None = None            # 1 heavier .. 5 much lighter
    trust: int | None = None           # 1 none .. 5 complete
    interruptions: int | None = None   # 1 annoying .. 5 always welcome
    note: str | None = None


@router.post("/selfreport")
def selfreport_add(body: SelfReportIn) -> dict:
    def _score(v):
        return v if (isinstance(v, int) and 1 <= v <= 5) else None
    if not any((_score(body.load), _score(body.trust),
                _score(body.interruptions), (body.note or "").strip())):
        raise HTTPException(status_code=400,
                            detail="give at least one score or a note")
    rid = memory._ensure_store().add_self_report(
        load_score=_score(body.load), trust_score=_score(body.trust),
        interrupt_score=_score(body.interruptions),
        note=(body.note or "").strip() or None)
    return {"ok": True, "id": rid}


# ------------------------------ memory changes -------------------------------

@router.get("/memory/changes", response_class=HTMLResponse)
def memory_changes_ui() -> HTMLResponse:
    """Contradiction surfacing: every auto-supersede as a reviewable,
    reversible old→new card. Automatic, never invisible."""
    from app.api.changes_page import CHANGES_PAGE

    return HTMLResponse(CHANGES_PAGE)


@router.get("/memory/supersessions")
def memory_supersessions(limit: int = 50) -> dict:
    return {"supersessions": memory._ensure_store()
            .recent_supersessions(max(1, min(limit, 200)))}


class SupersessionRevert(BaseModel):
    old_id: int


@router.post("/memory/supersessions/revert")
def memory_supersessions_revert(body: SupersessionRevert) -> dict:
    """The user says the OLD version was right — swap the supersede direction
    (old reactivates with its typed rows; new becomes the superseded one)."""
    store = memory._ensure_store()
    if not store.revert_supersession(int(body.old_id)):
        raise HTTPException(
            status_code=400,
            detail="not revertible — no such supersession, or a newer fact "
                   "already replaced the replacement")
    return {"ok": True, "restored": int(body.old_id)}


@router.get("/home/intelligence")
def home_intelligence() -> dict:
    """Today's Intelligence aggregate for the Home surface."""
    from app.services.home_intelligence import build as build_home

    agent_state = {}
    if not _agent_disabled():
        try:
            _, agent_state = agent.worker.snapshot(10**9)
        except Exception:
            agent_state = {}
    recent = memory.all()[-24:]
    return build_home(
        memory._ensure_store(),
        agent_state=agent_state,
        recent_events=recent,
    )


@router.get("/today", response_class=HTMLResponse)
def today_page() -> HTMLResponse:
    """Today — attention-ordered home + proposals (canonical dashboard)."""
    from app.api.shell_page import SHELL_PAGE
    return _html_with_approval(SHELL_PAGE, next_url="/today")


@router.get("/shell", response_class=RedirectResponse)
def shell_redirect() -> RedirectResponse:
    """Permanent redirect: engineering route → canonical /today."""
    return RedirectResponse(url="/today", status_code=301)


def _today_state(limit: int = 28) -> dict:
    """Aggregate: world (field/WM) + attention + pending offer peek."""
    from app.services import shell_state as _shell

    worker = None
    if not _agent_disabled():
        try:
            worker = agent.worker
        except Exception:
            worker = None
    return _shell.build(
        memory._ensure_store(),
        agent_worker=worker,
        field_limit=max(12, min(limit, 40)),
    )


@router.get("/today/state")
def today_state(limit: int = 28) -> dict:
    return _today_state(limit)


@router.get("/shell/state")
def shell_state(limit: int = 28) -> dict:
    """Alias for /today/state (bookmarks / older clients)."""
    return _today_state(limit)


class ShellOfferIn(BaseModel):
    accept: bool | None = None
    choice: str | None = None


def _today_offer(body: ShellOfferIn) -> dict:
    """Forward yes/no (or a meeting-record choice) to agent_bridge."""
    if _agent_disabled():
        return {"ok": False, "error": "agent disabled (QUILL_AGENT=0)"}
    try:
        accept = True if body.accept is None and body.choice else bool(body.accept)
        return agent.worker.resolve_todo(accept, choice=body.choice)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/today/offer")
def today_offer(body: ShellOfferIn) -> dict:
    return _today_offer(body)


@router.post("/shell/offer")
def shell_offer(body: ShellOfferIn) -> dict:
    """Alias for /today/offer."""
    return _today_offer(body)


class ShellRestoreIn(BaseModel):
    event_id: int


def _today_restore(body: ShellRestoreIn) -> dict:
    """Undo a compaction from the Today forgotten list (same path as console)."""
    from app.services import memory_economy
    ok = memory_economy.restore(memory._ensure_store(), int(body.event_id))
    if not ok:
        raise HTTPException(status_code=404,
                            detail="no archived original for that event")
    return {"ok": True, "event_id": int(body.event_id)}


@router.post("/today/restore")
def today_restore(body: ShellRestoreIn) -> dict:
    return _today_restore(body)


@router.post("/shell/restore")
def shell_restore(body: ShellRestoreIn) -> dict:
    """Alias for /today/restore."""
    return _today_restore(body)


class SessionNoteIn(BaseModel):
    text: str
    session_id: int | None = None


def _session_note(body: SessionNoteIn) -> dict:
    """Meeting Layer P2 — notepad jot → meeting.note TEXT event."""
    from app.services import meeting_notes
    eid = meeting_notes.ingest(
        body.text,
        store=memory._ensure_store(),
        session_id=body.session_id,
    )
    if eid is None:
        raise HTTPException(status_code=400,
                            detail="note too short or notes disabled")
    return {"ok": True, "event_id": int(eid), "text": (body.text or "").strip()}


@router.post("/session/note")
def session_note(body: SessionNoteIn) -> dict:
    return _session_note(body)


@router.post("/today/note")
def today_note(body: SessionNoteIn) -> dict:
    """Alias for /session/note (Today notepad)."""
    return _session_note(body)


# --- Meeting Layer P3: enhanced meeting notes with receipts ----------------
def _meeting_title(summary: str) -> str:
    title = (summary or "").split("\n", 1)[0].strip() or "Meeting note"
    if " · " in title:
        title = title.split(" · ", 1)[0].strip()
    return title


@router.get("/meetings", response_class=HTMLResponse)
def meetings_page() -> HTMLResponse:
    from app.api.meeting_page import MEETINGS_LIST_PAGE
    return _html_with_approval(MEETINGS_LIST_PAGE, next_url="/meetings")


@router.get("/meetings/list")
def meetings_list(limit: int = 40) -> dict:
    from app.services import meeting_enhance
    store = memory._ensure_store()
    rows = store.list_reflections(scope="meeting", limit=limit)
    out = []
    for r in rows:
        items = store.reflection_items(r["id"])
        when = ""
        if r.get("period_start"):
            import time as _t
            when = _t.strftime("%a %b %d %H:%M", _t.localtime(r["period_start"]))
        out.append({
            "id": r["id"],
            "title": _meeting_title(r.get("summary") or ""),
            "when": when,
            "n_items": len(items),
            "created_at": r.get("created_at"),
        })
    return {"meetings": out}


@router.get("/meetings/{session_id}", response_class=RedirectResponse)
def meeting_by_session(session_id: int) -> RedirectResponse:
    """First-win toast deep-link. Session id → enhanced note, never a JSON 404."""
    from app.services import meeting_enhance
    store = memory._ensure_store()
    href = meeting_enhance.note_href_for_session(store, session_id)
    return RedirectResponse(url=href, status_code=303)


@router.get("/meeting/note/latest")
def meeting_note_latest(format: str = "html"):
    """Latest meeting note — `?format=json` for the hydrated payload."""
    from app.services import meeting_enhance
    store = memory._ensure_store()
    header = store.latest_reflection("meeting")
    if (format or "").lower() == "json":
        if not header:
            return {"note": None}
        return {"note": meeting_enhance.hydrate_meeting_note(store, header)}
    from app.api.meeting_page import MEETING_PAGE
    page = MEETING_PAGE.replace("@@NOTE_ID@@", "null")
    return _html_with_approval(page, next_url="/meeting/note/latest")


@router.get("/meeting/note/{reflection_id}")
def meeting_note(reflection_id: int, format: str = "html"):
    from app.services import meeting_enhance
    store = memory._ensure_store()
    header = store.get_reflection(reflection_id)
    if header is None or header.get("scope") != "meeting":
        raise HTTPException(status_code=404, detail="meeting note not found")
    if (format or "").lower() == "json":
        return {"note": meeting_enhance.hydrate_meeting_note(store, header)}
    from app.api.meeting_page import MEETING_PAGE
    page = MEETING_PAGE.replace("@@NOTE_ID@@", str(int(reflection_id)))
    return _html_with_approval(
        page, next_url=f"/meeting/note/{reflection_id}")


@router.post("/meeting/enhance")
def meeting_enhance_run(force: bool = False) -> dict:
    """Manual trigger — enhance eligible settled sessions now."""
    from app.services import meeting_enhance
    return meeting_enhance.run_once(
        memory._ensure_store(), verbose=True, force=force)


# --- Meeting Layer P4: ask this meeting + draft follow-up ------------------
class MeetingAskIn(BaseModel):
    question: str
    session_id: int | None = None


class MeetingDraftIn(BaseModel):
    to: str | None = None
    dry_run: str | None = "draft"


@router.post("/meeting/note/{reflection_id}/ask")
def meeting_note_ask(reflection_id: int, body: MeetingAskIn) -> dict:
    """Answer a question scoped to one meeting note (no browser agent)."""
    from app.services import meeting_chat
    out = meeting_chat.ask(
        body.question,
        meeting_reflection_id=int(reflection_id),
        session_id=body.session_id,
        store=memory._ensure_store(),
    )
    if not out.get("ok"):
        raise HTTPException(status_code=404, detail=out.get("error") or "ask failed")
    return out


@router.post("/meeting/note/{reflection_id}/draft")
def meeting_note_draft(reflection_id: int, body: MeetingDraftIn | None = None) -> dict:
    """Enqueue a grounded follow-up draft citing the note's fact ids."""
    from app.services import meeting_chat
    body = body or MeetingDraftIn()
    dry = (body.dry_run or "draft").strip().lower()
    if dry not in ("draft", "plan", "navigate", "approval", "full", "autonomous"):
        dry = "draft"
    out = meeting_chat.draft_followup(
        int(reflection_id),
        store=memory._ensure_store(),
        dry_run=dry,
        to=body.to,
    )
    if not out.get("ok"):
        code = 503 if "agent" in (out.get("error") or "") else 404
        raise HTTPException(status_code=code, detail=out.get("error") or "draft failed")
    return out


# --- Meeting Layer P5: meeting mode + retention ----------------------------
class MeetingModeIn(BaseModel):
    until: float | None = None
    title: str | None = None
    calendar_event_id: str | None = None
    session_id: int | None = None


class MeetingRetentionIn(BaseModel):
    retention: str  # transcript_only | keep_receipts
    session_id: int | None = None
    calendar_event_id: str | None = None
    default: bool = False  # when True, set the user default preference


@router.get("/meeting/mode")
def meeting_mode_get() -> dict:
    from app.services import meeting_mode as _mm
    return _mm.status()


@router.post("/meeting/mode")
def meeting_mode_enter(body: MeetingModeIn) -> dict:
    from app.services import meeting_mode as _mm
    return _mm.enter(
        until=body.until,
        title=body.title or "",
        calendar_event_id=body.calendar_event_id,
        session_id=body.session_id,
        source="manual",
    )


@router.post("/meeting/mode/exit")
def meeting_mode_exit() -> dict:
    from app.services import meeting_mode as _mm
    return _mm.exit_mode(reason="manual")


@router.post("/meeting/retention")
def meeting_retention_set(body: MeetingRetentionIn) -> dict:
    """Set default retention preference and/or apply to one session."""
    from app.services import meeting_mode as _mm
    if body.default:
        try:
            prefs = _mm.set_default_retention(body.retention)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "default_retention": prefs.get("default_retention")}
    out = _mm.set_session_retention(
        body.retention,
        session_id=body.session_id,
        calendar_event_id=body.calendar_event_id,
        store=memory._ensure_store(),
        apply=True,
    )
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "bad retention")
    return out


@router.get("/approvals/state")
def approvals_state() -> dict:
    """Current pending approval/offer for the global banner."""
    from app.api.approval_partial import collect_state
    return collect_state(_agent_worker())


@router.post("/approvals/resolve")
async def approvals_resolve(
    request: Request,
    accept: str = Form("0"),
    next: str = Form("/today"),
    as_json: str = Form(""),
) -> Response:
    """Yes/No for the global banner — works without JS (303 redirect).

    With `as_json=1` or Accept: application/json returns JSON instead of redirecting.
    When a bound packet is pending, resolve() routes through hash-checked decide.
    """
    from app.api.approval_partial import resolve as _resolve

    yes = str(accept).strip().lower() in ("1", "true", "yes", "on")
    result = _resolve(_agent_worker(), yes)
    want_json = (
        str(as_json).strip() in ("1", "true")
        or "application/json" in (request.headers.get("accept") or "")
    )
    if want_json:
        from fastapi.responses import JSONResponse
        status = 200 if (isinstance(result, dict) and result.get("ok", True)) else 409
        return JSONResponse(
            result if isinstance(result, dict) else {"ok": True, "result": result},
            status_code=status)
    dest = next if str(next).startswith("/") else "/today"
    return RedirectResponse(url=dest, status_code=303)


@router.post("/approval/{packet_id}/decide")
async def approval_decide(packet_id: int, request: Request) -> Response:
    """Bound Approve/Cancel/Edit (plan 0.6).

    Accepts JSON `{payload_hash, decision, user_edit?, fields?, approved_via?}`
    or the same fields as a form POST (banner / no-JS). Stale or drifted
    `payload_hash` is refused — free text alone cannot authorize the packet.
    """
    from app.api.approval_partial import decide as _decide
    from fastapi.responses import JSONResponse

    ctype = (request.headers.get("content-type") or "").lower()
    fields = None
    next_url = "/chat"
    as_json = ""
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = {}
        payload_hash = body.get("payload_hash") or ""
        decision = body.get("decision") or "approve"
        user_edit = body.get("user_edit")
        approved_via = body.get("approved_via") or "button"
        fields = body.get("fields")
        next_url = body.get("next") or "/chat"
        want_json = True
    else:
        form = await request.form()
        payload_hash = form.get("payload_hash") or ""
        decision = form.get("decision") or "approve"
        user_edit = form.get("user_edit")
        approved_via = form.get("approved_via") or "button"
        next_url = form.get("next") or "/chat"
        as_json = form.get("as_json") or ""
        want_json = (
            str(as_json).strip() in ("1", "true")
            or "application/json" in (request.headers.get("accept") or "")
        )

    result = _decide(
        _agent_worker(), int(packet_id), str(payload_hash or ""),
        str(decision or "approve"),
        user_edit=user_edit, fields=fields,
        approved_via=str(approved_via or "button"))
    if want_json:
        status = 200 if result.get("ok") else 409
        return JSONResponse(result, status_code=status)
    dest = next_url if str(next_url).startswith("/") else "/chat"
    return RedirectResponse(url=dest, status_code=303)


@router.get("/approvals/stream")
async def approvals_stream():
    """SSE of approval state — MPA-compatible; long-poll style heartbeat."""
    import asyncio
    import json as _json

    from app.api.approval_partial import collect_state
    from fastapi.responses import StreamingResponse

    async def gen():
        last = ""
        # ~2 minutes of ticks; EventSource reconnects.
        for _ in range(60):
            state = collect_state(_agent_worker())
            sig = state.get("sig") or ""
            if sig != last:
                last = sig
                payload = {k: state[k] for k in state
                           if k not in ("offer", "packet")}
                yield f"event: approval\ndata: {_json.dumps(payload)}\n\n"
            else:
                yield ": ping\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _hold_tip_path():
    from pathlib import Path
    from app.config import settings
    return Path(settings.storage.data_dir) / "ui_hold_tip.json"


class HoldTipIn(BaseModel):
    seen: bool = True


@router.get("/ui/hold-tip")
def hold_tip_get() -> dict:
    """Whether the one-time hold-gesture teach tip has been dismissed (§6)."""
    path = _hold_tip_path()
    try:
        if path.is_file():
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            return {"seen": bool(data.get("seen"))}
    except Exception:
        pass
    return {"seen": False}


@router.post("/ui/hold-tip")
def hold_tip_set(body: HoldTipIn | None = None) -> dict:
    """Persist hold-tip dismissal server-side (not localStorage)."""
    import json
    path = _hold_tip_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = True if body is None else bool(body.seen)
    path.write_text(json.dumps({"seen": seen}) + "\n", encoding="utf-8")
    return {"ok": True, "seen": seen}


def _policy_preflight_banner() -> str:
    """Warn on the console when the source-policy table failed to load and the
    restrictive fallback (no minting, no contacts) is in effect."""
    from app.services import source_policy
    if source_policy.policies_loaded():
        return ""
    return (
        '<div style="background:#7f1d1d;color:#fff;padding:8px 14px;'
        'font-size:13px">⚠ data/source_policies.json missing or unreadable — '
        'running on the restrictive fallback policy (no people/commitment/'
        'claim minting, no contact extraction). Restore the file and restart.'
        '</div>')


def _adoption_console_chrome() -> str:
    """First-win toast, ambient unlock card, Report-a-problem (Workstreams 1+4)."""
    return r"""
<div id="mnemosToast" hidden style="position:fixed;right:18px;bottom:18px;z-index:40;
  max-width:340px;padding:14px 16px;border-radius:14px;background:#0b1320;color:#f8f6f1;
  box-shadow:0 12px 40px rgba(11,19,32,.28);font:14px/1.45 system-ui"></div>
<script>
(function(){
  const toast=document.getElementById('mnemosToast');
  function place(){
    // The recording pill tray (#mnemosRecBar) owns the bottom-right corner and
    // outranks the toast (z 70 vs 40) — lift the toast to sit above however
    // many pill rows are showing, instead of letting them stamp over it.
    if(!toast) return;
    try{
      const rb=document.getElementById('mnemosRecBar');
      const h=(rb&&rb.offsetHeight)?(rb.offsetHeight+28):18;
      toast.style.bottom=h+'px';
    }catch(e){}
  }
  function show(html){ if(!toast) return; toast.innerHTML=html; toast.hidden=false; place(); }
  async function pollNudge(){
    if(toast&&!toast.hidden) place();
    try{
      const d=await (await fetch('/first-run/nudge')).json();
      if(d.first_win && d.first_win.href){
        const href=d.first_win.href;
        const copy=d.first_win.has_facts
          ? 'Your meeting brief is ready — commitments have play buttons.'
          : 'Meeting closed. Open the transcript timeline (nothing extracted yet).';
        show('<strong>First brief</strong><p style="margin:6px 0 10px">'+copy+'</p>'
          +'<a href="'+href+'" style="color:#c9a227">Open meeting</a>'
          +' · <button type="button" id="toastAck" style="background:none;border:0;color:#bbb;cursor:pointer">dismiss</button>');
        const ack=document.getElementById('toastAck');
        if(ack) ack.onclick=async()=>{ await fetch('/first-run/nudge/ack',{method:'POST'}); toast.hidden=true; };
        return;
      }
      if(d.unlock && d.unlock.show){
        show('<strong>Between meetings</strong><p style="margin:6px 0 10px">'
          +(d.unlock.copy||'Optional always-on capture. Nothing is enabled by this card.')
          +'</p><a href="/onboarding" style="color:#c9a227">Review capture options</a>'
          +' · <button type="button" id="unlockAck" style="background:none;border:0;color:#bbb;cursor:pointer">not now</button>');
        const u=document.getElementById('unlockAck');
        if(u) u.onclick=async()=>{ await fetch('/first-run/unlock/ack',{method:'POST'}); toast.hidden=true; };
      }
    }catch(e){}
  }
  pollNudge();
  setInterval(pollNudge, 20000);
  const btn=document.getElementById('reportBtn');
  if(btn) btn.onclick=async()=>{
    const note=prompt('What went wrong? (saved locally — nothing is sent)')||'';
    const r=await fetch('/console/report',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({note})});
    const j=await r.json();
    alert(j.ok ? ('Saved zip:\\n'+j.path) : (j.detail||'report failed'));
  };
})();
</script>
"""


@router.get("/memory", response_class=HTMLResponse)
def memory_console_page() -> HTMLResponse:
    """The Memory Console — timeline, search, provenance, confidence."""
    page = _CONSOLE_PAGE.replace(
        '<button class="btn" onclick="load()">Refresh</button>',
        '<button class="btn" onclick="load()">Refresh</button>\n    '
        '<button class="btn" id="reportBtn" type="button">Report a problem</button>',
        1)
    page = page.replace(
        '<div class="layout">',
        _policy_preflight_banner() + _adoption_console_chrome() + '<div class="layout">',
        1)
    return _html_with_approval(page, next_url="/memory")


@router.get("/console", response_class=RedirectResponse)
def console_redirect() -> RedirectResponse:
    """Permanent redirect: /console HTML → canonical /memory.

    JSON/API under /console/* is unchanged (attention, economy, events, …).
    """
    return RedirectResponse(url="/memory", status_code=301)


# --- extracted facts: the review/train loop (Track A, step 5) --------------
def _provenance(d: dict) -> str:
    """A human-readable source line for a fact, e.g. 'audio · 2:14 PM'."""
    import time as _time

    st = d.get("source_time")
    mod = (d.get("source_modality") or "").split(":")[0] or "memory"
    when = ""
    if st:
        try:
            when = _time.strftime("%-I:%M %p", _time.localtime(st))
        except (ValueError, TypeError):
            when = _time.strftime("%I:%M %p", _time.localtime(st)).lstrip("0")
    label = {"audio": "heard", "vision": "seen"}.get(mod, mod)
    return f"{label}{(' · ' + when) if when else ''}"


def _fact_view(d: dict) -> dict:
    """Shape a joined fact row for the Console UI."""
    return {
        "fact_id": d["fact_id"], "kind": d["kind"], "text": d.get("text") or "",
        "status": d.get("status"), "review": d.get("review"),
        "owner": d.get("owner"), "from_person": d.get("from_person"),
        "to_person": d.get("to_person"), "due": d.get("due"),
        "confidence": d.get("confidence"),
        "source_span": d.get("source_span") or "",
        "source": _provenance(d), "source_event_id": d.get("source_event_id"),
        "source_audio": None,  # filled in by the route from the source event
        "enhanced_audio": None,
        "play_path": None,
        "source_transcript": "",
        "span_highlight": None,
        "playable": False,
        # Plan 4.1 — commitment lifecycle (null for tasks/claims).
        "commitment_state": d.get("commitment_state"),
        "completion_evidence_json": d.get("completion_evidence_json"),
        "last_surfaced": d.get("last_surfaced"),
        "counterparty_expects": d.get("counterparty_expects"),
    }


@router.get("/facts")
def facts_list(kind: str | None = None, status: str | None = None,
               review: str | None = None, limit: int = 200) -> dict:
    """Extracted facts for the Console — filter by kind (task|commitment|claim),
    lifecycle status, or review verdict ('none' = not yet reviewed)."""
    store = memory._ensure_store()
    rows = store.list_facts(kind=kind, status=status, review=review, limit=limit)
    views = [_fact_view(r) for r in rows]
    # Attach the source clip so a fact can be heard, not just read (plan 3.4).
    ids = [v["source_event_id"] for v in views if v.get("source_event_id")]
    if ids:
        from app.services.evidence_playback import clip_from_event, find_span
        emap = store.by_ids_map(ids)
        for v in views:
            ev = emap.get(v.get("source_event_id"))
            if ev is None:
                continue
            clip = clip_from_event(ev)
            v["source_audio"] = clip.get("audio_path")
            v["enhanced_audio"] = clip.get("enhanced_audio")
            v["play_path"] = clip.get("play_path")
            v["source_transcript"] = clip.get("transcript") or ""
            v["playable"] = bool(clip.get("play_path"))
            span = v.get("source_span") or ""
            hit = find_span(v["source_transcript"], span) if span else None
            if hit:
                v["span_highlight"] = {
                    "before": hit["before"],
                    "match": hit["match"],
                    "after": hit["after"],
                }
    return {"count": len(views), "facts": views}


@router.get("/facts/open_tasks")
def facts_open_tasks(limit: int = 100) -> dict:
    """The current open tasks (extracted from speech + pages)."""
    store = memory._ensure_store()
    return {"tasks": store.open_tasks(limit)}


class FactEdit(BaseModel):
    text: str


def _get_or_404(fact_id: int) -> dict:
    store = memory._ensure_store()
    d = store.get_fact(fact_id)
    if d is None:
        raise HTTPException(status_code=404, detail=f"no fact {fact_id}")
    return d


def _label_distill_outcome(fact: dict, outcome: str,
                           edited_text: str | None = None) -> None:
    """Thread a human fact verdict back onto the escalation distill trail.

    If the fact's source event was a vision frame that needed a parent-VLM
    escalation, the reviewer's approve/dismiss/edit is exactly the user_outcome
    label that row was waiting on (Part 1 of the retrain pipe). Best-effort —
    labeling must never break the review endpoints."""
    try:
        sev = fact.get("source_event_id")
        if not sev:
            return
        store = memory._ensure_store()
        ev = store.by_ids_map([int(sev)]).get(int(sev))
        if ev is None:
            return
        meta = ev.meta if isinstance(ev.meta, dict) else {}
        frame_path = meta.get("frame_path")
        if not frame_path:
            return
        from app.services.escalate_log import escalate_log
        escalate_log.set_user_outcome(outcome, frame_path=frame_path,
                                      time=getattr(ev, "time", None),
                                      edited_text=edited_text)
    except Exception as exc:
        print(f"[escalate_log] fact outcome label skipped ({exc}).")


def _harvest_fact_verdict(fact: dict, verdict: str,
                          edited_text: str | None = None) -> None:
    """Workstream A: every fact-review verdict is a learning pair (best-effort)."""
    try:
        from app.services import learning_store
        learning_store.record_fact_verdict(fact, verdict,
                                           edited_text=edited_text)
    except Exception as exc:
        print(f"[learning_store] fact harvest skipped ({exc}).")


@router.get("/profile", response_class=HTMLResponse)
def profile_ui() -> HTMLResponse:
    """You — the living user profile: see, correct, or forget what the system
    believes about its user. Cards reuse the /facts verdict endpoints."""
    from app.api.profile_page import PROFILE_PAGE
    return _html_with_approval(PROFILE_PAGE, next_url="/profile")


@router.get("/org/{entity_id}", response_class=HTMLResponse)
def org_ui(entity_id: int) -> HTMLResponse:
    """Living brief for one org/entity — people, facts, open work."""
    from app.api.org_page import ORG_PAGE
    return HTMLResponse(ORG_PAGE)


@router.get("/org/{entity_id}/data")
def org_data(entity_id: int) -> dict:
    """JSON payload for the org living brief (and tests)."""
    from app.services import entity_details, graph

    store = memory._ensure_store()
    e = store.get_entity(entity_id)
    if e is None:
        raise HTTPException(status_code=404, detail="no such entity")
    name = e.get("name") or ""
    edges = store.relations_of("entity", entity_id).get("in", [])
    fact_ids = [x["subj_id"] for x in edges if x.get("subj_type") == "fact"]
    fmap = store.facts_by_ids(list(dict.fromkeys(fact_ids)))
    facts = []
    for fid in dict.fromkeys(fact_ids):
        f = fmap.get(fid)
        if not f or (f.get("state") or "active") != "active" \
                or f.get("review") == "dismissed":
            continue
        facts.append({k: f.get(k) for k in
                      ("fact_id", "kind", "text", "status", "review",
                       "confidence", "updated_at")})
    facts.sort(key=lambda r: -(r.get("updated_at") or 0))
    mined = entity_details.mine(name, e.get("aliases") or [], facts)
    details = entity_details.merge(mined, store.entity_attrs(entity_id))
    org_people = graph.people_for_entity(store, name)
    people = org_people.get("people") or []
    # Open work mentioning the org name (grounding-style substring match).
    needle = name.casefold()
    aliases = [str(a).casefold() for a in (e.get("aliases") or []) if a]
    work = []
    for f in store.list_facts(limit=400, actionable=True):
        if f.get("kind") not in ("task", "commitment"):
            continue
        if (f.get("state") or "active") != "active" \
                or f.get("review") == "dismissed" or f.get("status") != "open":
            continue
        text = str(f.get("text") or "")
        low = text.casefold()
        if (needle and needle in low) or any(a and a in low for a in aliases):
            work.append({k: f.get(k) for k in
                         ("fact_id", "kind", "text", "status", "due",
                          "confidence", "updated_at")})
    work.sort(key=lambda r: -(r.get("updated_at") or 0))
    return {
        "entity": {k: e.get(k) for k in
                   ("id", "name", "kind", "aliases", "last_seen", "canonical_id")},
        "details": details,
        "people": people,
        "org_people": org_people,
        "facts": facts[:40],
        "work": work[:40],
    }


@router.get("/profile/data")
def profile_data() -> dict:
    """The user's identity core + self-facts + owned open work, for review."""
    from app.services import self_profile
    from app.services.identity import user_identity

    store = memory._ensure_store()
    ident = user_identity(store)
    about: list[dict] = []
    work: list[dict] = []
    pid = self_profile.self_person_id(store)
    if pid is not None:
        def _alive(f) -> bool:
            return bool(f and (f.get("state") or "active") == "active"
                        and f.get("review") != "dismissed")

        def _row(f) -> dict:
            return {k: f.get(k) for k in
                    ("fact_id", "kind", "text", "status", "review",
                     "confidence", "updated_at")}

        edges = store.relations_of("person", pid).get("out", [])
        self_ids = [e["obj_id"] for e in edges if e.get("obj_type") == "fact"
                    and e.get("predicate") == self_profile.SELF_PREDICATE]
        owned_ids = [e["obj_id"] for e in edges if e.get("obj_type") == "fact"
                     and e.get("predicate") != self_profile.SELF_PREDICATE]
        # Ownership is also written directly at extraction time (typed rows) —
        # edges only appear after a graph rebuild, so take the union.
        owned_ids += [t["fact_id"] for t in store.open_tasks(200)
                      if t.get("owner_person_id") == pid]
        fmap = store.facts_by_ids(list(dict.fromkeys(self_ids + owned_ids)))
        about = [_row(f) for fid in dict.fromkeys(self_ids)
                 if _alive(f := fmap.get(fid))]
        seen = {r["fact_id"] for r in about}
        for fid in dict.fromkeys(owned_ids):
            f = fmap.get(fid)
            if (fid not in seen and _alive(f) and f.get("status") == "open"
                    and f.get("kind") in ("task", "commitment")):
                work.append(_row(f))
        about.sort(key=lambda r: -(r.get("updated_at") or 0))
        work.sort(key=lambda r: -(r.get("updated_at") or 0))
    return {"identity": ident, "self_known": pid is not None,
            "about": about, "work": work}


class PersonName(BaseModel):
    name: str


class PersonAlias(BaseModel):
    alias: str


class PersonNote(BaseModel):
    text: str


@router.get("/people/list")
def people_list(include_hidden: bool = False, include_candidates: bool = True) -> dict:
    """Every person in memory, evidence-ranked — the People tab's roster.

    People v2: hides soft-merged / hide_from_people rows unless include_hidden.
    Candidates (low promotion) are included by default but tagged.
    """
    import time as _time
    from app.services.home_intelligence import person_score
    from app.services import score_v2
    from app.services import self_profile

    store = memory._ensure_store()
    now = _time.time()
    self_pid = self_profile.self_person_id(store)
    # WS-B: v2 ranks only when QUILL_SCORE_V2 is on AND the shadow soak
    # passed (7 clean nightlies). None -> v1, the shipped default.
    v2_scores = score_v2.live_scores(store, now)
    out = []
    for p in store.all_people():
        if not include_hidden and (
                p.get("hide_from_people") or p.get("canonical_person_id")):
            continue
        if not include_candidates and (p.get("promotion_state") or "") == "candidate":
            continue
        if v2_scores is not None:
            score = v2_scores.get(p["id"], 0.0)
        else:
            rel = store.relations_of("person", p["id"])
            score = person_score(rel.get("out") or [], p.get("last_seen"), now)
        out.append({"id": p["id"], "name": p["name"],
                    "weight": round(score, 1),
                    "last_seen": p.get("last_seen"),
                    "is_self": p["id"] == self_pid,
                    "promotion_state": p.get("promotion_state") or "candidate",
                    "actor_type": p.get("actor_type") or "human_person",
                    "interaction_strength": p.get("interaction_strength") or 0,
                    "from_calendar": False})
    # Exhaust: calendar-derived people rank above header-only.
    try:
        for row in out:
            attrs = store.person_attrs(int(row["id"]))
            row["from_calendar"] = bool(
                (attrs.get("exhaust_from_calendar") or {}).get("value"))
    except Exception:
        pass
    out.sort(key=lambda x: (
        -int(bool(x.get("from_calendar"))),
        -(x.get("interaction_strength") or 0),
        -x["weight"],
        x["name"].lower(),
    ))
    return {"people": out}


@router.get("/people/unresolved-mentions")
def people_unresolved_mentions(limit: int = 50) -> dict:
    """People v2 review queue: mentions that left identity open."""
    store = memory._ensure_store()
    try:
        rows = store.list_person_mentions(unresolved_only=True, limit=limit)
    except Exception:
        rows = []
    return {"count": len(rows), "mentions": rows}


@router.get("/people/{person_id}")
def person_detail(person_id: int) -> dict:
    """One person: aliases, connections, structured details (phone / email /
    role / org / team / location — mined from their facts, user edits win), and
    the facts that mention them (reviewable with the standard fact verdicts)."""
    from app.services import graph, person_details

    store = memory._ensure_store()
    p = store.get_person(person_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such person")
    edges = store.relations_of("person", person_id).get("out", [])
    fact_ids = [e["obj_id"] for e in edges if e.get("obj_type") == "fact"]
    fmap = store.facts_by_ids(list(dict.fromkeys(fact_ids)))
    facts = []
    for fid in dict.fromkeys(fact_ids):
        f = fmap.get(fid)
        if not f or (f.get("state") or "active") != "active" \
                or f.get("review") == "dismissed":
            continue
        facts.append({k: f.get(k) for k in
                      ("fact_id", "kind", "text", "status", "review",
                       "confidence", "updated_at")})
    facts.sort(key=lambda r: -(r.get("updated_at") or 0))
    ctx = {}
    try:
        ctx = graph.context_for_person(p["name"], store)
    except Exception:
        ctx = {}
    affiliations = (ctx.get("affiliations") or [])[:12]
    mined = person_details.mine(p["name"], p.get("aliases") or [], facts,
                                affiliations=affiliations)
    attrs = store.person_attrs(person_id)
    details = person_details.merge(mined, attrs)
    # People v2: evidence-linked contact points overlay mined/attrs.
    contact_points = []
    try:
        contact_points = store.list_contact_points(person_id, active_only=True)
        for cp in contact_points:
            key = cp.get("type")
            if key in ("email", "phone") and key not in details:
                details[key] = {
                    "value": cp.get("value_display"),
                    "source": "attributed",
                    "fact_id": cp.get("source_event_id"),
                    "quote": cp.get("evidence_quote"),
                    "verification_status": cp.get("verification_status"),
                    "confidence": cp.get("confidence"),
                }
    except Exception:
        contact_points = []
    detail_lists = person_details.detail_lists(
        merged=details, attrs=attrs,
        contact_points=contact_points, affiliations=affiliations)
    mentions = []
    try:
        mentions = store.list_person_mentions(person_id=person_id, limit=20)
    except Exception:
        mentions = []
    from app.services import self_profile
    return {**p, "facts": facts[:30], "details": details,
            "detail_lists": detail_lists,
            "contact_points": contact_points[:20],
            "mentions": [
                {k: m.get(k) for k in
                 ("mention_id", "raw_text", "normalized_text",
                  "resolution_status", "resolution_confidence",
                  "relationship_relevance", "observed_at", "event_id")}
                for m in mentions
            ],
            "is_self": person_id == self_profile.self_person_id(store),
            "affiliations": affiliations[:6],
            "discussed_with": (ctx.get("discussed_with") or [])[:6]}


@router.post("/people/{person_id}/rename")
def person_rename(person_id: int, body: PersonName) -> dict:
    store = memory._ensure_store()
    if not store.rename_person(person_id, body.name):
        raise HTTPException(
            status_code=400,
            detail="empty name, unknown person, or the name already belongs "
                   "to someone else (that would be a merge, not a rename)")
    from app.services import self_profile
    self_profile.reset()   # cached self node may have been renamed
    return {"ok": True, "id": person_id, "name": body.name.strip()}


@router.post("/people/{person_id}/alias")
def person_alias(person_id: int, body: PersonAlias) -> dict:
    import time as _time
    store = memory._ensure_store()
    if store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="no such person")
    alias = (body.alias or "").strip()
    if not alias:
        raise HTTPException(status_code=400, detail="alias required")
    store.touch_person(person_id, _time.time(), alias=alias)
    return {"ok": True, "id": person_id, "alias": alias}


@router.post("/people/{person_id}/note")
def person_note(person_id: int, body: PersonNote) -> dict:
    """A human-written fact about this person — stored as an APPROVED claim
    (the user asserted it directly) and linked to them in the graph."""
    import time as _time
    store = memory._ensure_store()
    p = store.get_person(person_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such person")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    if p["name"].lower() not in text.lower():
        text = f"About {p['name']}: {text}"
    now = _time.time()
    fid = store.add_claim(text, confidence=1.0, extracted_at=now)
    store.review_fact(fid, "approved")
    store.add_relation("person", person_id, "mentioned_in", "fact", fid,
                       origin="asserted", ts=now)
    try:
        memory.index_fact(fid, "claim", text, now)
    except Exception:
        pass
    return {"ok": True, "fact_id": fid, "text": text}


class PersonDetailField(BaseModel):
    key: str
    value: str = ""
    # set = replace primary attr (legacy); add = another value; remove = drop by ref
    op: str = "set"
    ref: str | None = None


def _assert_detail_claim(store, memory, person_id: int, name: str,
                         key: str, value: str, now: float) -> int:
    from app.services import person_details
    text = person_details.claim_text(key, name, value)
    fid = store.add_claim(text, confidence=1.0, extracted_at=now)
    store.review_fact(fid, "approved")
    store.add_relation("person", person_id, "mentioned_in", "fact", fid,
                       origin="asserted", ts=now)
    try:
        memory.index_fact(fid, "claim", text, now)
    except Exception:
        pass
    return fid


def _add_detail_side_effects(store, person_id: int, key: str, value: str,
                             now: float) -> None:
    """Mirror multi-value writes into contact_points / affiliation edges."""
    from app.services import person_details
    if key in ("phone", "email"):
        store.upsert_contact_point(
            person_id=person_id, type_=key, value_display=value,
            value_normalized=person_details.normalize_value(key, value) or value.lower(),
            confidence=1.0, attribution_method="user_assert",
            verification_status="user_verified", source_event_id=None,
            evidence_quote=None, discourse_role=None, ts=now,
            created_by="user", pipeline_version="people_details_multi")
    elif key in ("org", "team"):
        kind = "org"
        predicate = "works_at" if key == "org" else "member_of"
        eid = store.resolve_entity(value, kind, ts=now)
        if eid:
            store.add_relation("person", person_id, predicate, "entity", eid,
                               origin="asserted", confidence=1.0, ts=now)


@router.post("/people/{person_id}/detail")
def person_detail_set(person_id: int, body: PersonDetailField) -> dict:
    """Set, add, or remove a structured detail field.

    `op=set` (default): replace the primary override — empty value clears it so
    memory shows through again. `op=add`: keep existing values and append
    (phone/email → contact_points; org → works_at; team → member_of).
    `op=remove`: drop one row by `ref` (`attr:key`, `cp:id`, `rel:pred:id`).
    """
    import time as _time
    from app.services import person_details

    store = memory._ensure_store()
    p = store.get_person(person_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such person")
    key = (body.key or "").strip().lower()
    if key not in person_details.DETAIL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown field (one of: {', '.join(person_details.DETAIL_KEYS)})")
    op = (body.op or "set").strip().lower()
    if op not in ("set", "add", "remove"):
        raise HTTPException(status_code=400, detail="op must be set, add, or remove")
    now = _time.time()
    value = (body.value or "").strip()

    if op == "remove":
        ref = (body.ref or "").strip()
        if not ref:
            raise HTTPException(status_code=400, detail="ref required for remove")
        if ref.startswith("attr:"):
            old_fid = store.clear_person_attr(person_id, key)
            if old_fid:
                store.archive_fact(old_fid, now)
        elif ref.startswith("cp:"):
            try:
                cpid = int(ref.split(":", 1)[1])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="bad contact ref") from exc
            store.archive_contact_point(cpid)
            # If the archived value was also the primary attr, clear it.
            attr = store.person_attrs(person_id).get(key)
            if attr and person_details.normalize_value(
                    key, attr.get("value") or "") == person_details.normalize_value(
                    key, value or attr.get("value") or ""):
                # Prefer matching by value when provided.
                pass
            if attr and value and person_details.normalize_value(
                    key, attr["value"]) == person_details.normalize_value(key, value):
                old_fid = store.clear_person_attr(person_id, key)
                if old_fid:
                    store.archive_fact(old_fid, now)
        elif ref.startswith("rel:"):
            parts = ref.split(":")
            if len(parts) < 3:
                raise HTTPException(status_code=400, detail="bad relation ref")
            pred, eid_s = parts[1], parts[2]
            try:
                eid = int(eid_s)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="bad relation ref") from exc
            store.delete_relation("person", person_id, pred, "entity", eid)
            attr = store.person_attrs(person_id).get(key)
            if attr and value and person_details.normalize_value(
                    key, attr["value"]) == person_details.normalize_value(key, value):
                old_fid = store.clear_person_attr(person_id, key)
                if old_fid:
                    store.archive_fact(old_fid, now)
        elif ref.startswith("merged:"):
            # Memory-only row — nothing durable to delete; user can override via set.
            return {"ok": True, "key": key, "removed": False, "note": "memory-only"}
        else:
            raise HTTPException(status_code=400, detail="unknown ref kind")
        return {"ok": True, "key": key, "removed": True, "ref": ref}

    if op == "add":
        if key not in person_details.MULTI_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"{key} is single-valued — use op=set")
        if not value:
            raise HTTPException(status_code=400, detail="value required for add")
        fid = _assert_detail_claim(store, memory, person_id, p["name"], key, value, now)
        _add_detail_side_effects(store, person_id, key, value, now)
        # Seed primary attr when empty so merge/chat still see a primary.
        if key not in store.person_attrs(person_id):
            store.set_person_attr(person_id, key, value, fid, now)
        return {"ok": True, "key": key, "value": value, "op": "add", "fact_id": fid}

    # op == set (legacy primary replace / clear)
    if not value:
        old_fid = store.clear_person_attr(person_id, key)
        if old_fid:
            store.archive_fact(old_fid, now)
        return {"ok": True, "key": key, "cleared": True}
    fid = _assert_detail_claim(store, memory, person_id, p["name"], key, value, now)
    prev_fid = store.set_person_attr(person_id, key, value, fid, now)
    if prev_fid:
        store.supersede_fact(prev_fid, fid, now)
    if key in person_details.MULTI_KEYS:
        _add_detail_side_effects(store, person_id, key, value, now)
    return {"ok": True, "key": key, "value": value, "fact_id": fid}


@router.post("/people/{person_id}/forget")
def person_forget(person_id: int) -> dict:
    """Delete a person node (junk from speech noise, or someone the user wants
    gone). Facts they owned are detached, never deleted. The self node is
    protected — that's what Setup is for."""
    from app.services import self_profile
    store = memory._ensure_store()
    if store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="no such person")
    if person_id == self_profile.self_person_id(store):
        raise HTTPException(status_code=400,
                            detail="that's you — edit identity in Setup instead")
    gone = store.delete_person(person_id)
    return {"ok": True, "deleted": gone.get("canonical_name")}


class SoftMergeBody(BaseModel):
    absorbed_id: int
    reason: str = ""


@router.post("/people/{person_id}/soft-merge")
def people_soft_merge(person_id: int, body: SoftMergeBody) -> dict:
    """Reversible merge: absorbed redirects to survivor without deleting rows."""
    store = memory._ensure_store()
    if store.get_person(person_id) is None or store.get_person(body.absorbed_id) is None:
        raise HTTPException(status_code=404, detail="no such person")
    if person_id == body.absorbed_id:
        raise HTTPException(status_code=400, detail="cannot merge a person into self")
    mid = store.soft_merge_people(
        person_id, body.absorbed_id, reason=body.reason, actor="user")
    # Workstream A: a human-approved merge is identity-resolution ground truth.
    try:
        from app.services import learning_store
        learning_store.record_person_merge(
            store.get_person(person_id) or {},
            store.get_person(body.absorbed_id) or {},
            merge_id=mid, store=store)
    except Exception as exc:
        print(f"[learning_store] merge harvest skipped ({exc}).")
    # The merge's alias rules may now bind mentions that were left open —
    # sweep unowned tasks/commitments so the fix flows back onto facts.
    try:
        from app.services.worker import worker
        worker.enqueue("people_reattribute", unique=True)
    except Exception:
        pass
    return {"ok": True, "merge_id": mid, "survivor": person_id,
            "absorbed": body.absorbed_id}


@router.get("/people/{person_id}/action-gate")
def people_action_gate(person_id: int, contact_type: str = "email") -> dict:
    """Fail-closed check before approval-gated email/call/send."""
    from app.services.people_pipeline import agent_may_use_contact
    store = memory._ensure_store()
    return agent_may_use_contact(store, person_id, contact_type)


class EntityKind(BaseModel):
    kind: str


@router.get("/entities/list")
def entities_list() -> dict:
    """Every org / project / tool / place in memory, evidence-ranked —
    including noise rows, since this surface is where the user prunes them."""
    import time as _time
    from app.services.home_intelligence import entity_score
    from app.services import project_rollup

    store = memory._ensure_store()
    now = _time.time()
    try:
        homes = project_rollup.current(store)
    except Exception:
        homes = {}
    out = []
    for e in store.all_entities():
        rel = store.relations_of("entity", e["id"])
        score = entity_score(rel.get("in") or [], e.get("last_seen"), now)
        out.append({"id": e["id"], "name": e["name"],
                    "kind": e.get("kind") or "idea",
                    "weight": round(score, 1), "last_seen": e.get("last_seen"),
                    "project": homes.get(int(e["id"]))})
    out.sort(key=lambda x: (-x["weight"], x["name"].lower()))
    return {"entities": out}


@router.get("/entities/{entity_id}")
def entity_detail(entity_id: int) -> dict:
    """One entity: aliases, the facts about it, the people tied to it, and
    structured details (status / owner / url / location — mined from its
    facts, user edits win, each value carrying confidence + staleness)."""
    from app.services import entity_details

    store = memory._ensure_store()
    e = store.get_entity(entity_id)
    if e is None:
        raise HTTPException(status_code=404, detail="no such entity")
    edges = store.relations_of("entity", entity_id).get("in", [])
    fact_ids = [x["subj_id"] for x in edges if x.get("subj_type") == "fact"]
    person_ids = [x["subj_id"] for x in edges if x.get("subj_type") == "person"]
    fmap = store.facts_by_ids(list(dict.fromkeys(fact_ids)))
    facts = []
    for fid in dict.fromkeys(fact_ids):
        f = fmap.get(fid)
        if not f or (f.get("state") or "active") != "active" \
                or f.get("review") == "dismissed":
            continue
        facts.append({k: f.get(k) for k in
                      ("fact_id", "kind", "text", "status", "review",
                       "confidence", "updated_at")})
    facts.sort(key=lambda r: -(r.get("updated_at") or 0))
    pmap = {p["id"]: p["name"] for p in store.all_people()}
    people = [{"id": pid, "name": pmap.get(pid, "?")}
              for pid in dict.fromkeys(person_ids) if pid in pmap]
    mined = entity_details.mine(e["name"], e.get("aliases") or [], facts)
    details = entity_details.merge(mined, store.entity_attrs(entity_id))
    return {**e, "facts": facts[:30], "people": people[:8], "details": details}


@router.post("/entities/{entity_id}/rename")
def entity_rename(entity_id: int, body: PersonName) -> dict:
    store = memory._ensure_store()
    if not store.rename_entity(entity_id, body.name):
        raise HTTPException(
            status_code=400,
            detail="empty name, unknown entity, or the name already belongs "
                   "to another entity (that would be a merge)")
    return {"ok": True, "id": entity_id, "name": body.name.strip()}


@router.post("/entities/{entity_id}/alias")
def entity_alias(entity_id: int, body: PersonAlias) -> dict:
    import time as _time
    store = memory._ensure_store()
    if store.get_entity(entity_id) is None:
        raise HTTPException(status_code=404, detail="no such entity")
    alias = (body.alias or "").strip()
    if not alias:
        raise HTTPException(status_code=400, detail="alias required")
    store.touch_entity(entity_id, _time.time(), alias=alias)
    return {"ok": True, "id": entity_id, "alias": alias}


@router.post("/entities/{entity_id}/kind")
def entity_kind(entity_id: int, body: EntityKind) -> dict:
    store = memory._ensure_store()
    if store.get_entity(entity_id) is None:
        raise HTTPException(status_code=404, detail="no such entity")
    if not store.set_entity_kind(entity_id, body.kind):
        raise HTTPException(status_code=400, detail="invalid kind")
    return {"ok": True, "id": entity_id, "kind": (body.kind or "").strip().lower()}


@router.post("/entities/{entity_id}/note")
def entity_note(entity_id: int, body: PersonNote) -> dict:
    """A human-written fact about this org/project/tool — stored as an
    APPROVED claim and linked with an asserted `about` edge."""
    import time as _time
    store = memory._ensure_store()
    e = store.get_entity(entity_id)
    if e is None:
        raise HTTPException(status_code=404, detail="no such entity")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    if e["name"].lower() not in text.lower():
        text = f"About {e['name']}: {text}"
    now = _time.time()
    fid = store.add_claim(text, confidence=1.0, extracted_at=now)
    store.review_fact(fid, "approved")
    store.add_relation("fact", fid, "about", "entity", entity_id,
                       origin="asserted", ts=now)
    try:
        memory.index_fact(fid, "claim", text, now)
    except Exception:
        pass
    return {"ok": True, "fact_id": fid, "text": text}


@router.post("/entities/{entity_id}/detail")
def entity_detail_set(entity_id: int, body: PersonDetailField) -> dict:
    """Set or clear one structured entity detail. A set value is the user's
    word: stored as the override AND written as an APPROVED claim linked to
    the entity with an asserted `about` edge (so chat grounding sees the same
    truth), superseding the claim from any previous edit of the same field.
    An empty value clears the override — the mined value shows through."""
    import time as _time
    from app.services import entity_details

    store = memory._ensure_store()
    e = store.get_entity(entity_id)
    if e is None:
        raise HTTPException(status_code=404, detail="no such entity")
    key = (body.key or "").strip().lower()
    if key not in entity_details.DETAIL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown field (one of: {', '.join(entity_details.DETAIL_KEYS)})")
    now = _time.time()
    value = (body.value or "").strip()
    if not value:
        old_fid = store.clear_entity_attr(entity_id, key)
        if old_fid:
            store.archive_fact(old_fid, now)
        return {"ok": True, "key": key, "cleared": True}
    text = entity_details.claim_text(key, e["name"], value)
    fid = store.add_claim(text, confidence=1.0, extracted_at=now)
    store.review_fact(fid, "approved")
    store.add_relation("fact", fid, "about", "entity", entity_id,
                       origin="asserted", ts=now)
    prev_fid = store.set_entity_attr(entity_id, key, value, fid, now)
    if prev_fid:
        store.supersede_fact(prev_fid, fid, now)
    try:
        memory.index_fact(fid, "claim", text, now)
    except Exception:
        pass
    return {"ok": True, "key": key, "value": value, "fact_id": fid}


@router.post("/entities/{entity_id}/forget")
def entity_forget(entity_id: int) -> dict:
    """Delete an entity node (mis-extractions, noise). Facts stay, detached."""
    store = memory._ensure_store()
    if store.get_entity(entity_id) is None:
        raise HTTPException(status_code=404, detail="no such entity")
    gone = store.delete_entity(entity_id)
    return {"ok": True, "deleted": gone.get("canonical_name")}


class WorkAdd(BaseModel):
    kind: str                 # task | commitment
    text: str
    due: str | None = None
    owner: str | None = None  # a person's name; "me" = the user


class FactDue(BaseModel):
    due: str | None = None


def _work_brief(f: dict) -> dict:
    return {k: f.get(k) for k in
            ("fact_id", "kind", "text", "status", "review", "confidence",
             "owner", "from_person", "to_person", "due", "updated_at")}


@router.get("/work/list")
def work_list() -> dict:
    """Open tasks + commitments (freshest first) and the recently closed tail
    — the Tasks tab's working set. Dismissed rows never resurface here.
    Unreviewed screen-mined "work" is quarantined off the board (weak
    attribution); `screen_pending` counts it so the tab can point at the
    Memory Console review queue instead of silently hiding rows."""
    store = memory._ensure_store()
    rows = store.list_facts(limit=400, actionable=True)
    open_items, closed = [], []
    for f in rows:
        if f.get("kind") not in ("task", "commitment"):
            continue
        if (f.get("state") or "active") != "active" \
                or f.get("review") == "dismissed":
            continue
        if f.get("status") == "open":
            open_items.append(_work_brief(f))
        elif f.get("status") in ("done", "cancelled"):
            closed.append(_work_brief(f))
    open_items.sort(key=lambda r: -(r.get("updated_at") or 0))
    closed.sort(key=lambda r: -(r.get("updated_at") or 0))
    pending = sum(
        1 for f in store.list_facts(limit=400)
        if f.get("kind") in ("task", "commitment")
        and (f.get("state") or "active") == "active"
        and f.get("review") is None and f.get("status") == "open"
        and (f.get("event_source") or "") in store.WEAK_ATTRIBUTION_SOURCES)
    return {"open": open_items, "closed": closed[:8],
            "screen_pending": pending}


@router.post("/work/add")
def work_add(body: WorkAdd) -> dict:
    """A human-created task/commitment: enters as an APPROVED fact (conf 1.0),
    indexed for retrieval; owner names resolve through the graph ('me' = the
    self node), and the graph rebuild is chained so it maps immediately."""
    import time as _time
    from app.services import self_profile
    from app.services.clock import coerce_due

    kind = (body.kind or "").strip().lower()
    text = (body.text or "").strip()
    if kind not in ("task", "commitment"):
        raise HTTPException(status_code=400, detail="kind must be task|commitment")
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    store = memory._ensure_store()
    now = _time.time()
    due = coerce_due(body.due)
    owner = (body.owner or "").strip()
    pid = None
    if owner:
        pid = (self_profile.self_person_id(store)
               if self_profile.is_self_name(owner)
               else store.resolve_person(owner, ts=now))
    if kind == "task":
        fid = store.add_task(text, owner_person_id=pid, due=due,
                             confidence=1.0, extracted_at=now)
    else:
        fid = store.add_commitment(
            text, from_person_id=self_profile.self_person_id(store),
            to_person_id=pid, due=due, confidence=1.0, extracted_at=now)
    store.review_fact(fid, "approved")
    try:
        memory.index_fact(fid, kind, text, now)
    except Exception:
        pass
    try:
        from app.services.worker import worker
        worker.enqueue("graph", unique=True)
    except Exception:
        pass
    return {"ok": True, "fact_id": fid, "kind": kind, "text": text}


class WorkBulk(BaseModel):
    ids: list[int]
    action: str  # done | dismiss | reopen | due | edit
    due: str | None = None
    text: str | None = None  # for action=edit — same rewrite applied to all


@router.post("/work/bulk")
def work_bulk(body: WorkBulk) -> dict:
    """Apply one verb to many tasks/commitments (Profile Tasks multi-select).

    Per-id results so one bad id does not abort the rest. `dismiss` soft-deletes
    (same as the single Delete button); there is no hard purge here.
    """
    import time as _time

    action = (body.action or "").strip().lower()
    if action not in ("done", "dismiss", "reopen", "due", "edit"):
        raise HTTPException(
            status_code=400,
            detail="action must be done|dismiss|reopen|due|edit")
    ids = [int(i) for i in (body.ids or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="ids required")
    if len(ids) > 200:
        raise HTTPException(status_code=400, detail="at most 200 ids per call")
    if action == "edit":
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text required for edit")
    store = memory._ensure_store()
    now = _time.time()
    results = []
    for fid in ids:
        try:
            fact = store.get_fact(fid)
            if fact is None:
                results.append({"fact_id": fid, "ok": False, "error": "not found"})
                continue
            if fact.get("kind") not in ("task", "commitment"):
                results.append({"fact_id": fid, "ok": False,
                                "error": "not a task/commitment"})
                continue
            if action == "done":
                store.set_fact_status(fid, "done")
            elif action == "reopen":
                store.set_fact_status(fid, "open")
            elif action == "dismiss":
                store.review_fact(fid, "dismissed")
                _label_distill_outcome(fact, "rejected")
                _harvest_fact_verdict(fact, "dismissed")
            elif action == "due":
                if not store.set_fact_due(fid, body.due, now):
                    results.append({"fact_id": fid, "ok": False,
                                    "error": "due failed"})
                    continue
            elif action == "edit":
                old_text = fact.get("text") or fact.get("source_span") or ""
                if not store.edit_fact_text(fid, text):
                    results.append({"fact_id": fid, "ok": False,
                                    "error": "edit failed"})
                    continue
                try:
                    from app.services import provenance as _prov
                    sev = fact.get("source_event_id")
                    if sev:
                        _prov.append_correction(
                            int(sev), _prov.USER_EDIT,
                            before=old_text, after=text,
                            note=f"fact #{fid} bulk-corrected")
                except Exception:
                    pass
                _label_distill_outcome(fact, "edited", edited_text=text)
                _harvest_fact_verdict(fact, "edited", edited_text=text)
            results.append({"fact_id": fid, "ok": True})
        except Exception as exc:
            results.append({"fact_id": fid, "ok": False, "error": str(exc)})
    ok_n = sum(1 for r in results if r.get("ok"))
    return {"ok": ok_n == len(results), "action": action,
            "updated": ok_n, "results": results}


@router.post("/facts/{fact_id}/due")
def fact_due(fact_id: int, body: FactDue) -> dict:
    import time as _time
    _get_or_404(fact_id)
    if not memory._ensure_store().set_fact_due(fact_id, body.due, _time.time()):
        raise HTTPException(status_code=400, detail="not a task/commitment")
    return {"ok": True, "fact_id": fact_id, "due": (body.due or "").strip() or None}


@router.post("/facts/{fact_id}/reopen")
def fact_reopen(fact_id: int) -> dict:
    """Bring a done/cancelled item back to the open board."""
    fact = _get_or_404(fact_id)
    memory._ensure_store().set_fact_status(fact_id, "open")
    refreshed = memory._ensure_store().get_fact(fact_id) or fact
    return {"ok": True, "fact_id": fact_id, "status": "open",
            "commitment_state": refreshed.get("commitment_state")}


@router.post("/facts/{fact_id}/approve")
def fact_approve(fact_id: int) -> dict:
    fact = _get_or_404(fact_id)
    memory._ensure_store().review_fact(fact_id, "approved")
    _label_distill_outcome(fact, "accepted")
    _harvest_fact_verdict(fact, "accepted")
    refreshed = memory._ensure_store().get_fact(fact_id) or fact
    return {"ok": True, "fact_id": fact_id, "review": "approved",
            "commitment_state": refreshed.get("commitment_state")}


@router.post("/facts/{fact_id}/dismiss")
def fact_dismiss(fact_id: int) -> dict:
    """Kill a hallucinated/irrelevant fact — marks it dismissed and cancels its
    task/commitment. The human signal that keeps the timeline trustworthy."""
    fact = _get_or_404(fact_id)
    memory._ensure_store().review_fact(fact_id, "dismissed")
    _label_distill_outcome(fact, "rejected")
    _harvest_fact_verdict(fact, "dismissed")
    refreshed = memory._ensure_store().get_fact(fact_id) or fact
    return {"ok": True, "fact_id": fact_id, "review": "dismissed",
            "commitment_state": refreshed.get("commitment_state")}


class BatchReviewBody(BaseModel):
    fact_ids: list[int]
    verb: str  # approve | dismiss


@router.post("/facts/batch_review")
def facts_batch_review(body: BatchReviewBody) -> dict:
    """People v3 WS-E session sweep: one gesture reviews a whole group of
    facts. Reuses the single-fact handlers so the distill labeling and
    commitment side effects stay identical to a one-by-one review."""
    if body.verb not in ("approve", "dismiss"):
        raise HTTPException(400, detail="verb must be approve|dismiss")
    reviewed, missing = [], []
    for fid in body.fact_ids[:500]:
        try:
            if body.verb == "approve":
                fact_approve(int(fid))
            else:
                fact_dismiss(int(fid))
            reviewed.append(int(fid))
        except HTTPException:
            missing.append(int(fid))
    return {"ok": True, "verb": body.verb,
            "reviewed": len(reviewed), "missing": missing}


@router.get("/facts/review_queue")
def facts_review_queue(limit: int = 300) -> dict:
    """Unreviewed ACTIVE facts grouped by session (WS-E sweep view): one bad
    meeting is one group with its fact ids, not thirty loose rows."""
    store = memory._ensure_store()
    rows = store.list_facts(review="none", limit=limit)
    views = [_fact_view(r) for r in rows]
    ev_ids = [v["source_event_id"] for v in views if v.get("source_event_id")]
    emap = store.by_ids_map(ev_ids) if ev_ids else {}
    times = {eid: (e.get("time") or 0.0) for eid, e in emap.items()}
    stamps = [t for t in times.values() if t]
    sessions = (store.sessions_in_range(min(stamps), max(stamps))
                if stamps else [])

    def _session_for(t: float | None):
        if not t:
            return None
        for s in sessions:
            if s["start"] <= t <= s["end"]:
                return s
        return None

    groups: dict = {}
    for v in views:
        s = _session_for(times.get(v.get("source_event_id")))
        key = s["id"] if s else 0
        g = groups.get(key)
        if g is None:
            label = ((s.get("text") or "")[:120] if s else "(no session)")
            g = groups[key] = {"session_id": key or None,
                               "start": s["start"] if s else None,
                               "end": s["end"] if s else None,
                               "label": label, "fact_ids": [], "facts": []}
        g["fact_ids"].append(v["id"])
        g["facts"].append(v)
    ordered = sorted(groups.values(),
                     key=lambda g: -(g["start"] or 0.0))
    return {"count": len(views), "groups": ordered}


@router.get("/console/queue_slo")
def console_queue_slo() -> dict:
    """WS-E queue SLO: unreviewed depth + age vs targets (depth < 25,
    age p50 < 48h). Over target = extraction is too chatty — a signal,
    not a UI problem."""
    from app.services import queue_hygiene
    return queue_hygiene.queue_slo(memory._ensure_store())


@router.post("/facts/{fact_id}/done")
def fact_done(fact_id: int) -> dict:
    fact = _get_or_404(fact_id)
    memory._ensure_store().set_fact_status(fact_id, "done")
    refreshed = memory._ensure_store().get_fact(fact_id) or fact
    return {"ok": True, "fact_id": fact_id, "status": "done",
            "commitment_state": refreshed.get("commitment_state")}


class CommitmentTransitionBody(BaseModel):
    to_state: str
    reason: str | None = None
    evidence: dict | None = None
    actor: str = "user"


@router.post("/facts/{fact_id}/transition")
def fact_transition(fact_id: int, body: CommitmentTransitionBody) -> dict:
    """Plan 4.1 — legal commitment state transition with optional evidence."""
    fact = _get_or_404(fact_id)
    if fact.get("kind") != "commitment":
        raise HTTPException(status_code=400,
                            detail="transition is only for commitments")
    try:
        out = memory._ensure_store().transition_commitment(
            fact_id, body.to_state,
            reason=body.reason, evidence=body.evidence,
            actor=body.actor or "user")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return out


@router.get("/facts/{fact_id}/transitions")
def fact_transitions(fact_id: int, limit: int = 50) -> dict:
    """Commitment transition history (plan 4.1)."""
    fact = _get_or_404(fact_id)
    if fact.get("kind") != "commitment":
        return {"fact_id": fact_id, "transitions": []}
    rows = memory._ensure_store().list_commitment_transitions(
        fact_id, limit=limit)
    return {"fact_id": fact_id, "count": len(rows), "transitions": rows}


@router.post("/facts/{fact_id}/edit")
def fact_edit(fact_id: int, body: FactEdit) -> dict:
    """Correct a mis-extracted fact's text (marks it reviewed=edited)."""
    fact = _get_or_404(fact_id)
    old_text = fact.get("text") or fact.get("source_span") or ""
    ok = memory._ensure_store().edit_fact_text(fact_id, body.text)
    if not ok:
        raise HTTPException(status_code=400, detail="empty or unapplied edit")
    # #12: a human edit is ground truth — thread it back onto the SOURCE utterance's
    # provenance chain, so the recording shows the transcript AND the human's fix.
    try:
        from app.services import provenance as _prov
        sev = fact.get("source_event_id")
        if sev:
            _prov.append_correction(int(sev), _prov.USER_EDIT,
                                    before=old_text, after=body.text,
                                    note=f"fact #{fact_id} corrected")
    except Exception:
        pass
    _label_distill_outcome(fact, "edited", edited_text=body.text)
    _harvest_fact_verdict(fact, "edited", edited_text=body.text)
    return {"ok": True, "fact_id": fact_id, "review": "edited", "text": body.text}


# --- reflections: durable intelligence over the facts (the reflect loop) ----
def _reflection_view(store, reflection: dict) -> dict:
    """Shape a reflection header + its insights for the Console, hydrating each
    insight's cited fact ids into readable evidence (provenance, not an oracle)."""
    items = store.reflection_items(reflection["id"])
    all_ids = sorted({i for it in items for i in it.get("source_fact_ids", [])})
    fmap = store.facts_by_ids(all_ids) if all_ids else {}
    views = []
    for it in items:
        evidence = []
        for fid in it.get("source_fact_ids", []):
            fr = fmap.get(fid)
            if fr:
                evidence.append({"fact_id": fid,
                                 "text": fr.get("text") or fr.get("source_span") or "",
                                 "source": _provenance(fr)})
        views.append({
            "id": it["id"], "kind": it["kind"], "text": it["text"],
            "detail": it.get("detail") or "", "subject": it.get("subject") or "",
            "confidence": it.get("confidence"), "review": it.get("review"),
            "converted_fact_id": it.get("converted_fact_id"), "evidence": evidence,
        })
    return {
        "id": reflection["id"], "scope": reflection["scope"],
        "summary": reflection.get("summary") or "", "model": reflection.get("model"),
        "confidence": reflection.get("confidence"),
        "period_start": reflection.get("period_start"),
        "period_end": reflection.get("period_end"),
        "created_at": reflection.get("created_at"), "items": views,
    }


@router.post("/reflect/run")
def reflect_run(scope: str = "daily") -> dict:
    """Run a reflection now (manual trigger — Mnemos has no cron yet). v1 supports
    the daily scope; returns the reflection so the caller can render it."""
    from app.services.reflector import reflector

    if scope != "daily":
        raise HTTPException(status_code=400,
                            detail=f"scope {scope!r} not yet supported (v1: daily)")
    res = reflector.reflect_daily()
    store = memory._ensure_store()
    if res.get("reflection_id"):
        res["reflection"] = _reflection_view(store, store.get_reflection(res["reflection_id"]))
    return {"ok": True, **res}


@router.get("/reflections")
def reflections_latest(scope: str = "daily", id: int | None = None) -> dict:
    """The latest reflection of a scope (or a specific one by id), with its
    insights hydrated for review. This is the Console's Reflection view."""
    store = memory._ensure_store()
    header = store.get_reflection(id) if id else store.latest_reflection(scope)
    if header is None:
        return {"reflection": None}
    return {"reflection": _reflection_view(store, header)}


@router.get("/reflections/list")
def reflections_list(scope: str | None = None, limit: int = 30) -> dict:
    store = memory._ensure_store()
    return {"reflections": store.list_reflections(scope=scope, limit=limit)}


def _item_or_404(item_id: int) -> dict:
    store = memory._ensure_store()
    it = store.get_reflection_item(item_id)
    if it is None:
        raise HTTPException(status_code=404, detail=f"no reflection item {item_id}")
    return it


def _harvest_reflection_verdict(item: dict, verdict: str,
                                edited_text: str | None = None) -> None:
    """Workstream A: audit/insight verdicts are learning pairs (best-effort)."""
    try:
        from app.services import learning_store
        learning_store.record_reflection_verdict(item, verdict,
                                                 edited_text=edited_text)
    except Exception as exc:
        print(f"[learning_store] reflection harvest skipped ({exc}).")


@router.post("/reflection_items/{item_id}/approve")
def reflection_item_approve(item_id: int) -> dict:
    item = _item_or_404(item_id)
    memory._ensure_store().review_reflection_item(item_id, "approved")
    _harvest_reflection_verdict(item, "accepted")
    return {"ok": True, "item_id": item_id, "review": "approved"}


@router.post("/reflection_items/{item_id}/dismiss")
def reflection_item_dismiss(item_id: int) -> dict:
    item = _item_or_404(item_id)
    memory._ensure_store().review_reflection_item(item_id, "dismissed")
    _harvest_reflection_verdict(item, "dismissed")
    return {"ok": True, "item_id": item_id, "review": "dismissed"}


@router.post("/reflection_items/{item_id}/edit")
def reflection_item_edit(item_id: int, body: FactEdit) -> dict:
    item = _item_or_404(item_id)
    ok = memory._ensure_store().edit_reflection_item_text(item_id, body.text)
    if not ok:
        raise HTTPException(status_code=400, detail="empty or unapplied edit")
    _harvest_reflection_verdict(item, "edited", edited_text=body.text)
    return {"ok": True, "item_id": item_id, "review": "edited", "text": body.text}


@router.post("/reflection_items/{item_id}/convert")
def reflection_item_convert(item_id: int) -> dict:
    """The human-gated bridge: turn an insight (usually a recommendation) into a
    real open task. Nothing auto-mutates the tasks loop — this is the only path."""
    import time as _time

    it = _item_or_404(item_id)
    if it.get("converted_fact_id"):
        raise HTTPException(status_code=409,
                            detail=f"already converted to task {it['converted_fact_id']}")
    store = memory._ensure_store()
    now = _time.time()
    fid = store.add_task(it["text"], source_event_id=None,
                         source_span=it["text"], confidence=it.get("confidence"),
                         extracted_at=now)
    try:  # index the new task into semantic memory, like the extractor does
        memory.index_fact(fid, "task", it["text"], now)
    except Exception as exc:
        print(f"[reflect] convert index skipped ({exc}).")
    store.set_reflection_item_converted(item_id, fid)
    return {"ok": True, "item_id": item_id, "fact_id": fid}


class ChatIn(BaseModel):
    message: str
    dry_run: str | None = None   # plan|navigate|draft|approval|full|autonomous
    # Optional notes from the Chat UI "Add context" panel — authoritative for
    # this turn; shown cleanly in the bubble, merged into the agent goal.
    context: str | None = None
    # Optional study mode for this turn (also sticky via POST /chat/mode).
    mode: str | None = None


class ChatModeBody(BaseModel):
    mode: str | None = None   # registry id, or "clear"/"general" to reset


@router.get("/chat/mode")
def chat_mode_get() -> dict:
    """Sticky student/study chat mode + full registry for the /ui picker."""
    from app.services import agent_chat_mode as _smode
    cur = _smode.current()
    return {
        "id": cur["id"],
        "label": cur["label"],
        "posture": cur["posture"],
        "source": cur.get("source"),
        "until": cur.get("until"),
        "registry": _smode.registry(),
    }


@router.post("/chat/mode")
def chat_mode_set(body: ChatModeBody) -> dict:
    """Manual study mode. TTL 2h; 'clear' resets to general."""
    from app.services import agent_chat_mode as _smode
    try:
        mode = _smode.set_manual(body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "mode": mode}


class LearningPracticeIn(BaseModel):
    concept: str
    correct: bool = True


@router.get("/learning/concepts")
def learning_concepts(limit: int = 12) -> dict:
    """Weak + recent study concepts from the Learning Memory Engine."""
    from app.services import learning_memory as _lme
    from app.services import memory as _mem

    store = _mem._ensure_store()
    lim = max(1, min(int(limit or 12), 40))
    return {
        "ok": True,
        "weak": _lme.weak_concepts(store, limit=lim),
        "recent": _lme.recent_concepts(store, limit=lim),
    }


@router.post("/learning/practice")
def learning_practice(body: LearningPracticeIn) -> dict:
    """Record a practice outcome for a named concept."""
    from app.services import learning_memory as _lme
    from app.services import memory as _mem

    name = (body.concept or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="concept required")
    store = _mem._ensure_store()
    eid = _lme.upsert_concept(store, name)
    if not eid:
        raise HTTPException(status_code=400, detail="could not resolve concept")
    result = _lme.record_practice(store, eid, bool(body.correct))
    snap = _lme.concept_snapshot(store, eid)
    return {"ok": True, "result": result, "concept": snap}


def _attach_user_context(message: str, context: str | None) -> tuple[str, str]:
    """Return (agent_goal, display_message). Context is UI-only sticky notes."""
    display = (message or "").strip()
    ctx = (context or "").strip()
    if not ctx or not display:
        return display, display
    agent_goal = (
        "USER-PROVIDED CONTEXT (authoritative for this turn — prefer over "
        "conflicting retrieved memories when they disagree):\n"
        f"{ctx}\n\n"
        f"User request: {display}"
    )
    return agent_goal, display


@router.post("/chat/attach")
async def chat_attach(file: UploadFile = File(...)) -> dict:
    """Upload a document or photo from Chat → Context.

    The file is saved under data/uploads/, ingested into the event timeline,
    and indexed for semantic search (source='chat.attach'). Fact mining runs
    in a background thread so a multi-chunk PDF cannot freeze the live server.
    The response includes a `context` snippet the Chat UI merges into the next
    message's sticky context.
    """
    import asyncio

    from app.services import attachments

    name = file.filename or "file"
    try:
        data = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"read failed: {exc}") from exc
    # PDF parse + DB write still belong off the event loop (pypdf can be slow).
    result = await asyncio.to_thread(attachments.ingest_bytes, name, data)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "ingest failed")
    return result


@router.post("/chat")
def chat(body: ChatIn) -> dict:
    """Dispatch a chat turn to the browser agent (the hear -> act loop).

    Non-blocking: the agent routes the message (answer directly vs. drive the
    browser), grounded in Mnemos's memory, on its own thread. Poll /chat/poll for
    progress, results, and any approval/ask_human prompts. Set QUILL_AGENT=0 to
    fall back to the memory-only retriever.

    Dry-run posture (how far the agent may go this turn) can be set with the
    `dry_run` field or an inline `/plan|/navigate|/draft|/approval|/full` prefix
    on the message; omitted = the server default (AGENT_DRY_RUN).

    Optional `context` (Chat UI "Add context") is merged into the agent goal
    but the chat bubble still shows only `message`.

    Optional `mode` updates the sticky study mode and is passed into the agent
    for this turn (lecture notes, homework, etc.).
    """
    from app.services import agent_chat_mode as _smode

    if body.mode is not None and str(body.mode).strip():
        try:
            _smode.set_manual(body.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    study_mode = _smode.current()["id"]

    agent_goal, display = _attach_user_context(body.message, body.context)
    # Learning Memory: short homework/quiz verdicts update mastery (best-effort).
    if study_mode in ("homework", "study_quiz"):
        try:
            from app.services import learning_memory as _lme
            from app.services import memory as _mem
            _lme.apply_chat_verdict(_mem._ensure_store(), display)
        except Exception as exc:
            print(f"[learning_memory] chat verdict skipped ({exc}).")
    # Typed statements are memory too: store + queue extraction so a new
    # person / commitment / tool told to the chat reaches the graph without
    # being spoken aloud. Best-effort and non-blocking (LLM work runs on the
    # job worker); short approvals/verdicts are filtered inside.
    try:
        from app.services import chat_ingest
        chat_ingest.ingest(display)
    except Exception as exc:
        print(f"[chat_ingest] hook skipped ({exc}).")
    if _agent_disabled():
        return llm.answer(agent_goal)
    # Drop stale yes/no offers so they can't swallow a fresh instruction.
    agent.worker.expire_stale_offers()
    with agent.worker.lock:
        since = agent.worker.next_id
        pending = (agent.worker.awaiting or agent.worker.awaiting_fast
                   or agent.worker.pending_todo is not None)
    # Approvals / offer replies ignore sticky context — only the short message.
    if pending:
        return {**agent.worker.handle_reply(display), "since": since}
    # Bare yes/no with nothing pending: never route it as a goal (the router
    # would just re-answer the previous result) — accept/decline the last
    # reply's trailing offer, or say that nothing is waiting.
    idle = agent.worker.handle_idle_verdict(display)
    if idle is not None:
        return {**idle, "since": since}
    # Calendar-add intent -> approval-gated iCloud write (skips the browser
    # agent). Only a CREATE request is intercepted; "what's on my calendar?"
    # falls through to normal grounding, which already sees synced events.
    try:
        from app.services import calendar_intent
        if calendar_intent.looks_like_calendar_add(display):
            event = calendar_intent.parse(display)
            if event:
                from app.services import icloud_account
                agent.worker._emit("user", display)
                if not icloud_account.status()["connected"]:
                    agent.worker._emit(
                        "result", "To add calendar events I need your iCloud "
                        "connected — do it once on the Phone page (/phone), then "
                        "ask me again.")
                    return {"ok": True, "routed": "calendar_unconnected",
                            "since": since}
                agent.worker.propose_calendar(event)
                return {"ok": True, "routed": "calendar_offer", "since": since}
    except Exception as exc:
        print(f"[calendar_intent] skipped ({exc}).")
    # Team ask ("ask sarah: are the slides done?") -> the peer channel, not
    # the browser agent. Deterministic: only fires when the addressee resolves
    # to a PAIRED peer. The ask runs on a background thread (the teammate's
    # compose/approval can take a while); every outcome surfaces in chat.
    try:
        from app.services import peer_channel
        team = peer_channel.parse_team_ask(display)
        if team:
            agent.worker._emit("user", display)
            if team.get("fanout"):
                from app.services import team_layer
                if team.get("unknown"):
                    agent.worker._emit(
                        "result",
                        f"No team named {team.get('team_name')!r} yet — "
                        "create it on the Team page (/peer).")
                    return {"ok": True, "routed": "peer_team_unknown",
                            "since": since}
                if not team.get("peer_ids"):
                    agent.worker._emit(
                        "result",
                        f"The {team.get('team_name') or 'that'} team has no "
                        "paired members yet — add them on /peer.")
                    return {"ok": True, "routed": "peer_team_empty",
                            "since": since}
                team_layer.chat_team_ask_async(
                    team["team_slug"], team["question"],
                    team.get("kind", "question"))
                n = len(team["peer_ids"])
                verb = ("Handing off to" if team.get("kind") == "handoff"
                        else "Asking")
                agent.worker._emit(
                    "result",
                    f"{verb} the {team.get('team_name')} team "
                    f"({n} teammate{'s' if n != 1 else ''})…")
                return {"ok": True, "routed": "peer_team_ask", "since": since}
            peer_channel.chat_ask_async(team["peer_id"], team["question"],
                                        team.get("kind", "question"))
            verb = ("Handing off to" if team.get("kind") == "handoff"
                    else "Asking")
            agent.worker._emit("result",
                               f"{verb} {team['peer_name']}'s Mnemos…")
            return {"ok": True, "routed": "peer_ask", "since": since}
    except Exception as exc:
        print(f"[peer_intent] skipped ({exc}).")
    # Trigger authoring ("whenever I make progress on X, offer to email Z") ->
    # compile + backtest on a background thread, then an approval card. Nothing
    # persists until the user answers yes on the draft.
    try:
        from app.services.triggers import authoring as trigger_authoring
        if trigger_authoring.looks_like_trigger_request(display):
            agent.worker._emit("user", display)
            agent.worker._emit(
                "result", "Designing that trigger — I'll show you a draft "
                "to approve in a moment…")
            trigger_authoring.author_async(display)
            return {"ok": True, "routed": "trigger_draft", "since": since}
    except Exception as exc:
        print(f"[trigger_intent] skipped ({exc}).")
    agent.worker.send(agent_goal, dry_run=body.dry_run, display=display,
                      study_mode=study_mode)
    return {"ok": True, "routed": "goal", "since": since, "mode": study_mode}

class DesktopIn(BaseModel):
    message: str
    dry_run: str | None = None   # plan|navigate|draft|approval|full|autonomous


@router.post("/desktop")
def desktop(body: DesktopIn) -> dict:
    """Dispatch a goal straight to the guarded desktop agent, bypassing the
    web-vs-desktop router.

    Use this when you already know the task is a desktop/OS action — open an
    allowlisted app (e.g. Cursor), make a project folder, run an allowlisted
    build command. Every mutating step is jailed to the desktop sandbox and
    passes the approval gate (surfaces as an `ask` on /chat/poll, answered via
    /chat/answer or a yes/no on /chat). Non-blocking; poll /chat/poll for
    progress and prompts, same as /chat. QUILL_AGENT=0 disables the agent.
    """
    if _agent_disabled():
        return {"ok": False, "error": "agent disabled (QUILL_AGENT=0)"}
    with agent.worker.lock:
        since = agent.worker.next_id
    agent.worker.send(body.message, dry_run=body.dry_run, surface="desktop")
    return {"ok": True, "routed": "desktop", "since": since}


class PhoneIn(BaseModel):
    message: str
    dry_run: str | None = None


@router.post("/phone")
def phone(body: PhoneIn) -> dict:
    """Dispatch a goal straight to Phone Link (iPhone SMS on Windows), bypassing
    the router. Use when you already know the task is a text/call/reply action.
    Non-blocking; poll /chat/poll for progress and approval prompts."""
    if _agent_disabled():
        return {"ok": False, "error": "agent disabled (QUILL_AGENT=0)"}
    with agent.worker.lock:
        since = agent.worker.next_id
    agent.worker.send(body.message, dry_run=body.dry_run, surface="phone_link")
    return {"ok": True, "routed": "phone_link", "since": since}


# --- Desktop Access panel (strategic doc #5): make the allowlist visible ----
class DesktopAccessToggle(BaseModel):
    app: str
    disabled: bool


class DesktopAccessLaunch(BaseModel):
    app: str


@router.get("/console/desktop-access")
def console_desktop_access() -> dict:
    """The Desktop Access read-model: environment posture + one row per
    allowlisted app (installed?, resolved path, capabilities, UI control, risk,
    disabled?). Everything the desktop agent may do, made inspectable."""
    from desktop_agent import access
    return access.desktop_access_state()


@router.get("/console/desktop-access/recent")
def console_desktop_access_recent(limit: int = 10) -> dict:
    """The last N audited desktop actions (newest first) — the panel's activity
    feed, sourced from the same append-only audit log the driver writes."""
    from desktop_agent import access
    return {"recent": access.recent_actions(limit)}


@router.post("/console/desktop-access/toggle")
def console_desktop_access_toggle(body: DesktopAccessToggle) -> dict:
    """Enable/disable an app. Disabling is enforced by the driver — a disabled
    app's launch is refused — so this toggle is real, not cosmetic."""
    from desktop_agent import access
    if not access.set_app_disabled(body.app, body.disabled):
        raise HTTPException(status_code=404, detail=f"unknown app {body.app!r}")
    return access.desktop_access_state()


@router.post("/console/desktop-access/test-launch")
def console_desktop_access_test_launch(body: DesktopAccessLaunch) -> dict:
    """Test-launch an app straight from the panel (the click is the approval).
    Still goes through the guarded driver: allowlist + disable + jail all apply."""
    try:
        from desktop_agent import DesktopDriver
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "detail": f"desktop control unavailable: {exc}"}
    driver = DesktopDriver(on_log=lambda s: None, on_approve=lambda *a, **k: True)
    res = driver.launch_app(body.app)
    return {"ok": bool(res.get("ok")), "detail": res.get("detail", "")}


@router.get("/console/desktop-metrics")
def console_desktop_metrics(window_s: float | None = None) -> dict:
    """Desktop reliability metrics computed from the audit log (doc #6):
    launch/run success rates, refusals bucketed by reason, per-task action
    counts + budget exhaustion, repeated-failure loops, and the safety counters
    (jail escapes, unknown apps, blocked verbs, shell attempts). `window_s`
    restricts to recent activity."""
    from desktop_agent import telemetry
    return telemetry.desktop_metrics(window_s=window_s)


@router.get("/desktop-access", response_class=HTMLResponse)
def desktop_access_page() -> HTMLResponse:
    """The Desktop Access panel."""
    return _html_with_approval(_DESKTOP_ACCESS_PAGE, next_url="/desktop-access")


@router.get("/chat/poll")
def chat_poll(since: int = 0) -> dict:
    """Tail the agent's event log (progress/result/ask/error) since an id."""
    if _agent_disabled():
        return {"events": [], "state": {"error": "agent disabled (QUILL_AGENT=0)"}}
    events, state = agent.worker.snapshot(since)
    return {"events": events, "state": state}


@router.get("/chat", response_class=HTMLResponse)
def chat_ui() -> HTMLResponse:
    """Live chat — watch capture, see offers, reply. (POST /chat remains the API.)"""
    return _html_with_approval(_CHAT_PAGE, next_url="/chat")


@router.get("/ui", response_class=RedirectResponse)
def chat_ui_redirect() -> RedirectResponse:
    """Permanent redirect: /ui → canonical /chat."""
    return RedirectResponse(url="/chat", status_code=301)


# --- ghost browser: the agent's live view, streamed into the chat pane ------
@router.get("/agent/ghost/status")
def ghost_status() -> dict:
    """Freshness probe the chat pane polls to decide whether to show itself."""
    from browser_agent import config as bcfg, ghost
    return {"mode": bcfg.GHOST_MODE, **ghost.meta()}


@router.get("/agent/ghost/frame")
def ghost_frame() -> Response:
    """The newest frame of the agent's browser as PNG (204 until one exists)."""
    from browser_agent import ghost
    fr = ghost.latest()
    if fr is None:
        return Response(status_code=204)
    png, _meta = fr
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/agent/ghost/reveal")
def ghost_reveal() -> dict:
    """Bring the parked agent window on-screen (sign-in handoff). Windows-only;
    only a window parked at the ghost off-screen position can ever match."""
    from browser_agent import ghost
    return ghost.reveal_window()


@router.post("/agent/ghost/park")
def ghost_park() -> dict:
    """Send a previously revealed agent window back off-screen."""
    from browser_agent import ghost
    return ghost.park_window()


class ChatAnswerIn(BaseModel):
    text: str


@router.post("/chat/answer")
def chat_answer(body: ChatAnswerIn) -> dict:
    """Answer a pending ask_human / approval prompt from the agent."""
    if _agent_disabled():
        return {"ok": False, "error": "agent disabled (QUILL_AGENT=0)"}
    return agent.worker.handle_reply(body.text)


@router.post("/chat/new")
def chat_new() -> dict:
    """Archive the live chat (when it has turns) and start a fresh conversation.

    Clears the in-memory bubble log and both agents' LLM transcripts. Prior
    chats are written under data/chat_sessions/ and listed via GET /chat/sessions.
    """
    if _agent_disabled():
        return {"ok": False, "error": "agent disabled (QUILL_AGENT=0)"}
    archived = agent.worker.new()
    return {"ok": True, "archived": archived or None}


@router.get("/chat/sessions")
def chat_sessions(limit: int = Query(50, ge=1, le=200)) -> dict:
    """List archived chat conversations (newest first)."""
    from app.services import chat_sessions as _cs
    return {"ok": True, "sessions": _cs.list_sessions(limit=limit)}


@router.get("/chat/sessions/{session_id}")
def chat_session(session_id: str) -> dict:
    """Load one archived conversation (read-only replay for the Chat UI)."""
    from app.services import chat_sessions as _cs
    data = _cs.load_session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, "session": data}


class ChatOutcomeIn(BaseModel):
    distill_id: str
    outcome: str                          # accepted | rejected | edited
    edited_text: str | None = None


@router.post("/chat/outcome")
def chat_outcome(body: ChatOutcomeIn) -> dict:
    """Label an escalated chat answer's distill row (👍/👎/✏️ in the UI).

    Same plumbing as `scripts/distill_label.py` / fact review: accepted and
    edited rows feed few-shot; rejected rows are excluded. `distill_id` is the
    row id attached to the result event when ModelRouter escalated."""
    outcome = (body.outcome or "").strip().lower()
    if outcome not in ("accepted", "rejected", "edited"):
        raise HTTPException(status_code=400,
                            detail="outcome must be accepted|rejected|edited")
    if outcome == "edited" and not (body.edited_text or "").strip():
        raise HTTPException(status_code=400,
                            detail="edited needs edited_text (the training target)")
    rid = (body.distill_id or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="distill_id required")
    try:
        from app.services.escalate_log import escalate_log
        ok = escalate_log.set_user_outcome(
            outcome, row_id=rid,
            edited_text=(body.edited_text.strip() if body.edited_text else None))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404,
                            detail=f"no distill row matching id {rid[:12]}…")
    # Attention ledger: the verdict also labels the grounding impressions the
    # answer was composed from (used / rejected / edited). Best-effort.
    try:
        from app.services.attention_ledger import attention_ledger
        attention_ledger.close_grounding_for_row(rid, outcome,
                                                 store=memory._ensure_store())
    except Exception as exc:
        print(f"[chat_outcome] attention join skipped ({exc}).")
    # Workstream A: the labeled row is also a canonical learning pair.
    try:
        from app.services import learning_store
        from app.services.escalate_log import escalate_log as _elog
        row = _elog.row_by_id(rid)
        if row:
            learning_store.record_from_distill(
                row, outcome,
                edited_text=(body.edited_text.strip()
                             if body.edited_text else None))
    except Exception as exc:
        print(f"[learning_store] chat harvest skipped ({exc}).")
    return {"ok": True, "distill_id": rid, "outcome": outcome}


class OnboardingIn(BaseModel):
    profile: dict | None = None   # inline answers; omitted = read the sheet on disk


@router.get("/welcome", response_class=HTMLResponse)
def welcome_ui() -> HTMLResponse:
    """Launch page (same as `/`) — new setup vs continue on this machine."""
    from app.api.welcome_page import WELCOME_PAGE

    return HTMLResponse(WELCOME_PAGE)


@router.get("/welcome/status")
def welcome_status(request: Request) -> dict:
    """New vs returning + whether this browser still needs the LAN unlock."""
    from app.services import api_auth, onboarding

    lan_gate = not api_auth.bind_is_loopback()
    authorized = (
        api_auth.client_is_loopback(
            request.client.host if request.client else None)
        or api_auth.bind_is_loopback()
        or api_auth.request_authorized(request)
    )
    return onboarding.launch_status(authorized=authorized, lan_gate=lan_gate)


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_ui() -> HTMLResponse:
    """Guided web wizard — preferred over hand-editing the JSON profile sheet."""
    from app.api.onboarding_page import ONBOARDING_PAGE

    return HTMLResponse(ONBOARDING_PAGE)


@router.get("/onboarding/status")
def onboarding_status() -> dict:
    """Is the one-time new-user profile done? Where does the sheet live?"""
    from app.services import onboarding

    return onboarding.status()


@router.get("/onboarding/profile")
def onboarding_profile() -> dict:
    """Current profile sheet (for the wizard to pre-fill)."""
    from app.services import onboarding

    profile = onboarding.load_profile()
    if profile is None:
        onboarding.write_template()
        profile = onboarding.load_profile() or {}
    return {"ok": True, "profile": profile, **onboarding.status()}


@router.post("/onboarding/template")
def onboarding_template() -> dict:
    """(Re)create the blank profile sheet. Never overwrites a filled one."""
    from app.services import onboarding

    return onboarding.write_template()


@router.get("/onboarding/scan-available")
def onboarding_scan_available() -> dict:
    """Cheap probe so the wizard shows the auto-fill button only when scanning
    is enabled — no scan work is done here. `optional` lists sources the user
    can explicitly opt into (e.g. bookmarks)."""
    return {"available": settings.onboarding.scan_enabled,
            "sources": sorted(settings.onboarding.scan_sources),
            "optional": sorted(settings.onboarding.scan_optional)}


class OnboardingScanIn(BaseModel):
    include: list[str] = []   # opt-in extra sources (must be in scan_optional)


def _scan_sources(body: "OnboardingScanIn | None") -> set:
    """Configured auto sources, plus any opt-ins the SERVER permits. Never trust
    the client to widen reach to a source the operator disabled."""
    sources = set(settings.onboarding.scan_sources)
    if body and body.include:
        sources |= ({s.strip().lower() for s in body.include}
                    & set(settings.onboarding.scan_optional))
    return sources


@router.post("/onboarding/enrich")
def onboarding_enrich(body: OnboardingScanIn | None = None) -> dict:
    """Add CONTEXT from the machine to memory as OBSERVED knowledge — the
    projects the user works on, tools they actually use, git identity. This is
    enrichment, NOT the survey: it never fills the wizard form, never marks
    onboarding complete, and lands as observed/unreviewed (traceable via
    source 'onboarding.scan', reversible in the console) — distinct from the
    user-stated, human-accepted answers. `include` opts into bookmarks."""
    from app.services import onboarding_scan

    return onboarding_scan.enrich(sources=_scan_sources(body))


@router.post("/onboarding/scan")
def onboarding_scan(body: OnboardingScanIn | None = None) -> dict:
    """Read-only draft of the local signals (no ingest, no form changes) — kept
    for inspection/tests. The wizard uses /onboarding/enrich instead."""
    from app.services import onboarding_scan

    return onboarding_scan.scan(sources=_scan_sources(body))


# --- read-my-documents (opt-in content ingestion) ---------------------------
@router.get("/onboarding/documents-available")
def onboarding_documents_available() -> dict:
    """Cheap probe: is document ingestion enabled, and which folders are in
    scope? No files are read here — just the configured roots — so the wizard can
    show the consent checkbox with an honest 'we'll read these folders' line."""
    from app.services import documents

    if not settings.documents.enabled:
        return {"available": False, "roots": [], "exts": []}
    return {"available": True,
            "roots": [str(p) for p in documents.roots()],
            "exts": sorted(settings.documents.exts)}


@router.get("/onboarding/documents-preview")
def onboarding_documents_preview(limit: int = 50) -> dict:
    """Read-only list of the files an ingest WOULD read (name/type/size/chars),
    so the user sees exactly what's in scope before consenting. Reads file text
    to report char counts but writes nothing to memory."""
    from app.services import documents

    if not settings.documents.enabled:
        return {"available": False, "files": []}
    files = documents.preview(limit=max(1, min(limit, 200)))
    return {"available": True, "count": len(files), "files": files}


@router.post("/onboarding/documents")
def onboarding_documents() -> dict:
    """Read the user's documents (PDF / Word / notes) and mine each for structured
    facts via the normal extraction pipeline. EXPLICIT opt-in, content-level: the
    text is sent to the extraction model, every fact lands unreviewed (reviewable
    in the Console), and all events carry source='documents.scan' (reversible).
    Idempotent — re-running skips unchanged files. Roots are server-side only (no
    client-supplied paths), so this can't be pointed at arbitrary directories."""
    from app.services import documents

    return documents.ingest()


@router.post("/onboarding/ingest")
def onboarding_ingest(body: OnboardingIn | None = None) -> dict:
    """Feed the profile into Mnemos's knowledge (people/entities/facts/graph).

    Reads the sheet on disk unless inline answers are posted. Idempotent —
    only new/changed answers are added, so it's safe to edit and re-run."""
    from app.services import onboarding

    return onboarding.ingest((body.profile if body else None) or None)


# --- phone channel (direct phone -> Mnemos, no Phone Link) ------------------
class PhoneClaimIn(BaseModel):
    code: str
    name: str = ""
    platform: str = ""    # ios | android | other


class PhoneRevokeIn(BaseModel):
    device_id: str


class AuthUnlockIn(BaseModel):
    token: str


_AUTH_PAGE = """<!doctype html>
<html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>mnemos — unlock</title>
<style>
body{font:15px/1.45 system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;
color:#1a1a1a;background:#f6f5f2}
h1{font-size:1.35rem;font-weight:650;margin:0 0 .5rem}
p{color:#555;margin:0 0 1rem}
input,button{font:inherit;padding:.55rem .7rem;border-radius:6px;border:1px solid #ccc;width:100%;
box-sizing:border-box}
button{margin-top:.6rem;background:#1a1a1a;color:#fff;border-color:#1a1a1a;cursor:pointer}
#msg{margin-top:.8rem;min-height:1.2em}
</style></head><body>
<h1>Unlock LAN access</h1>
<p>This server is reachable on the network. Paste the API token from
<code>QUILL_API_TOKEN</code> or <code>data/.api_token</code>.</p>
<input id=tok type=password autocomplete=off placeholder="API token">
<button id=go>Unlock</button>
<div id=msg></div>
<script>
const msg=document.getElementById('msg');
document.getElementById('go').onclick=async()=>{
  msg.textContent='…';
  const r=await fetch('/auth/unlock',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:document.getElementById('tok').value})});
  const j=await r.json().catch(()=>({}));
  if(r.ok){msg.textContent='Unlocked. You can close this tab and use the UI.';
    location.href='/';}
  else msg.textContent=j.detail||('Failed ('+r.status+')');
};
</script></body></html>
"""


@router.get("/auth", response_class=HTMLResponse)
def auth_page() -> HTMLResponse:
    """LAN unlock form — sets an HttpOnly session cookie for browser UIs."""
    return HTMLResponse(_AUTH_PAGE)


@router.get("/auth/status")
def auth_status(request: Request, response: Response) -> dict:
    from app.services import api_auth

    csrf = (request.cookies.get(api_auth.CSRF_COOKIE) or "").strip()
    if not csrf:
        csrf = api_auth.apply_csrf_cookie(response)
    return {
        "bind_loopback": api_auth.bind_is_loopback(),
        "lan_gate": not api_auth.bind_is_loopback(),
        "token_configured": bool(api_auth.get_api_token()),
        "authorized": (
            api_auth.client_is_loopback(
                request.client.host if request.client else None)
            or api_auth.bind_is_loopback()
            or api_auth.request_authorized(request)
        ),
        "csrf_token": csrf,
        "csrf_header": api_auth.CSRF_HEADER,
    }


@router.post("/auth/unlock")
def auth_unlock(body: AuthUnlockIn, response: Response) -> dict:
    """Unlock LAN browser UI. Cookie gets an HMAC session token (plan 6.3),
    never the raw QUILL_API_TOKEN — cookie theft ≠ Bearer credential.
    Also mints the CSRF double-submit cookie (plan 6.4)."""
    from app.services import api_auth

    if not api_auth.token_matches(body.token):
        raise HTTPException(status_code=401, detail="invalid token")
    api_auth.apply_session_cookie(response, body.token.strip())
    csrf = api_auth.apply_csrf_cookie(response)
    return {"ok": True, "csrf_token": csrf}


@router.post("/auth/logout")
def auth_logout(response: Response) -> dict:
    from app.services import api_auth

    api_auth.clear_session_cookie(response)
    return {"ok": True}


@router.get("/phone", response_class=HTMLResponse)
def phone_ui() -> HTMLResponse:
    """Desktop pairing page: QR + code, paired-device list, revoke."""
    from app.api.phone_page import PHONE_PAGE

    return _html_with_approval(PHONE_PAGE, next_url="/phone")


@router.get("/phone/setup", response_class=HTMLResponse)
def phone_setup_ui() -> HTMLResponse:
    """Mobile page the QR opens: claim the code, get shortcut instructions."""
    from app.api.phone_page import PHONE_SETUP_PAGE

    return HTMLResponse(PHONE_SETUP_PAGE)


@router.get("/phone/status")
def phone_status() -> dict:
    """Devices + reachability + whether a pairing offer is live."""
    from app.services import phone_channel

    return phone_channel.status()


@router.post("/phone/pair/start")
def phone_pair_start() -> dict:
    """Begin (or restart) pairing; returns the code, setup URL, and QR."""
    from app.services import phone_channel

    res = phone_channel.start_pairing()
    if res.get("ok"):
        res["qr_svg"] = phone_channel.qr_svg(res["setup_url"])
    return res


@router.post("/phone/pair/claim")
def phone_pair_claim(body: PhoneClaimIn) -> dict:
    """The phone trades a valid pairing code for its one-time device token."""
    from app.services import phone_channel

    return phone_channel.claim_pairing(body.code, body.name, body.platform)


@router.post("/phone/ingest")
def phone_ingest(body: dict, authorization: str | None = Header(None)) -> dict:
    """Authenticated phone payload -> Event on the bus (source=phone.<kind>).

    Auth is the per-device bearer token minted at pairing. Content lands as
    memory context with the confidence contract attached — it can never act
    on its own; offers/actions ride the normal readiness + approval gates."""
    from app.services import phone_channel

    device = phone_channel.authenticate(authorization)
    if device is None:
        raise HTTPException(status_code=401, detail="invalid or missing device token")
    res = phone_channel.ingest(device, body)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "bad payload"))
    return res


@router.post("/phone/photo")
async def phone_photo(request: Request, caption: str = "",
                      taken_at: str | None = None,
                      lat: float | None = None, lon: float | None = None,
                      authorization: str | None = Header(None)) -> dict:
    """Authenticated photo upload -> the vision pipeline (source=phone.photo).

    The raw image is the request body (Shortcuts: Request Body = File). Optional
    query params: ?caption= a note; ?taken_at= the capture time (Unix or ISO) so
    a shared OLD photo lands at its real date; ?lat=&lon= where it was taken. The
    photo is described/OCR'd by the same VLM the webcam uses and lands as a VISION
    memory event. Opt-in per photo; iOS gates photo access with its own prompt."""
    from app.services import phone_channel

    device = phone_channel.authenticate(authorization)
    if device is None:
        raise HTTPException(status_code=401, detail="invalid or missing device token")
    data = await request.body()
    res = phone_channel.ingest_photo(
        device, data, caption=caption, taken_at=taken_at, lat=lat, lon=lon,
        content_type=request.headers.get("content-type", ""))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "photo failed"))
    return res


@router.post("/phone/sync")
def phone_sync(body: dict | None = None,
               authorization: str | None = Header(None)) -> dict:
    """Unified phone exchange: optionally ingest {kind,text,meta}, always drain
    the outbox. One authenticated call powers the single "mnemos" shortcut for
    both directions."""
    from app.services import phone_channel

    device = phone_channel.authenticate(authorization)
    if device is None:
        raise HTTPException(status_code=401, detail="invalid or missing device token")
    return phone_channel.sync_exchange(device, body or {})


@router.post("/phone/revoke")
def phone_revoke(body: PhoneRevokeIn) -> dict:
    """Forget a paired device — its token stops working immediately."""
    from app.services import phone_channel

    if not phone_channel.revoke(body.device_id):
        raise HTTPException(status_code=404, detail="unknown device")
    return {"ok": True}


class OutboxQueueIn(BaseModel):
    text: str
    kind: str = "notify"          # notify | reminder | url | other
    device_id: str | None = None  # None = whichever paired phone drains first


@router.post("/phone/outbox/queue")
def phone_outbox_queue(body: OutboxQueueIn) -> dict:
    """Desktop-side enqueue (Mnemos -> phone). The phone can never call this —
    device tokens only READ the outbox; the desktop is the decider."""
    from app.services import phone_channel

    res = phone_channel.queue_outbox(body.kind, body.text,
                                     device_id=body.device_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "bad item"))
    return res


@router.get("/phone/outbox")
def phone_outbox_drain(peek: bool = False,
                       authorization: str | None = Header(None)) -> dict:
    """The phone's pull: authenticated drain of its pending items.

    A native Shortcuts recipe calls this (same bearer token as ingest) and
    executes each item with built-in actions. `?peek=1` previews without
    marking delivered — for testing the shortcut safely."""
    from app.services import phone_channel

    device = phone_channel.authenticate(authorization)
    if device is None:
        raise HTTPException(status_code=401, detail="invalid or missing device token")
    return phone_channel.drain_outbox(device, peek=peek)


# --- peer channel (Mnemos <-> Mnemos, teams) --------------------------------
class PeerClaimIn(BaseModel):
    code: str
    name: str = ""
    base_url: str = ""          # where WE can call the claimer back
    token_for_caller: str = ""  # what WE present when calling them


class PeerJoinIn(BaseModel):
    url: str    # the other instance, e.g. http://192.168.1.20:8000
    code: str   # the 6-digit code their desktop is showing


class PeerQueryIn(BaseModel):
    peer_id: str
    question: str
    kind: str = "question"   # "handoff" = ask them to DO it (always human-gated)


class PeerDecideIn(BaseModel):
    id: str     # local pending-ask id from GET /peer/asks


class PeerRevokeIn(BaseModel):
    peer_id: str


class PeerLinkIn(BaseModel):
    peer_id: str
    person_id: int | None = None
    create_name: str | None = None  # optional: create Person then link


class PeerUnlinkIn(BaseModel):
    peer_id: str


class PeerPolicyIn(BaseModel):
    peer_id: str
    policy: dict   # {class: "auto"|"offer"|"deny"}; `personal` can never be auto
    pack: str | None = None  # optional pack name stored alongside the map


class PeerPackIn(BaseModel):
    peer_id: str
    pack: str   # teammate | manager | company | vendor


class PeerTeamIn(BaseModel):
    name: str
    slug: str | None = None
    peer_ids: list[str] | None = None


class PeerTeamMembersIn(BaseModel):
    slug: str
    peer_ids: list[str]


class PeerTeamDeleteIn(BaseModel):
    slug: str


@router.get("/peer", response_class=HTMLResponse)
def peer_ui() -> HTMLResponse:
    """Team page: pair instances, disclosure policy, approval queue."""
    from app.api.peer_page import PEER_PAGE

    return HTMLResponse(PEER_PAGE)


@router.get("/peer/status")
def peer_status() -> dict:
    """Peers + pairing state + queues, for the desktop UI."""
    from app.services import peer_channel

    return peer_channel.status()


@router.post("/peer/policy")
def peer_policy(body: PeerPolicyIn) -> dict:
    """Set one peer's disclosure policy. Only the user can widen sharing, and
    `personal` -> auto is refused outright."""
    from app.services import peer_channel

    res = peer_channel.set_policy(body.peer_id, body.policy, pack=body.pack)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "invalid policy"))
    return res


@router.post("/peer/pair/start")
def peer_pair_start() -> dict:
    """Desktop-side: show a single-use code a teammate's instance can claim."""
    from app.services import peer_channel

    return peer_channel.start_pairing()


@router.post("/peer/pair/claim")
def peer_pair_claim(body: PeerClaimIn) -> dict:
    """A joining instance trades the code for our token and hands us theirs
    (mutual pairing in one round trip). Auth-exempt: the code IS the auth."""
    from app.services import peer_channel

    return peer_channel.claim_pairing(body.code, body.name, body.base_url,
                                      body.token_for_caller)


@router.post("/peer/pair/join")
def peer_pair_join(body: PeerJoinIn) -> dict:
    """Desktop-side: claim a code shown on a teammate's desktop at `url`."""
    from app.services import peer_channel

    return peer_channel.join(body.url, body.code)


@router.post("/peer/ask")
def peer_ask_inbound(body: dict, authorization: str | None = Header(None)) -> dict:
    """Authenticated inbound ask from a paired peer. Default posture queues it
    for this user's disclosure verdict; QUILL_PEER_AUTO_ANSWER=1 composes a
    grounded, redacted answer synchronously (dev/sim)."""
    from app.services import peer_channel

    peer = peer_channel.authenticate(authorization)
    if peer is None:
        raise HTTPException(status_code=401, detail="invalid or missing peer token")
    return peer_channel.handle_ask(peer, body)


@router.post("/peer/ping")
def peer_ping_inbound(authorization: str | None = Header(None)) -> dict:
    """Authenticated liveness ping from a paired peer. Updates last_seen and
    flushes any asks queued while we were offline."""
    from app.services import peer_channel, team_layer

    peer = peer_channel.authenticate(authorization)
    if peer is None:
        raise HTTPException(status_code=401, detail="invalid or missing peer token")
    return team_layer.handle_ping(peer)


@router.post("/peer/answer")
def peer_answer_inbound(body: dict, authorization: str | None = Header(None)) -> dict:
    """Authenticated delivery of an answer to an ask WE sent. Refused unless it
    matches an outstanding ask to that peer."""
    from app.services import peer_channel

    peer = peer_channel.authenticate(authorization)
    if peer is None:
        raise HTTPException(status_code=401, detail="invalid or missing peer token")
    return peer_channel.handle_answer(peer, body)


@router.get("/peer/asks")
def peer_asks() -> dict:
    """Inbound asks awaiting this user's disclosure decision."""
    from app.services import peer_channel

    return {"ok": True, "asks": peer_channel.pending_asks()}


@router.post("/peer/asks/approve")
def peer_ask_approve(body: PeerDecideIn) -> dict:
    """Disclosure verdict YES: compose (grounded + redacted) and deliver."""
    from app.services import peer_channel

    return peer_channel.decide_ask(body.id, approve=True)


@router.post("/peer/asks/deny")
def peer_ask_deny(body: PeerDecideIn) -> dict:
    """Disclosure verdict NO: tell the asking peer it was declined."""
    from app.services import peer_channel

    return peer_channel.decide_ask(body.id, approve=False)


@router.post("/peer/query")
def peer_query(body: PeerQueryIn) -> dict:
    """Desktop-side: ask a paired peer's Mnemos a question (or hand off a
    task). Synchronous when their side auto-answers a question; handoffs
    always wait for their human."""
    from app.services import peer_channel

    return peer_channel.ask(body.peer_id, body.question, body.kind)


@router.get("/peer/answers")
def peer_answers(ask_id: str | None = None) -> dict:
    """Status/answers for asks we sent (optionally one ask_id)."""
    from app.services import peer_channel

    return {"ok": True, "answers": peer_channel.answers(ask_id)}


@router.post("/peer/revoke")
def peer_revoke(body: PeerRevokeIn) -> dict:
    """Forget a peer — tokens die in both directions."""
    from app.services import peer_channel

    if not peer_channel.revoke(body.peer_id):
        raise HTTPException(status_code=404, detail="unknown peer")
    return {"ok": True}


@router.post("/peer/link")
def peer_link(body: PeerLinkIn) -> dict:
    """User-asserted peer ↔ Person link (never auto on pair)."""
    from app.services import peer_channel

    if body.create_name is not None and str(body.create_name).strip():
        res = peer_channel.create_and_link_person(
            body.peer_id, str(body.create_name).strip())
    elif body.person_id is not None:
        res = peer_channel.link_person(body.peer_id, int(body.person_id))
    else:
        raise HTTPException(status_code=400,
                            detail="person_id or create_name required")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "link failed")
    return res


@router.post("/peer/unlink")
def peer_unlink(body: PeerUnlinkIn) -> dict:
    """Clear peer ↔ Person link; pairing unchanged."""
    from app.services import peer_channel

    res = peer_channel.unlink_person(body.peer_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error") or "unknown peer")
    return res


@router.post("/peer/policy/pack")
def peer_policy_pack(body: PeerPackIn) -> dict:
    """Apply a relationship policy pack (teammate / manager / company / vendor)."""
    from app.services import team_layer

    res = team_layer.apply_pack(body.peer_id, body.pack)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "pack failed")
    return res


@router.post("/peer/teams")
def peer_team_upsert(body: PeerTeamIn) -> dict:
    from app.services import team_layer

    res = team_layer.upsert_team(body.name, body.peer_ids, slug=body.slug)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "team failed")
    return res


@router.post("/peer/teams/members")
def peer_team_members(body: PeerTeamMembersIn) -> dict:
    from app.services import team_layer

    res = team_layer.set_team_members(body.slug, body.peer_ids)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "team failed")
    return res


@router.post("/peer/teams/delete")
def peer_team_delete(body: PeerTeamDeleteIn) -> dict:
    from app.services import team_layer

    res = team_layer.delete_team(body.slug)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error") or "unknown team")
    return res


# --- Org AI Network (hybrid coordinator + local digests/priorities) ----------
class OrgRegisterIn(BaseModel):
    node_id: str = ""
    display_name: str = ""
    role: str = "ic"
    reports_to: str = ""
    manager_peer_id: str = ""
    coordinator_url: str = ""
    base_url: str = ""


class OrgGoalIn(BaseModel):
    title: str
    detail: str = ""
    horizon: str = ""
    priority: float = 0.8
    owner_role: str = "ceo"


@router.get("/org-network", response_class=HTMLResponse)
def org_network_ui() -> HTMLResponse:
    from app.api.org_network_page import ORG_NETWORK_PAGE
    return HTMLResponse(ORG_NETWORK_PAGE)


@router.get("/org-network/status")
def org_network_status() -> dict:
    from app.services import org_client, org_priority
    st = org_client.status()
    st["priorities"] = org_priority.latest(3)
    return st


@router.post("/org-network/register")
def org_network_register(body: OrgRegisterIn) -> dict:
    """Register this Mnemos with the Org Coordinator. Persists node token locally."""
    from app.services import org_client

    res = org_client.register(
        base_url=body.base_url,
        peer_id=body.manager_peer_id,
        display_name=body.display_name,
        role=body.role or "ic",
        reports_to=body.reports_to,
        node_id_override=body.node_id or None,
        coordinator_url=body.coordinator_url or None,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400,
                            detail=res.get("error") or res.get("detail")
                            or "register failed")
    return res


@router.post("/org-network/digest")
def org_network_digest() -> dict:
    from app.services import org_client, org_digest
    if not org_client.enabled():
        raise HTTPException(status_code=400,
                            detail="QUILL_ORG_NETWORK is off")
    if not org_client.node_token():
        raise HTTPException(status_code=400,
                            detail="Register this node first (POST /org-network/register)")
    if not org_client.coordinator_reachable():
        raise HTTPException(
            status_code=503,
            detail="Org Coordinator not reachable — start with run_all.py "
                   "or python -m org_coordinator.main")
    return org_digest.ship_digest()


@router.post("/org-network/priorities")
def org_network_priorities() -> dict:
    from app.services import org_client, org_priority
    if not org_client.enabled():
        raise HTTPException(status_code=400,
                            detail="QUILL_ORG_NETWORK is off")
    if not org_client.node_token():
        raise HTTPException(status_code=400,
                            detail="Register this node first")
    return org_priority.pull_from_coordinator()


@router.post("/org-network/goals")
def org_network_goals(body: OrgGoalIn) -> dict:
    from app.services import org_client
    res = org_client.create_goal(
        body.title, detail=body.detail, horizon=body.horizon,
        priority=body.priority, owner_role=body.owner_role)
    if not res.get("ok"):
        raise HTTPException(status_code=400,
                            detail=res.get("error") or res.get("detail")
                            or "goal failed")
    return res


@router.post("/org-network/cascade")
def org_network_cascade() -> dict:
    from app.services import org_client, org_priority
    res = org_client.cascade()
    # Also pull our own packet
    local = org_priority.pull_from_coordinator()
    return {"ok": True, "cascade": res, "local": local}


@router.post("/org-network/escalate")
def org_network_escalate(body: dict) -> dict:
    from app.services import org_escalate
    return org_escalate.record_and_notify(body or {})


# --- iCloud account connection (guided; public-product onboarding) ----------
class IcloudConnectIn(BaseModel):
    user: str            # Apple ID email
    app_password: str    # APP-SPECIFIC password, never the real one


@router.get("/icloud/status")
def icloud_status() -> dict:
    """Connected or not + masked account. Secrets never leave the server."""
    from app.services import icloud_account

    return icloud_account.status()


@router.post("/icloud/connect")
def icloud_connect(body: IcloudConnectIn) -> dict:
    """Validate the pair live against Apple's CalDAV endpoint, then persist it
    to the credentials file. Rejected credentials are never stored."""
    from app.services import icloud_account

    res = icloud_account.connect(body.user, body.app_password)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "connect failed"))
    return res


@router.post("/icloud/disconnect")
def icloud_disconnect() -> dict:
    """Forget the stored credentials (also revocable Apple-side anytime)."""
    from app.services import icloud_account

    return icloud_account.disconnect()


@router.post("/icloud/sync")
def icloud_sync() -> dict:
    """Run one calendar sync pass now (also runs periodically on its own)."""
    from app.services import icloud_calendar

    return icloud_calendar.sync()


@router.get("/icloud/sync/status")
def icloud_sync_status() -> dict:
    """Sync health: connected, last run, last result, cadence."""
    from app.services import icloud_calendar

    return icloud_calendar.sync_status()


class CalendarEventIn(BaseModel):
    summary: str
    start: str                       # ISO, e.g. 2026-07-20T15:00:00 (local)
    end: str | None = None
    duration_min: int = 60
    calendar: str = "Home"
    location: str = ""
    all_day: bool = False


@router.post("/icloud/calendar/event")
def icloud_create_event(body: CalendarEventIn) -> dict:
    """Create a personal event on the iCloud calendar (no attendees).

    Human-initiated: issuing this call IS the approval. Mnemos never adds guests,
    so a write can't email or invite anyone."""
    from app.services import icloud_calendar

    res = icloud_calendar.create_event(
        body.summary, body.start, end=body.end, duration_min=body.duration_min,
        calendar=body.calendar, location=body.location, all_day=body.all_day)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "create failed"))
    return res


class CredentialsIn(BaseModel):
    site: str          # host or URL, e.g. gmail.com
    username: str
    password: str


@router.get("/credentials")
def credentials_list() -> dict:
    """List sites with saved logins (values not returned)."""
    from browser_agent.credentials import list_sites

    return {"sites": list_sites()}


@router.post("/credentials")
def credentials_save(body: CredentialsIn) -> dict:
    """Save site login to `.credentials.env` for automatic fill on future runs."""
    from browser_agent.credentials import list_sites, save

    try:
        meta = save(body.site, body.username, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "site": meta["site"], "sites": list_sites()}


class SpeakIn(BaseModel):
    text: str


class SpeakMuteIn(BaseModel):
    muted: bool


@router.post("/speak")
def speak(body: SpeakIn) -> dict:
    return voice.speak(body.text)


@router.get("/speak/status")
def speak_status() -> dict:
    """Whether TTS is enabled and whether the user muted the AI voice."""
    return voice.status()


@router.post("/speak/mute")
def speak_mute(body: SpeakMuteIn) -> dict:
    """Mute or unmute spoken replies. Persists; no restart needed."""
    return voice.set_muted(body.muted)


@router.get("/speak/voices")
def speak_voices() -> dict:
    """Available TTS voice names — set one via QUILL_TTS_VOICE (substring match)."""
    return {"voices": voice.voices()}


@router.get("/speakers")
def speakers_list() -> dict:
    from app.services.speakers import speakers as spk

    return {"enrolled": spk.enrolled_names()}


@router.get("/speakers/profiles")
def speakers_profiles() -> dict:
    """Per-environment-profile speaker-ID calibration (#4): counts, decision mix,
    and the learned cluster-threshold offset — so 'adaptive thresholds' is
    inspectable, not a black box."""
    from app.services.speakers import speakers as spk

    return {"profiles": spk.profile_report()}


class EnrollIn(BaseModel):
    name: str
    wav_path: str  # path to a 16 kHz mono WAV sample of this person


@router.post("/speakers/enroll")
def speakers_enroll(body: EnrollIn) -> dict:
    import wave

    import numpy as np

    from app.config import settings
    from app.services.speakers import speakers as spk

    with wave.open(body.wav_path, "rb") as wf:
        if wf.getframerate() != settings.audio.sample_rate:
            return {"ok": False, "error": f"WAV must be {settings.audio.sample_rate} Hz"}
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    spk.enroll(body.name, pcm.astype(np.float32) / 32768.0, settings.audio.sample_rate)
    return {"ok": True, "enrolled": spk.enrolled_names()}


_CHAT_PAGE = _mnemos(r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>@@BRAND@@ — Chat</title><meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{
  margin:0;font:15px/1.55 var(--font);color:var(--text);
  height:100vh;display:flex;flex-direction:column;
  background:
    radial-gradient(900px 480px at 8% -5%, rgba(184,115,51,.05), transparent 55%),
    radial-gradient(700px 400px at 96% 0%, rgba(30,91,79,.04), transparent 50%),
    linear-gradient(180deg, #FBF9F4 0%, var(--paper) 40%, var(--workspace) 100%);
}
#ambientChat{
  position:fixed;left:12px;top:72px;width:min(200px,18vw);z-index:15;
  pointer-events:none;
}
/* Approval banner owns this strip — ambient must not paint over it. */
body.has-approval #ambientChat{display:none}
body.has-approval #mnemosApproval{position:relative;z-index:25}
@media(max-width:900px){#ambientChat{display:none}}
.top{
  display:flex;align-items:center;gap:18px;flex-wrap:wrap;
  padding:14px 22px;
}
.page-sub{margin-left:-4px}
.meta{display:flex;gap:14px;align-items:center;font-family:var(--mono);font-size:12px;color:var(--mut)}
.chat-tools{display:flex;gap:8px;align-items:center;position:relative}
.chat-tools button{
  background:transparent;border:1px solid transparent;border-radius:10px;
  padding:6px 10px;font:500 12px var(--font);color:var(--mut);cursor:pointer;
  box-shadow:none;
}
.chat-tools button:hover{
  color:var(--text);border-color:rgba(11,19,32,.1);background:rgba(11,19,32,.03);
  transform:none;box-shadow:none;
}
.chat-tools button:active{transform:none}
#pastPanel{
  display:none;position:absolute;right:0;top:calc(100% + 8px);z-index:50;
  width:min(340px,82vw);max-height:360px;overflow:auto;
  background:var(--surface);border:1px solid rgba(11,19,32,.1);border-radius:14px;
  box-shadow:var(--shadow-float);padding:8px;animation:fadeUp .22s var(--ease) both;
}
#pastPanel.open{display:block}
#pastPanel .past-head{
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:6px 8px 8px;font:11px/1.2 var(--mono);color:var(--mut);
  text-transform:uppercase;letter-spacing:.06em;
}
#pastPanel .past-empty{padding:14px 10px;color:var(--mut);font:13px var(--font)}
.past-item{
  display:block;width:100%;text-align:left;border:none;background:transparent;
  border-radius:10px;padding:10px 10px;cursor:pointer;color:var(--text);
  font:13px/1.35 var(--font);box-shadow:none;
}
.past-item:hover{background:rgba(184,115,51,.08);transform:none;box-shadow:none}
.past-item .past-title{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.past-item .past-meta{display:block;margin-top:3px;font:11px var(--mono);color:var(--mut)}
#archiveBanner{
  display:none;width:min(640px,94%);margin:8px auto 0;padding:10px 14px;
  background:rgba(30,91,79,.06);border:1px solid rgba(30,91,79,.18);
  border-radius:12px;font:13px var(--font);color:var(--navy);
  align-items:center;justify-content:space-between;gap:12px;
}
#archiveBanner.show{display:flex}
#archiveBanner button{
  background:transparent;border:1px solid rgba(11,19,32,.12);border-radius:8px;
  padding:5px 10px;font:500 12px var(--font);color:var(--navy);cursor:pointer;
  box-shadow:none;flex:0 0 auto;
}
#archiveBanner button:hover{background:rgba(11,19,32,.04);transform:none;box-shadow:none}
#log{
  flex:1;overflow:auto;padding:32px 20px 40px;
  display:flex;flex-direction:column;gap:4px;align-items:center;
}
.msg{
  max-width:min(640px,94%);width:100%;
  animation:fadeUp .32s var(--ease) both;
  position:relative;
}
.msg-label{
  font:500 10px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;
  color:var(--mut);margin:0 0 6px;padding:0 2px;
}
.msg-body{
  white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;
  font:15px/1.65 var(--font);color:var(--text);letter-spacing:-.01em;
}
/* You — quiet ink note, not a navy brick */
.msg.user{
  align-self:stretch;max-width:min(640px,94%);
  margin:14px 0 6px;display:flex;flex-direction:column;align-items:flex-end;
}
.msg.user .msg-label{align-self:flex-end;color:rgba(11,19,32,.45)}
.msg.user .msg-body{
  max-width:min(420px,88%);
  background:transparent;color:var(--navy);font-weight:500;
  padding:0 0 10px;border-bottom:1.5px solid rgba(184,115,51,.35);
  text-align:right;box-shadow:none;border-radius:0;
}
/* Mnemos — paper page of a reply */
.msg.result{
  margin:18px 0 8px;padding:0;
}
.msg.result .msg-shell{
  background:var(--surface);border:1px solid rgba(11,19,32,.07);
  border-radius:16px;padding:16px 18px 14px;
  box-shadow:var(--shadow-surface);
  position:relative;
}
.msg.result .msg-shell::before{
  content:"";position:absolute;left:0;top:14px;bottom:14px;width:2px;
  background:linear-gradient(180deg,rgba(184,115,51,.55),rgba(184,115,51,.08));
  border-radius:2px;
}
.msg.result .msg-label{color:var(--navy);opacity:.55}
.msg.result .msg-body{padding-left:8px}
.msg.result .msg-body.rd-host{padding-left:0;white-space:normal}
.msg.result .msg-shell{padding:18px 20px 16px}
.sources{
  margin:10px 0 0 8px;padding-top:8px;border-top:1px solid rgba(11,19,32,.06);
  font-size:12px;color:var(--mut);line-height:1.55;
}
.sources summary{
  cursor:pointer;user-select:none;list-style:none;
  font:500 11px/1.2 var(--mono);letter-spacing:.04em;text-transform:uppercase;
}
.sources summary::-webkit-details-marker{display:none}
.sources summary:hover{color:var(--navy)}
.sources div{margin:4px 0 0 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.verdict{
  display:flex;gap:6px;margin:12px 0 0 8px;padding-top:10px;
  border-top:1px solid rgba(11,19,32,.06);
}
.verdict button{
  background:transparent;border:1px solid transparent;border-radius:8px;
  padding:4px 10px;font-size:13px;line-height:1.2;cursor:pointer;color:var(--mut);
  font-family:var(--font);
}
.verdict button:hover:not(:disabled){
  border-color:rgba(11,19,32,.1);color:var(--text);background:rgba(11,19,32,.03);
}
.verdict button.on{border-color:rgba(46,111,87,.35);color:var(--ok);background:rgba(46,111,87,.06)}
.verdict button.bad.on{border-color:rgba(166,71,71,.35);color:var(--danger);background:rgba(166,71,71,.06)}
.verdict button:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
.msg.system{
  align-self:center;max-width:min(480px,90%);margin:10px 0;
  text-align:center;background:transparent;padding:0;box-shadow:none;
}
.msg.system .msg-body{
  font:italic 13px/1.5 var(--font);color:var(--mut);text-align:center;
}
.msg.ask{
  margin:14px 0 8px;
}
.msg.ask:not(.folio-wrap) .msg-shell{
  background:rgba(255,254,251,.9);border:1px solid rgba(199,138,44,.22);
  border-radius:16px;padding:14px 16px;
  box-shadow:var(--shadow-workspace);
}
.msg.ask .msg-label{color:var(--warn)}
.msg.ask.folio-wrap{background:transparent;border:none;box-shadow:none;padding:0;margin:18px 0}
.msg.error{margin:12px 0}
.msg.error .msg-shell{
  background:rgba(166,71,71,.05);border:1px solid rgba(166,71,71,.18);
  border-radius:14px;padding:12px 14px;
}
.msg.error .msg-label{color:var(--danger)}
.msg.error .msg-body{color:#6b3030;font-size:14px}
.msg.progress{
  margin:2px 0;max-width:min(640px,94%);
}
.msg.progress .msg-body{
  font:12px/1.5 var(--mono);color:rgba(107,111,118,.85);
  padding:3px 0 3px 14px;border-left:1.5px solid rgba(11,19,32,.1);
  background:transparent;box-shadow:none;
}
.dock{
  border-top:1px solid rgba(11,19,32,.06);background:rgba(248,246,241,.96);backdrop-filter:blur(14px);
  padding:0 18px 18px;display:flex;flex-direction:column;align-items:center;
}
#bar{
  display:none;gap:10px;align-items:center;justify-content:flex-start;flex-wrap:wrap;
  width:min(640px,100%);margin:12px 0 4px;padding:10px 14px;
  background:var(--surface);border:1px solid rgba(11,19,32,.08);border-radius:14px;
  box-shadow:var(--shadow-workspace);animation:fadeUp .3s var(--ease) both;
}
#bar .action-detail{flex:1 1 100%;order:5;margin:4px 0 0}
#bar .approval-form{order:6}
#waiting{
  flex:1;min-width:0;font-size:12.5px;color:var(--warn);line-height:1.35;
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
}
#bar button{
  flex:0 0 auto;border-radius:10px;padding:8px 18px;cursor:pointer;font:500 14px var(--font);
  border:1px solid var(--line);background:var(--bg-elev);color:var(--text);
}
#bar .yes{border-color:rgba(46,111,87,.4);color:var(--ok)}
#bar .yes:hover{
  background:rgba(46,111,87,.1);border-color:rgba(46,111,87,.55);
  box-shadow:0 4px 14px rgba(46,111,87,.12);
}
#bar .no{border-color:rgba(166,71,71,.4);color:var(--danger)}
#bar .no:hover{
  background:rgba(166,71,71,.1);border-color:rgba(166,71,71,.55);
  box-shadow:0 4px 14px rgba(166,71,71,.12);
}
.composer-wrap{
  display:flex;flex-direction:column;gap:8px;width:min(640px,100%);padding-top:12px;
}
.composer{
  display:flex;gap:10px;align-items:stretch;width:100%;
}
#box{
  flex:1;background:var(--panel);color:var(--text);border:1px solid var(--line);
  border-radius:var(--radius);padding:12px 14px;resize:none;min-height:52px;height:52px;
  font:inherit;line-height:1.45;box-shadow:var(--shadow);
  transition:border-color .28s var(--ease),box-shadow .28s var(--ease);
}
#box:focus{outline:none;border-color:rgba(184,115,51,.45);box-shadow:0 0 0 3px var(--acc-dim)}
#dry,#studyMode{
  background:var(--panel);color:var(--mut);border:1px solid var(--line);
  border-radius:var(--radius);padding:0 10px;min-width:132px;font:13px var(--font);
  cursor:pointer;
  transition:border-color .28s var(--ease),color .28s var(--ease),
    background .28s var(--ease),box-shadow .28s var(--ease),transform .22s var(--ease);
}
#studyMode{min-width:148px}
#dry:hover,#studyMode:hover{
  color:var(--text);border-color:rgba(184,115,51,.4);
  background:var(--bg-elev);transform:translateY(-1px);
  box-shadow:0 4px 12px rgba(11,19,32,.06);
}
#dry:focus,#studyMode:focus{color:var(--text);outline:none;border-color:rgba(184,115,51,.4)}
#ctxBtn{
  background:var(--panel);color:var(--mut);border:1px solid var(--line);
  border-radius:var(--radius);padding:0 12px;min-width:auto;cursor:pointer;
  font:500 13px var(--font);white-space:nowrap;
}
#ctxBtn:hover{
  color:var(--text);border-color:rgba(184,115,51,.4);
  background:var(--bg-elev);
}
#ctxBtn.on{color:var(--navy);border-color:rgba(184,115,51,.45);background:rgba(184,115,51,.08)}
#ctxBtn.has{color:var(--ok);border-color:rgba(46,111,87,.4)}
#ctxPanel{
  display:none;width:100%;background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:10px 12px;box-shadow:var(--shadow);
  animation:fadeUp .25s var(--ease) both;
}
#ctxPanel.open{display:block}
#ctxPanel .ctx-label{
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  font:12px var(--mono);color:var(--mut);margin-bottom:6px;
}
#ctxPanel .ctx-label button{
  background:transparent;border:none;color:var(--mut);cursor:pointer;
  font:12px var(--font);padding:0 4px;box-shadow:none;
}
#ctxPanel .ctx-label button:hover{
  color:var(--danger);transform:none;box-shadow:none;background:transparent;
}
#ctxBox{
  width:100%;box-sizing:border-box;background:var(--bg-elev);color:var(--text);
  border:1px solid var(--line);border-radius:10px;padding:10px 12px;resize:vertical;
  min-height:72px;max-height:200px;font:inherit;line-height:1.45;
  transition:border-color .28s var(--ease),box-shadow .28s var(--ease);
}
#ctxBox:focus{outline:none;border-color:rgba(184,115,51,.45);box-shadow:0 0 0 3px var(--acc-dim)}
#ctxFiles{
  display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;min-height:0;
}
#ctxFiles:empty{display:none}
.ctx-file{
  display:inline-flex;align-items:center;gap:6px;max-width:100%;
  background:var(--bg-elev);border:1px solid var(--line);border-radius:10px;
  padding:5px 8px 5px 10px;font:12px var(--font);color:var(--text);
}
.ctx-file .ctx-file-name{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px;
}
.ctx-file .ctx-file-meta{color:var(--mut);font:11px var(--mono);white-space:nowrap}
.ctx-file.pending{opacity:.7}
.ctx-file.err{border-color:rgba(160,50,50,.45);color:var(--danger)}
.ctx-file.ok{border-color:rgba(46,111,87,.35)}
.ctx-file button{
  background:transparent;border:none;color:var(--mut);cursor:pointer;
  font:12px var(--font);padding:0 2px;box-shadow:none;line-height:1;
}
.ctx-file button:hover{
  color:var(--danger);transform:none;box-shadow:none;background:transparent;
}
#ctxAttach{
  background:transparent;border:1px dashed var(--line);border-radius:10px;
  color:var(--mut);cursor:pointer;font:12px var(--font);padding:6px 10px;
  margin-top:8px;width:100%;text-align:left;
  transition:border-color .22s var(--ease),color .22s var(--ease),background .22s var(--ease);
}
#ctxAttach:hover{
  color:var(--text);border-color:rgba(184,115,51,.45);background:rgba(184,115,51,.05);
  transform:none;box-shadow:none;
}
#ctxAttach:disabled{opacity:.55;cursor:wait}
#ctxLearn{
  margin-top:6px;font:11px var(--mono);color:var(--mut);line-height:1.35;
}
#send{
  background:var(--navy);color:#F8F6F1;border:none;border-radius:var(--radius);
  padding:0 22px;cursor:pointer;font-weight:600;font-size:14px;
  box-shadow:0 2px 8px rgba(11,19,32,.16);
}
#send:hover{
  background:#152033;transform:translateY(-2px);
  box-shadow:0 8px 20px rgba(11,19,32,.22);
  filter:brightness(1.06);
}
#send:active{transform:translateY(0) scale(.97);box-shadow:0 2px 6px rgba(11,19,32,.14)}
#ghost{
  position:fixed;right:18px;bottom:120px;width:380px;z-index:40;display:none;
  background:var(--float);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-float);overflow:hidden;animation:fadeUp .3s var(--ease) both;
  transition:box-shadow .32s var(--ease);
}
#ghost:hover{box-shadow:0 8px 32px rgba(11,19,32,.14)}
#ghost.ink-border{box-shadow:var(--shadow-float),inset 0 0 0 1px rgba(184,115,51,.2)}
#ghost .head{
  display:flex;align-items:center;gap:8px;padding:7px 10px;
  border-bottom:1px solid var(--line);font:12px var(--mono);color:var(--mut);
}
#ghost .head .ttl{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#ghost .head button{
  background:transparent;border:1px solid var(--line);border-radius:8px;
  padding:2px 8px;font-size:11px;cursor:pointer;color:var(--mut);font-family:var(--font);
}
#ghost .head button:hover{
  color:var(--text);border-color:rgba(184,115,51,.45);background:rgba(184,115,51,.06);
}
#ghost img{display:block;width:100%;background:#fff}
#ghost.min img{display:none}
@media(max-width:900px){#ghost{width:280px;bottom:150px}}
@media(max-width:640px){
  .composer{flex-wrap:wrap}
  #dry,#studyMode,#send,#ctxBtn{height:44px}
  #ctxBtn{flex:0 0 auto}
  #dry,#studyMode{flex:1} #send{flex:0 0 auto}
  .msg{max-width:100%}
  .msg.user .msg-body{max-width:92%;text-align:left}
  .msg.user,.msg.user .msg-label{align-items:flex-start;align-self:flex-start}
  #ghost{display:none !important}
}
</style></head><body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Chat</span>
  @@NAV@@
  <span class="spacer"></span>
  <div class="chat-tools">
    <button type="button" id="pastBtn" title="Browse saved conversations">Past</button>
    <button type="button" id="newChatBtn" title="Save this chat and start fresh">New</button>
    <div id="pastPanel" role="menu" aria-label="Past conversations">
      <div class="past-head"><span>Saved chats</span><span id="pastCount"></span></div>
      <div id="pastList"><div class="past-empty">No saved chats yet.</div></div>
    </div>
  </div>
  <div class="meta">
    <span id="url"></span>
    <span id="policy"></span>
    <span id="cost"></span>
  </div>
</header>
<aside id="ambientChat" aria-hidden="true"></aside>
<div id="archiveBanner">
  <span id="archiveBannerText">Viewing a saved conversation (read-only).</span>
  <button type="button" id="backLiveBtn">Back to live</button>
</div>
<div id="log"></div>
@@UI_JS@@
<div id="ghost">
  <div class="head">
    <span class="ttl" id="ghostttl">Agent browser</span>
    <button id="ghostreveal" title="Bring the agent's browser window on-screen (e.g. to sign in), or park it again">reveal</button>
    <button id="ghostmin" title="Collapse">–</button>
  </div>
  <img id="ghostimg" alt="agent browser view">
</div>
<div class="dock">
  <div id="bar">
    <span id="waiting"></span>
    <details class="action-detail" id="dockDetail">
      <summary>What will happen</summary>
      <div class="detail-card">
        <p class="intent" id="dockIntent"></p>
        <ol class="steps" id="dockSteps"></ol>
        <div class="payload" id="dockPayload" hidden></div>
      </div>
    </details>
    <form method="post" action="/approvals/resolve" class="approval-form" style="display:inline">
      <input type="hidden" name="accept" value="1">
      <input type="hidden" name="next" value="/chat">
      <button type="submit" class="yes">✓ Yes</button>
    </form>
    <form method="post" action="/approvals/resolve" class="approval-form" style="display:inline">
      <input type="hidden" name="accept" value="0">
      <input type="hidden" name="next" value="/chat">
      <button type="submit" class="no">✕ No</button>
    </form>
  </div>
  <div class="composer-wrap">
    <div id="ctxPanel">
      <div class="ctx-label">
        <span>Extra context for the next message (notes, files, photos)</span>
        <button type="button" id="ctxClear" title="Clear context">Clear</button>
      </div>
      <textarea id="ctxBox" placeholder="Paste facts, constraints, or background the model should treat as authoritative for this turn…"></textarea>
      <div id="ctxFiles" aria-live="polite"></div>
      <button type="button" id="ctxAttach" title="Attach a document or photo — saved to memory to learn about you">+ Attach document or photo</button>
      <input type="file" id="ctxFileInput" multiple accept=".txt,.md,.markdown,.pdf,.docx,.rst,.text,.log,.jpg,.jpeg,.png,.webp,.gif,.bmp,image/*,text/plain,application/pdf" hidden>
      <div id="ctxLearn">Attachments are kept in memory (reviewable in Memory) so @@BRAND@@ can learn about you.</div>
    </div>
    <div class="composer">
      <textarea id="box" placeholder="Ask @@BRAND@@, or give the agent a task… (show a to-do list to the camera and it will offer to run it)"></textarea>
      <button type="button" id="ctxBtn" title="Add notes, documents, or photos for the next message">+ Context</button>
      <select id="studyMode" title="Study mode — how the assistant coaches this session">
        <option value="general">Mode: General</option>
        <option value="lecture_notes">Lecture notes</option>
        <option value="homework">Homework help</option>
        <option value="study_quiz">Study / quiz</option>
        <option value="syllabus">Syllabus &amp; deadlines</option>
        <option value="essay_rubric">Essay / rubric</option>
        <option value="reading">Reading / textbook</option>
      </select>
      <select id="dry" title="How far the agent may go this turn">
        <option value="">Posture: default</option>
        <option value="plan">Plan only</option>
        <option value="navigate">Navigate only</option>
        <option value="draft">Draft only</option>
        <option value="approval">Approval</option>
        <option value="full">Full (autonomous)</option>
        <option value="autonomous">Autonomous</option>
      </select>
      <button id="send" onclick="send()">Send</button>
    </div>
  </div>
</div>
<script>
let since=0, awaiting=false, todo=false, polling=false, approvalMode=false;
let lastErrShown=null; // dedup: state.error persists across polls — show once
let liveMode=true;
const log=document.getElementById('log'), box=document.getElementById('box');
function fillDockDetail(s){
  const det=document.getElementById('dockDetail');
  if(!det) return;
  const pkt=s&&s.packet;
  const fields=(pkt&&pkt.fields)||{};
  const intent=(fields.action||(pkt&&pkt.summary)||s.waiting_on||s.question||'').trim();
  document.getElementById('dockIntent').textContent=intent||'Mnemos is waiting for your decision.';
  const steps=[];
  if(fields.to) steps.push('Compose to '+fields.to);
  if(fields.subject) steps.push('Subject: '+fields.subject);
  if(fields.action&&!steps.length) steps.push(fields.action);
  if(!steps.length&&intent) steps.push(intent);
  document.getElementById('dockSteps').innerHTML=steps.map(x=>'<li>'+MnemosEsc(String(x))+'</li>').join('');
  const body=(fields.body||fields.details||'').trim();
  const payload=document.getElementById('dockPayload');
  const outbound=!!(body||fields.to||/email|message|send|post|sms|text/i.test(intent));
  if(body){ payload.hidden=false; payload.textContent=body; }
  else { payload.hidden=true; payload.textContent=''; }
  det.open=outbound;
  // Show detail whenever the bar is visible (mobile + minimized ghost).
  det.style.display=(s&&(s.awaiting||s.todo_pending))?'block':'none';
}
window.addEventListener('mnemos:approval-resolved',()=>{ try{ poll(); }catch(e){} });
window.addEventListener('mnemos:approval',()=>{ try{ poll(); }catch(e){} });
const ctxBtn=document.getElementById('ctxBtn'), ctxPanel=document.getElementById('ctxPanel'),
      ctxBox=document.getElementById('ctxBox'), ctxClear=document.getElementById('ctxClear'),
      ctxAttach=document.getElementById('ctxAttach'), ctxFileInput=document.getElementById('ctxFileInput'),
      ctxFiles=document.getElementById('ctxFiles');
const pastBtn=document.getElementById('pastBtn'), pastPanel=document.getElementById('pastPanel'),
      pastList=document.getElementById('pastList'), pastCount=document.getElementById('pastCount'),
      newChatBtn=document.getElementById('newChatBtn'),
      archiveBanner=document.getElementById('archiveBanner'),
      archiveBannerText=document.getElementById('archiveBannerText'),
      backLiveBtn=document.getElementById('backLiveBtn');
// Pending attachments for the next send: {id,name,kind,context,summary,status,error}
let pendingAttach=[];
let attachSeq=0;
MnemosMemory.set('lastRoute','/chat');
(function restoreChat(){
  const st=MnemosMemory.get('chat',{});
  if(st.dry) document.getElementById('dry').value=st.dry;
  if(st.mode) document.getElementById('studyMode').value=st.mode;
  if(st.draft) box.value=st.draft;
  if(st.ctx){ ctxBox.value=st.ctx; }
  if(st.ctxOpen){ ctxPanel.classList.add('open'); ctxBtn.classList.add('on'); }
})();
function persistChat(){
  MnemosMemory.set('chat',{
    dry:document.getElementById('dry').value||'',
    mode:document.getElementById('studyMode').value||'general',
    draft:box.value||'',
    ctx:ctxBox.value||'',
    ctxOpen:ctxPanel.classList.contains('open')
  });
}
function fmtWhen(iso){
  if(!iso) return '';
  try{
    const d=new Date(iso); if(isNaN(d)) return iso;
    return d.toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  }catch(e){ return iso; }
}
function setLiveMode(on){
  liveMode=!!on;
  archiveBanner.classList.toggle('show', !liveMode);
  box.disabled=!liveMode;
  document.getElementById('send').disabled=!liveMode;
  if(liveMode) archiveBannerText.textContent='Viewing a saved conversation (read-only).';
}
async function refreshPast(){
  try{
    const r=await fetch('/chat/sessions?limit=40'); const j=await r.json();
    const sessions=(j&&j.sessions)||[];
    pastCount.textContent=sessions.length?String(sessions.length):'';
    if(!sessions.length){
      pastList.innerHTML='<div class="past-empty">No saved chats yet. Hit New after a conversation to archive it.</div>';
      return;
    }
    pastList.innerHTML='';
    for(const s of sessions){
      const b=document.createElement('button');
      b.type='button'; b.className='past-item';
      b.innerHTML='<span class="past-title"></span><span class="past-meta"></span>';
      b.querySelector('.past-title').textContent=s.title||'Untitled chat';
      b.querySelector('.past-meta').textContent=
        fmtWhen(s.saved_at)+(s.n_turns!=null?(' · '+s.n_turns+' turn'+(s.n_turns===1?'':'s')):'');
      b.onclick=()=>openPast(s.id, s.title||'Untitled chat');
      pastList.appendChild(b);
    }
  }catch(e){
    pastList.innerHTML='<div class="past-empty">Could not load saved chats.</div>';
  }
}
async function openPast(id, title){
  pastPanel.classList.remove('open');
  try{
    const r=await fetch('/chat/sessions/'+encodeURIComponent(id));
    const j=await r.json();
    if(!r.ok||!j.session){ alert((j&&j.detail)||'Could not open saved chat'); return; }
    setLiveMode(false);
    archiveBannerText.textContent='Viewing “'+(title||j.session.title||'saved chat')+'” (read-only).';
    log.innerHTML='';
    for(const e of (j.session.events||[])){
      add(e.kind, e.text, e.distill_id, e.sources, e.packet, null);
    }
    if(!(j.session.events||[]).length){
      add('system','(empty saved chat)');
    }
  }catch(e){ alert('Could not open saved chat'); }
}
async function backToLive(){
  setLiveMode(true);
  log.innerHTML='';
  since=0;
  await poll();
}
async function newChat(){
  if(!liveMode){
    await backToLive();
  }
  if(!confirm('Start a new conversation? The current chat will be saved if it has messages.')) return;
  pastPanel.classList.remove('open');
  try{
    const r=await fetch('/chat/new',{method:'POST'});
    const j=await r.json().catch(()=>({}));
    if(!r.ok||j.ok===false){ alert(j.error||j.detail||'Could not start a new chat'); return; }
    setLiveMode(true);
    log.innerHTML='';
    since=0;
    await poll();
    refreshPast();
  }catch(e){ alert('Could not start a new chat'); }
}
pastBtn.onclick=(ev)=>{
  ev.stopPropagation();
  const open=!pastPanel.classList.contains('open');
  pastPanel.classList.toggle('open', open);
  if(open) refreshPast();
};
newChatBtn.onclick=()=>newChat();
backLiveBtn.onclick=()=>backToLive();
document.addEventListener('click',(ev)=>{
  if(!pastPanel.classList.contains('open')) return;
  if(pastPanel.contains(ev.target)||pastBtn.contains(ev.target)) return;
  pastPanel.classList.remove('open');
});
box.addEventListener('input', persistChat);
document.getElementById('dry').addEventListener('change', persistChat);
async function setStudyMode(id){
  const mode=id||document.getElementById('studyMode').value||'general';
  document.getElementById('studyMode').value=mode;
  persistChat();
  try{
    await fetch('/chat/mode',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode})});
  }catch(e){}
}
document.getElementById('studyMode').addEventListener('change',()=>setStudyMode());
(async function syncStudyMode(){
  try{
    const r=await fetch('/chat/mode'); const j=await r.json();
    if(j&&j.id){ document.getElementById('studyMode').value=j.id; persistChat(); }
  }catch(e){}
})();
function renderAttach(){
  ctxFiles.innerHTML='';
  for(const a of pendingAttach){
    const el=document.createElement('div');
    el.className='ctx-file '+(a.status||'');
    const kind=a.kind==='photo'?'photo':(a.kind==='document'?'doc':'file');
    const meta=a.status==='pending'?'uploading…'
      :(a.status==='err'?(a.error||'failed')
      :(a.facts_pending?'saved · mining facts…'
      :(a.facts!=null?('saved · '+(a.facts||0)+' facts'):'saved')));
    el.innerHTML='<span class="ctx-file-name" title="'+(a.name||'')+'">'
      +kind+' · '+(a.name||'file')+'</span>'
      +'<span class="ctx-file-meta">'+meta+'</span>';
    if(a.status!=='pending'){
      const rm=document.createElement('button');rm.type='button';rm.title='Remove from this message';
      rm.textContent='×';rm.onclick=()=>{pendingAttach=pendingAttach.filter(x=>x.id!==a.id);renderAttach();syncCtxBtn();};
      el.appendChild(rm);
    }
    ctxFiles.appendChild(el);
  }
}
function syncCtxBtn(){
  const nOk=pendingAttach.filter(a=>a.status==='ok').length;
  const nPend=pendingAttach.filter(a=>a.status==='pending').length;
  const has=!!(ctxBox.value||'').trim() || nOk>0 || nPend>0;
  ctxBtn.classList.toggle('has', has && !ctxPanel.classList.contains('open'));
  let label='+ Context';
  if(has){
    const bits=[];
    if((ctxBox.value||'').trim()) bits.push('notes');
    if(nOk||nPend) bits.push((nOk+nPend)+' file'+(nOk+nPend===1?'':'s'));
    label='Context ✓'+(bits.length?' · '+bits.join(' + '):'');
  }
  ctxBtn.textContent=label;
  persistChat();
}
ctxBtn.onclick=()=>{
  ctxPanel.classList.toggle('open');
  ctxBtn.classList.toggle('on', ctxPanel.classList.contains('open'));
  if(ctxPanel.classList.contains('open')) ctxBox.focus();
  syncCtxBtn();
};
ctxClear.onclick=()=>{
  ctxBox.value=''; pendingAttach=[]; renderAttach(); syncCtxBtn();
};
ctxBox.addEventListener('input', syncCtxBtn);
ctxAttach.onclick=()=>ctxFileInput.click();
ctxFileInput.addEventListener('change', async()=>{
  const files=[...ctxFileInput.files||[]];
  ctxFileInput.value='';
  if(!files.length) return;
  ctxPanel.classList.add('open'); ctxBtn.classList.add('on');
  for(const f of files){
    const id=++attachSeq;
    const row={id,name:f.name,kind:'',context:'',summary:'',status:'pending',facts:null,facts_pending:false,error:''};
    pendingAttach.push(row); renderAttach(); syncCtxBtn();
    ctxAttach.disabled=true;
    try{
      const fd=new FormData(); fd.append('file', f, f.name);
      const r=await fetch('/chat/attach',{method:'POST',body:fd});
      const j=await r.json().catch(()=>({}));
      if(!r.ok){
        row.status='err'; row.error=j.detail||('upload failed ('+r.status+')');
      }else{
        row.status='ok'; row.kind=j.kind||''; row.context=j.context||'';
        row.summary=j.summary||''; row.facts=j.facts||0;
        row.facts_pending=!!j.facts_pending; row.path=j.path||'';
      }
    }catch(e){
      row.status='err'; row.error=String(e.message||e);
    }
    renderAttach(); syncCtxBtn();
  }
  ctxAttach.disabled=false;
});
function bindFolioSeal(root){
  const approve=root.querySelector('.seal-approve');
  const cancel=root.querySelector('.seal-cancel');
  if(!approve) return;
  const row=root.querySelector('.seal-row')||root;
  const packetId=row.getAttribute('data-packet-id')||'';
  const payloadHash=row.getAttribute('data-payload-hash')||'';
  async function decide(decision, extra){
    if(!packetId||!payloadHash){
      // Legacy folio without bind metadata — fall back to typed reply.
      reply(decision==='cancel'?'cancel':(extra&&extra.user_edit)||'approve');
      return;
    }
    const body=Object.assign({
      payload_hash:payloadHash,
      decision:decision,
      approved_via:'button',
    }, extra||{});
    try{
      const r=await fetch('/approval/'+encodeURIComponent(packetId)+'/decide',{
        method:'POST',
        headers:{'Content-Type':'application/json','Accept':'application/json'},
        body:JSON.stringify(body),
      });
      const j=await r.json().catch(()=>({}));
      if(!r.ok||j.ok===false){
        add('system', (j&&j.error)||('Approval refused ('+r.status+')'));
        return;
      }
      try{ window.dispatchEvent(new CustomEvent('mnemos:approval-resolved',{detail:j})); }catch(e){}
    }catch(e){
      add('system','Approval request failed: '+String(e.message||e));
    }
  }
  MnemosSeal.bind(approve,{
    onApprove:()=>{
      const subjEl=root.querySelector('[data-field=subject]');
      const bodyEl=root.querySelector('[data-field=body]');
      const fields={};
      let changed=false;
      if(subjEl&&subjEl.defaultValue!==subjEl.value){
        fields.subject=subjEl.value; changed=true;
      }
      if(bodyEl&&bodyEl.defaultValue!==bodyEl.value){
        fields.body=bodyEl.value; changed=true;
      }
      if(changed){
        let msg='Please revise: ';
        if(fields.subject!=null) msg+='subject → '+fields.subject+'. ';
        if(fields.body!=null) msg+='body → '+fields.body;
        decide('edit',{user_edit:msg.trim(), fields:fields});
      } else {
        decide('approve');
      }
    }
  });
  if(cancel) cancel.onclick=()=>decide('cancel');
}
function add(kind,text,distillId,sources,packet,compiled){
  const d=document.createElement('div');d.className='msg '+kind;
  const pkt=packet||(kind==='ask'?MnemosParsePacket(text):null);
  if(kind==='ask' && pkt && pkt.kind==='approval'){
    d.className='msg ask folio-wrap';
    d.innerHTML=MnemosRenderFolio(pkt,{editable:true,meta:'Hold to seal · release early to abort'});
    bindFolioSeal(d);
    log.appendChild(d);log.scrollTop=log.scrollHeight;
    return;
  }
  const labels={user:'You',result:'@@BRAND@@',ask:'Needs you',error:'Issue',
    system:'',progress:''};
  const label=labels[kind];
  if(label){
    const lab=document.createElement('div');lab.className='msg-label';
    lab.textContent=label;d.appendChild(lab);
  }
  const shellNeeded=kind==='result'||kind==='ask'||kind==='error';
  const host=shellNeeded?document.createElement('div'):d;
  if(shellNeeded){host.className='msg-shell';d.appendChild(host);}
  const body=document.createElement('div');body.className='msg-body';
  const doc=compiled||null;
  const useDoc=kind==='result' && doc && doc.sections && doc.sections.length
    && window.MnemosResponse;
  if(useDoc){
    // Grounding lives inside the compiled document (collapsed).
    MnemosResponse.mount(body, doc, {
      includeGrounding:true,
      onAction:(prompt)=>{
        if(!prompt) return;
        box.value=prompt; persistChat(); send();
      }
    });
    // Keep raw text for verdict edit fallback
    body.dataset.rawText=text||'';
  }else{
    body.textContent=text;
  }
  host.appendChild(body);
  if(kind==='result' && sources && sources.length && !useDoc){
    const det=document.createElement('details');det.className='sources';
    const total=sources.reduce((n,s)=>n+(s.n||(s.items||[]).length||0),0);
    const sum=document.createElement('summary');
    sum.textContent='Grounded in '+total+' memory source'+(total===1?'':'s');
    det.appendChild(sum);
    for(const s of sources){
      for(const it of (s.items||[])){
        const li=document.createElement('div');li.textContent='— '+it;det.appendChild(li);
      }
    }
    host.appendChild(det);
  }
  if(kind==='result' && distillId){
    const acts=document.createElement('div');acts.className='verdict';
    const mk=(labelTxt,outcome,cls)=>{
      const b=document.createElement('button');b.type='button';b.textContent=labelTxt;
      if(cls) b.className=cls;
      b.title=outcome;b.onclick=()=>verdict(acts,distillId,outcome,b);
      return b;
    };
    acts.appendChild(mk('Helpful','accepted'));
    acts.appendChild(mk('Off','rejected','bad'));
    acts.appendChild(mk('Edit','edited'));
    host.appendChild(acts);
  }
  log.appendChild(d);log.scrollTop=log.scrollHeight;
}
async function verdict(acts,distillId,outcome,btn){
  let edited=null;
  if(outcome==='edited'){
    const bodyEl=acts.parentElement&&acts.parentElement.querySelector('.msg-body');
    const cur=(bodyEl&&(bodyEl.dataset.rawText||bodyEl.innerText))||'';
    edited=prompt('Corrected answer (saved as the training target):',cur);
    if(edited==null) return;
    edited=edited.trim(); if(!edited){alert('Edit needs corrected text.'); return;}
  }
  try{
    const r=await fetch('/chat/outcome',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({distill_id:distillId,outcome:outcome,edited_text:edited})});
    if(!r.ok){const j=await r.json().catch(()=>({})); alert(j.detail||('label failed ('+r.status+')')); return;}
    [...acts.querySelectorAll('button')].forEach(b=>{b.disabled=true;b.classList.remove('on');});
    btn.classList.add('on');
    if(outcome==='edited' && edited){
      const body=acts.parentElement&&acts.parentElement.querySelector('.msg-body');
      if(body){ body.classList.remove('rd-host'); body.textContent=edited; delete body.dataset.rawText; }
    }
  }catch(e){alert('label failed: '+e);}
}
async function poll(){
 // Guard against overlap: `since` only advances after the await, so a second
 // poll firing mid-flight (send()+setInterval, or a burst after the tab
 // regains focus) would re-fetch and re-render the same events (the "exit 0
 // x8" duplication). Skip if one is already running; the cursor persists.
 if(polling) return; polling=true;
 try{
  const r=await fetch('/chat/poll?since='+since); const j=await r.json();
  for(const e of (j.events||[])){
    since=e.id+1;
    if(e.kind==='error') lastErrShown=e.text; // event already renders it
    if(liveMode) add(e.kind, e.text, e.distill_id, e.sources, e.packet, e.compiled);
  }
  const s=j.state||{};
  awaiting=!!s.awaiting; todo=!!s.todo_pending;
  approvalMode=!!(s.packet && s.packet.kind==='approval')
    || !!(s.question && /APPROVAL NEEDED/.test(s.question));
  document.getElementById('url').textContent=s.url||'';
  const pol=[]; if(s.study_mode)pol.push(s.study_mode); if(s.mode)pol.push(s.mode); if(s.dry_run&&s.dry_run!=='approval')pol.push(s.dry_run==='full'||s.dry_run==='autonomous'?'autonomous':s.dry_run);
  document.getElementById('policy').textContent=pol.join(' · ');
  document.getElementById('cost').textContent=(s.cost!=null)?('$'+Number(s.cost).toFixed(4)):'';
  const waitEl=document.getElementById('waiting');
  if(waitEl) waitEl.textContent=s.waiting_on||(awaiting?(approvalMode?'Seal the approval folio…':'Waiting on your reply…'):(todo?'Waiting on yes/no…':''));
  // Offers keep Yes/No; approvals live in the folio Seal (hide generic bar).
  document.getElementById('bar').style.display=(liveMode&&((awaiting&&!approvalMode)||todo))?'flex':'none';
  fillDockDetail(s);
  box.placeholder=!liveMode?'Viewing a saved chat — Back to live to continue…'
    :(awaiting||todo)?(approvalMode?'Edit the folio, or type a revision…':'Yes/no above, or type a new request…')
    :'Ask @@BRAND@@, or give the agent a task…';
  // Banner + NEEDS YOU card + dock Yes/No already show pending offers —
  // never mirror waiting_on into the fixed ambient column (that was the overlap).
  const notes=[];
  if(liveMode){
    if(!(approvalMode || todo)){
      if(s.waiting_on) notes.push({text:s.waiting_on,attention:false});
    }
  } else {
    notes.push({text:'Reading a saved conversation.',attention:false});
  }
  document.body.classList.toggle('has-approval', !!(approvalMode || todo));
  MnemosAmbient.render(document.getElementById('ambientChat'), notes);
  if(liveMode && s.error && s.error!==lastErrShown){
    lastErrShown=s.error; add('error', s.error);
  }
 }catch(e){}
 finally{ polling=false; }
}
async function send(){
 if(!liveMode){ alert('You are viewing a saved chat. Click Back to live first.'); return; }
 const t=box.value.trim(); if(!t) return;
 if(pendingAttach.some(a=>a.status==='pending')){
   alert('Still uploading attachments — wait a moment, then send.');
   return;
 }
 box.value='';
 const dry=document.getElementById('dry').value||null;
 const mode=document.getElementById('studyMode').value||'general';
 const note=(ctxBox.value||'').trim();
 const attachCtx=pendingAttach.filter(a=>a.status==='ok'&&a.context)
   .map(a=>a.context).join('\n\n');
 const ctxParts=[note,attachCtx].filter(Boolean);
 const ctx=ctxParts.length?ctxParts.join('\n\n'):null;
 // Sticky context + attachment snippets are one-shot with the message.
 // File contents stay in memory (source=chat.attach) for learning.
 if(note||pendingAttach.length){
   ctxBox.value=''; pendingAttach=[]; renderAttach();
   ctxPanel.classList.remove('open'); ctxBtn.classList.remove('on'); syncCtxBtn();
 }
 const payload={message:t,dry_run:dry,mode}; if(ctx) payload.context=ctx;
 await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
 poll();
}
function reply(t){ box.value=t; send(); }
box.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
setInterval(poll, 1000); poll();

// --- ghost browser pane: the agent's live view, no window on your screen ---
const ghostEl=document.getElementById('ghost'), ghostImg=document.getElementById('ghostimg'),
      ghostTtl=document.getElementById('ghostttl');
let ghostRevealed=false, ghostHideAt=0;
document.getElementById('ghostmin').onclick=()=>{
  ghostEl.classList.toggle('min');
  document.getElementById('ghostmin').textContent=ghostEl.classList.contains('min')?'+':'–';
};
document.getElementById('ghostreveal').onclick=async()=>{
  const ep=ghostRevealed?'/agent/ghost/park':'/agent/ghost/reveal';
  try{
    const j=await (await fetch(ep,{method:'POST'})).json();
    if(j.ok){ghostRevealed=!ghostRevealed;
      document.getElementById('ghostreveal').textContent=ghostRevealed?'park':'reveal';}
    else if(j.reason) ghostTtl.textContent=j.reason;
  }catch(e){}
};
async function ghostPoll(){
  if(document.hidden) return;
  try{
    const s=await (await fetch('/agent/ghost/status')).json();
    if(s.fresh){
      ghostHideAt=Date.now()+30000;   // linger a moment after the run ends
      ghostEl.style.display='block';
      ghostEl.classList.add('ink-border');
      ghostTtl.textContent=s.title||s.url||'Agent browser';
      ghostTtl.title=s.url||'';
      if(!ghostEl.classList.contains('min'))
        ghostImg.src='/agent/ghost/frame?t='+Date.now();
    }else if(Date.now()>ghostHideAt){
      ghostEl.style.display='none';
      ghostEl.classList.remove('ink-border');
    }
  }catch(e){}
}
setInterval(ghostPoll, 1200); ghostPoll();
</script></body></html>""")


_DESKTOP_ACCESS_PAGE = _mnemos(r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>@@BRAND@@ — Desktop Access</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{
  margin:0;font:14px/1.55 var(--font);color:var(--text);
  background:
    radial-gradient(900px 480px at 6% -8%, rgba(184,115,51,.05), transparent 55%),
    radial-gradient(700px 400px at 95% 5%, rgba(30,91,79,.04), transparent 50%),
    var(--paper);
  min-height:100vh;
}
.top{
  display:flex;align-items:center;gap:18px;flex-wrap:wrap;
  padding:14px 24px;
}
.page-sub{margin-left:-4px}
#msg{font-family:var(--mono);font-size:12px;color:var(--mut)}
.lead{
  color:var(--mut);font-size:13px;padding:16px 24px 0;max-width:1100px;
}
main{padding:16px 24px 40px;max-width:1100px}
.env{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.chip{
  background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:8px 12px;font-size:12px;box-shadow:var(--shadow);
  transition:border-color .28s var(--ease),transform .22s var(--ease),box-shadow .28s var(--ease);
  animation:fadeUp .3s var(--ease) both;
}
.chip:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(11,19,32,.07)}
.chip b{color:var(--text)}.chip .k{color:var(--mut)}
.chip.warn{border-color:rgba(199,138,44,.4)}.chip.ok{border-color:rgba(46,111,87,.4)}
table{width:100%;border-collapse:collapse;font-size:13px;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow)}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;background:var(--panel-2)}
tbody tr{transition:background .22s var(--ease)}
tbody tr:hover{background:rgba(184,115,51,.04)}
.badge{display:inline-block;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:600}
.b-ok{background:rgba(46,111,87,.1);color:var(--ok);border:1px solid rgba(46,111,87,.28)}
.b-no{background:rgba(166,71,71,.1);color:var(--danger);border:1px solid rgba(166,71,71,.28)}
.b-off{background:rgba(199,138,44,.1);color:var(--warn);border:1px solid rgba(199,138,44,.28)}
.path{font-family:var(--mono);font-size:11px;color:var(--mut);word-break:break-all;cursor:pointer;transition:color .22s var(--ease)}
.path:hover{color:var(--navy)}
.caps{color:var(--mut);font-size:11px}
button{
  background:var(--panel);color:var(--text);border:1px solid var(--line);
  border-radius:10px;padding:6px 11px;font-size:12px;font-family:var(--font);
  cursor:pointer;margin-right:6px;
}
button:hover{background:var(--panel-2);border-color:rgba(184,115,51,.3)}
button.danger{border-color:rgba(166,71,71,.4);color:var(--danger)}
button.danger:hover{background:rgba(166,71,71,.1);border-color:rgba(166,71,71,.55);box-shadow:0 4px 14px rgba(166,71,71,.12)}
h2{
  font-family:var(--display);font-size:1.15rem;color:var(--navy);
  font-weight:400;letter-spacing:-.01em;margin:28px 0 10px;text-transform:none;
}
h2 .caps{font-family:var(--font);font-size:12px;letter-spacing:0;text-transform:none;font-weight:500}
.rec{
  font-size:12px;border-bottom:1px solid var(--line);padding:9px 0;
  display:flex;gap:14px;flex-wrap:wrap;animation:fadeUp .3s var(--ease) both;
}
.rec .when{color:var(--mut);font-family:var(--mono);white-space:nowrap}
.rec .out-ok{color:var(--ok)}.rec .out-blocked{color:var(--danger)}.rec .out-nonzero{color:var(--warn)}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:4px 0 10px}
.stat{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:14px 16px;min-width:104px;box-shadow:var(--shadow);animation:fadeUp .35s var(--ease) both;
}
.stat .n{font-family:var(--display);font-size:1.65rem;font-weight:400;letter-spacing:-.02em;color:var(--navy)}
.stat .l{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.03em;margin-top:3px}
.safety{margin:2px 0 8px;font-size:12px;color:var(--mut)}
.safety .s-ok{color:var(--ok)}
.safety .s-tag{
  display:inline-block;background:rgba(166,71,71,.08);color:var(--danger);
  border:1px solid rgba(166,71,71,.28);border-radius:999px;padding:2px 9px;
  margin:2px 6px 2px 0;font-size:11px;
}
</style></head><body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Desktop</span>
  @@NAV@@
  <span class="spacer"></span>
  <span id="msg"></span>
</header>
<p class="lead">What the desktop agent may launch and do on this machine — the allowlist, made visible.</p>
<main>
  <div class="env" id="env"></div>
  <h2>Reliability <span class="caps" style="text-transform:none;letter-spacing:0">— measured from the audit log</span></h2>
  <div class="stats" id="stats"></div>
  <div class="safety" id="safety"></div>
  <h2>Apps</h2>
  <table><thead><tr>
    <th>App</th><th>Status</th><th>Resolved path</th><th>UI control</th>
    <th>Risk</th><th>Opens</th><th>Actions</th>
  </tr></thead><tbody id="apps"></tbody></table>
  <h2>Recent actions</h2>
  <div id="recent"></div>
</main>
<script>
const msg = document.getElementById('msg');
function note(t){ msg.textContent = t; if(t) setTimeout(()=>{if(msg.textContent===t)msg.textContent='';}, 4000); }

function envChip(k,v,cls){ return `<div class="chip ${cls||''}"><span class="k">${k}:</span> <b>${v}</b></div>`; }

async function load(){
  const s = await (await fetch('/console/desktop-access')).json();
  const e = s.environment;
  document.getElementById('env').innerHTML =
    envChip('jail', e.jail) +
    envChip('pixel UI', e.pixel_ui?'on':'off', e.pixel_ui?'ok':'off') +
    envChip('approval', e.approval_required?'required':'autonomous') +
    envChip('autonomy', e.autonomy_desktop) +
    envChip('shell auto', e.autonomy_shell?'on':'off', e.autonomy_shell?'warn':'') +
    envChip('auto-run', (e.auto_verbs||[]).join(', ')||'none') +
    envChip('needs approval', (e.gated_verbs||[]).join(', ')||'none');
  document.getElementById('apps').innerHTML = s.apps.map(row).join('');
  loadMetrics();
  loadRecent();
}
function tile(n,l){ return `<div class="stat"><div class="n">${n}</div><div class="l">${l}</div></div>`; }
async function loadMetrics(){
  const m = await (await fetch('/console/desktop-metrics')).json();
  const pct = x => Math.round((x||0)*100)+'%';
  document.getElementById('stats').innerHTML =
    tile(m.totals.records, 'actions') +
    tile(pct(m.launch.success_rate), 'launch success') +
    tile(pct(m.run_command.success_rate), 'run_cmd exit-0') +
    tile(pct(m.totals.refusal_rate), 'refusal rate') +
    tile(m.per_task.avg_actions, 'avg actions/task') +
    tile(m.repeated_failures, 'repeat-fail loops');
  const unsafe = Object.entries(m.safety).filter(([k,v])=>v>0);
  document.getElementById('safety').innerHTML = 'Safety refusals: ' + (unsafe.length
    ? unsafe.map(([k,v])=>`<span class="s-tag">${k.replace(/_/g,' ')}: ${v}</span>`).join('')
    : '<span class="s-ok">none recorded</span>');
}
function statusBadge(a){
  if(a.disabled) return '<span class="badge b-no">Disabled</span>';
  if(!a.installed) return '<span class="badge b-off">Not found</span>';
  return '<span class="badge b-ok">Launch allowed</span>';
}
function ui(a){
  if(a.special) return '<span class="caps">special (SMS)</span>';
  if(a.ui_control==='n/a') return '<span class="caps">n/a</span>';
  return a.ui_control==='on' ? '<span class="badge b-ok">on</span>'
                            : '<span class="badge b-off">off</span>';
}
function opens(a){
  const bits=[]; if(a.opens_dirs) bits.push('folder');
  if(a.opens_files&&a.opens_files.length) bits.push(a.opens_files.join(' '));
  return `<span class="caps">${bits.join(' · ')||'launch only'}</span>`;
}
function row(a){
  const p = a.resolved_path ? `<span class="path" title="click to copy" onclick="navigator.clipboard.writeText('${a.resolved_path.replace(/\\/g,'\\\\')}');note('path copied')">${a.resolved_path}</span>` : '<span class="caps">—</span>';
  const toggle = a.disabled
    ? `<button onclick="toggle('${a.key}',false)">Enable</button>`
    : `<button class="danger" onclick="toggle('${a.key}',true)">Disable</button>`;
  const test = a.launch_allowed ? `<button onclick="testLaunch('${a.key}')">Test launch</button>` : '';
  return `<tr>
    <td><b>${a.display_name}</b><br><span class="caps">${a.key}</span></td>
    <td>${statusBadge(a)}</td><td>${p}</td><td>${ui(a)}</td>
    <td class="caps">${a.risk}</td><td>${opens(a)}</td>
    <td>${toggle}${test}</td></tr>`;
}
async function toggle(app, disabled){
  await fetch('/console/desktop-access/toggle',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({app,disabled})});
  note(disabled?`${app} disabled`:`${app} enabled`);
  load();
}
async function testLaunch(app){
  note(`launching ${app}…`);
  const r = await (await fetch('/console/desktop-access/test-launch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({app})})).json();
  note(r.ok ? `${app} launched` : `refused: ${r.detail}`);
  loadRecent();
}
async function loadRecent(){
  const r = await (await fetch('/console/desktop-access/recent?limit=10')).json();
  document.getElementById('recent').innerHTML = (r.recent||[]).map(a=>
    `<div class="rec"><span class="when">${a.when||''}</span>
     <span class="out-${a.outcome||''}">${a.action}</span>
     <span>${a.target||''}</span>
     <span class="caps">${a.detail||''}</span></div>`).join('') || '<div class="caps">no actions yet</div>';
}
load();
</script>
@@UI_JS@@
</body></html>""")


_CONSOLE_PAGE = _mnemos(r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>@@BRAND@@ — Memory Console</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{
  margin:0;font:15px/1.55 var(--font);color:var(--text);
  height:100vh;display:flex;flex-direction:column;
  background:
    radial-gradient(900px 480px at 8% -10%, rgba(184,115,51,.05), transparent 55%),
    radial-gradient(700px 400px at 94% 0%, rgba(30,91,79,.04), transparent 50%),
    var(--paper);
}
.layout{flex:1;display:grid;grid-template-columns:1fr minmax(180px,220px);min-height:0}
@media(max-width:860px){.layout{grid-template-columns:1fr}#consoleAmbient{display:none}}
#consoleAmbient{padding:16px 14px;border-left:1px solid var(--line);overflow:auto}
#consoleAmbient h2{font:600 11px/1 var(--font);letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin:0 0 12px}
#constPane{display:none;flex:1;min-height:0;padding:12px 20px 20px;align-items:center}
#constPane.on{display:flex;flex-direction:column}
#constPane .const-frame{
  position:relative;width:min(560px,100%);height:min(420px,62vh);margin:0 auto;
  border:1px solid var(--line);border-radius:var(--radius);background:var(--bg-elev);
  box-shadow:var(--shadow-surface);overflow:hidden;
}
#constPane canvas{width:100%;height:100%;display:block;touch-action:none}
.const-tools{
  position:absolute;right:10px;bottom:10px;z-index:2;display:flex;gap:4px;
  background:rgba(255,254,251,.92);border:1px solid var(--line);border-radius:10px;
  padding:4px;box-shadow:var(--shadow-workspace);
}
.const-tools button{
  width:32px;height:28px;border:0;background:transparent;border-radius:8px;
  font:600 13px var(--font);color:var(--navy);cursor:pointer;padding:0;
  box-shadow:none;transform:none;
}
.const-tools button:hover{background:var(--panel-2);transform:none;box-shadow:none}
.const-tools button[data-act="fit"],
.const-tools button[data-act="focus"],
.const-tools button[data-act="filaments"],
.const-tools button[data-act="correct"],
.const-tools button[data-act="diff"]{width:auto;padding:0 10px;font-size:12px;font-weight:500;color:var(--mut)}
.const-tools button.on{color:var(--acc);background:var(--acc-dim)}
.const-tip{
  position:absolute;z-index:4;max-width:200px;padding:8px 10px;border-radius:10px;
  background:rgba(255,254,251,.96);border:1px solid var(--line);box-shadow:var(--shadow-workspace);
  font-size:12px;pointer-events:none;line-height:1.35;
}
.const-tip strong{display:block;font-family:var(--display);font-weight:400;font-size:1rem;color:var(--navy)}
.const-tip-kind{font:11px var(--mono);color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.const-tip-why{margin-top:4px;color:var(--mut);font-style:italic}
.const-insight{
  /* bottom:52px, not 10px — the const-tools row owns the frame's bottom strip;
     insight cards stack in the clear band above it so neither covers the other
     however narrow the frame gets. */
  position:absolute;left:10px;bottom:52px;z-index:2;max-width:min(320px,80%);
  display:flex;flex-direction:column;gap:6px;
}
.const-insight-btn{
  text-align:left;border:1px solid var(--line);background:rgba(255,254,251,.94);
  border-radius:10px;padding:8px 10px;font:12px var(--font);color:var(--navy);cursor:pointer;
  box-shadow:var(--shadow-workspace);
}
.const-insight-btn:hover{border-color:rgba(184,115,51,.4)}
.const-why{margin:6px 0;color:var(--mut);font-style:italic;line-height:1.4}
.const-rank{margin:8px 0 10px;padding-top:6px;border-top:1px solid var(--line)}
.const-rank-title{font:11px var(--mono);letter-spacing:.04em;text-transform:uppercase;
  color:var(--mut);margin-bottom:4px}
.const-rank-admit{color:var(--navy);font-style:italic;margin:0 0 6px;line-height:1.35}
.const-rank-bar{display:flex;height:8px;border-radius:4px;overflow:hidden;
  background:rgba(11,19,32,.06);gap:1px;margin:4px 0 2px}
.const-rank-seg{display:block;min-width:2px;height:100%}
.const-rank-total{font:11px var(--mono);color:var(--mut);margin-bottom:6px}
.const-rank-list{display:flex;flex-direction:column;gap:2px}
.const-rank-row{display:flex;justify-content:space-between;gap:8px;align-items:baseline;
  width:100%;text-align:left;border:0;background:transparent;padding:4px 0;
  cursor:pointer;font:12px var(--font);color:var(--navy);border-radius:0}
.const-rank-row:hover{color:var(--acc)}
.const-rank-label{flex:1;line-height:1.35}
.const-rank-val{font:11px var(--mono);color:var(--mut);flex-shrink:0}
.const-rank-ev{padding:0 0 4px 2px;margin:0 0 4px}
.const-edit-actions{display:flex;gap:6px;margin:8px 0}
.const-ev-row{padding:6px 0;border-top:1px solid var(--line);line-height:1.35}
.const-ev-ch{font:10px var(--mono);color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.const-ev-body{margin-top:4px}
.const-ev-transcript{line-height:1.45;color:var(--ink)}
.const-ev-quote{margin-top:4px;font-style:italic;color:var(--mut)}
.const-ev-audio{display:block;width:100%;height:28px;margin-top:6px}
.const-play-moment{margin-top:6px}
mark.span-hl{
  background:rgba(184,115,51,.22);color:inherit;padding:0 .12em;border-radius:2px;
}
.const-kind-select{
  width:100%;margin-top:4px;padding:7px 10px;border-radius:8px;border:1px solid var(--line);
  background:var(--panel);color:var(--navy);font:13px var(--font);
}
.const-edit{
  position:absolute;left:10px;top:10px;z-index:3;width:min(260px,72%);
  background:rgba(255,254,251,.97);border:1px solid var(--line);border-radius:12px;
  box-shadow:var(--shadow-folio);padding:10px 12px;font-size:12px;max-height:78%;overflow:auto;
}
.const-edit-head{display:flex;justify-content:space-between;gap:8px;align-items:baseline;
  font-family:var(--display);font-size:1.05rem;color:var(--navy);margin-bottom:6px}
.const-edit-hint{color:var(--mut);font-style:italic;margin:4px 0 8px;line-height:1.4}
.const-edit-list{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.const-edit-row{display:flex;justify-content:space-between;gap:8px;align-items:center;
  padding:6px 0;border-top:1px solid var(--line)}
.const-edit-row button,.const-link-btn{
  border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:4px 8px;
  font:12px var(--font);cursor:pointer;color:var(--navy);box-shadow:none;transform:none;
}
.const-edit-row button:hover{border-color:rgba(166,71,71,.45);color:var(--danger)}
.const-link-btn{width:100%;margin-top:4px}
.const-link-btn:hover{border-color:rgba(184,115,51,.45);color:var(--acc)}
.const-edit .linkish{background:none;border:0;color:var(--mut);cursor:pointer;font:12px var(--font);padding:0}
.const-frame.editing{box-shadow:var(--shadow-folio),inset 0 0 0 1px rgba(184,115,51,.18)}
.mode-toggle{display:flex;gap:6px;padding:0;align-items:center;flex-wrap:wrap}
.mode-toggle .chip.on{color:var(--navy);background:rgba(11,19,32,.06);border-color:rgba(11,19,32,.12)}
.mode-seg{display:inline-flex;gap:0;border:1px solid var(--line);border-radius:10px;overflow:hidden;flex:0 0 auto}
.mode-seg .chip{border:0;border-radius:0;margin:0}
.mode-seg .chip + .chip{border-left:1px solid var(--line)}
.chrome{border-bottom:1px solid var(--line);position:relative;z-index:5}
.chrome-tools{
  display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  padding:8px 20px 10px;background:rgba(248,246,241,.94);
  border-bottom:1px solid var(--line);
  position:sticky;top:0;z-index:4;
  transition:transform .22s var(--ease),opacity .22s var(--ease);
}
.chrome-tools.tucked{transform:translateY(-110%);opacity:0;pointer-events:none}
.chrome-tools .tabs{display:flex;gap:6px;flex:1;flex-wrap:wrap;padding:0;min-width:0}
.row{position:relative}
.row.holdable::before{
  content:"";position:absolute;left:0;top:18px;bottom:18px;width:2px;
  background:var(--acc);border-radius:2px;opacity:.85;
}
.top{
  display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:12px 20px 10px;
}
.page-sub{margin-left:-4px}
.mut{color:var(--mut);font-size:13px}
.meta-bar{display:flex;gap:12px;align-items:center;font-family:var(--mono);font-size:12px;color:var(--mut)}
input,button,select{font:inherit}
#q{
  background:var(--panel);color:var(--text);border:1px solid var(--line);
  border-radius:10px;padding:8px 12px;width:min(280px,100%);
  box-shadow:var(--shadow);transition:border-color .28s var(--ease),box-shadow .28s var(--ease);
}
#q:focus{outline:none;border-color:rgba(184,115,51,.45);box-shadow:0 0 0 3px var(--acc-dim)}
.btn{
  background:var(--panel);color:var(--text);border:1px solid var(--line);
  border-radius:10px;padding:7px 12px;cursor:pointer;font-size:13px;
}
.btn:hover{background:var(--panel-2);border-color:rgba(184,115,51,.35);color:var(--navy)}
.tabs{
  display:flex;gap:6px;flex-wrap:wrap;align-items:center;
  padding:0;
}
.chip{
  background:var(--panel);color:var(--mut);border:1px solid var(--line);
  border-radius:999px;padding:6px 13px;cursor:pointer;font-size:13px;font-weight:500;
  transition:background .28s var(--ease),border-color .28s var(--ease),color .28s var(--ease),
    transform .22s var(--ease),box-shadow .28s var(--ease);
}
.chip:hover{
  color:var(--navy);border-color:rgba(184,115,51,.35);
  transform:translateY(-1px);box-shadow:0 4px 12px rgba(11,19,32,.06);
}
.chip:active{transform:translateY(0) scale(.98)}
.chip.on{background:var(--acc-dim);color:var(--acc);border-color:rgba(184,115,51,.4)}
.chip.on:hover{color:var(--acc);background:rgba(184,115,51,.16)}
.mode-caption{font:12px var(--font);color:var(--mut);padding:2px 12px 6px;font-style:italic}
.ambient-note.actionable{cursor:pointer;background:transparent;border:0;text-align:left;width:100%;
  font:inherit;color:inherit;padding:0;display:block}
.ambient-note.actionable:hover{color:var(--acc)}
.ambient-act{display:block;margin-top:3px;font:11px var(--mono);color:var(--mut)}
#list{
  flex:1;overflow:auto;padding:18px 20px 28px;
  display:flex;flex-direction:column;gap:10px;max-width:960px;width:100%;
  margin:0 auto;align-self:center;
}
.row{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:13px 16px;display:flex;gap:14px;align-items:flex-start;
  box-shadow:var(--shadow);animation:fadeUp .32s var(--ease) both;
  position:relative;
  transition:border-color .28s var(--ease),box-shadow .28s var(--ease),transform .22s var(--ease);
}
.row:hover{
  border-color:rgba(184,115,51,.22);
  box-shadow:0 4px 18px rgba(11,19,32,.07);
  transform:translateY(-1px);
}
.row::before{
  content:"";position:absolute;left:0;top:18px;bottom:18px;width:2px;
  background:var(--acc);border-radius:2px;
  opacity:.85;
}
.row.low{border-color:rgba(199,138,44,.35);background:rgba(199,138,44,.05)}
.badge{
  font-size:11px;font-weight:600;padding:3px 9px;border-radius:8px;
  white-space:nowrap;margin-top:2px;letter-spacing:.02em;
}
.b-audio{background:rgba(30,91,79,.1);color:var(--audio)}
.b-vision{background:rgba(184,115,51,.12);color:var(--vision)}
.b-desktop{background:rgba(110,36,51,.1);color:var(--desktop)}
.b-other{background:var(--panel-2);color:var(--mut)}
.actev{margin-top:10px;display:flex;flex-direction:column;gap:8px}
.actev .row{background:var(--bg-elev)}
.actev .row::before{opacity:.25}
.body{flex:1;min-width:0}.text{white-space:pre-wrap;word-wrap:break-word}
.meta{
  margin-top:6px;font-size:12px;color:var(--mut);font-family:var(--mono);
  display:flex;gap:10px;flex-wrap:wrap;align-items:center;
}
.spk{color:var(--emerald);font-family:var(--font)}.lowtag{color:var(--warn)}
audio{height:30px;margin-top:8px;max-width:320px;display:block}
img.thumb{margin-top:8px;max-height:120px;border-radius:10px;border:1px solid var(--line);cursor:zoom-in}
img.thumb.big{max-height:none;max-width:100%}
.empty{color:var(--mut);text-align:center;margin:72px 16px;line-height:1.6}
.prov{
  margin-top:8px;padding:8px 12px;border-left:2px solid var(--acc);
  color:var(--mut);font-size:13px;font-style:italic;background:rgba(184,115,51,.05);border-radius:0 10px 10px 0;
}
.prov audio{height:28px;margin-top:6px;font-style:normal;display:block;width:100%}
.prov .span-transcript{font-style:normal;color:var(--ink);line-height:1.45}
.prov mark.span-hl{
  background:rgba(184,115,51,.22);color:inherit;padding:0 .12em;border-radius:2px;font-style:normal;
}
.prov .play-moment{
  display:inline-block;margin-top:6px;border:1px solid var(--line);background:var(--panel);
  border-radius:8px;padding:4px 10px;font:12px var(--font);cursor:pointer;color:var(--navy);
  font-style:normal;
}
.prov .play-moment:hover{border-color:rgba(184,115,51,.45);color:var(--acc)}
.acts{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}
.mini{
  border:1px solid var(--line);background:var(--bg-elev);color:var(--text);
  border-radius:10px;padding:5px 12px;cursor:pointer;font-size:13px;
}
.mini:hover{border-color:rgba(184,115,51,.45);background:rgba(184,115,51,.05);color:var(--navy)}
.mini.done:hover{
  border-color:rgba(46,111,87,.5);color:var(--ok);background:rgba(46,111,87,.08);
  box-shadow:0 4px 12px rgba(46,111,87,.12);
}
.mini.drop:hover{
  border-color:rgba(166,71,71,.5);color:var(--danger);background:rgba(166,71,71,.08);
  box-shadow:0 4px 12px rgba(166,71,71,.12);
}
.sechead{
  color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  font-weight:600;margin:10px 2px 4px;
}
.rev{color:var(--ok);text-transform:uppercase;font-size:11px;letter-spacing:.4px}
.refl{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:14px 16px;margin-bottom:6px;box-shadow:var(--shadow);animation:fadeUp .32s var(--ease) both;
}
.refl .sum{font-size:15px;margin-top:6px}
.kind{
  font-size:11px;font-weight:600;padding:3px 9px;border-radius:8px;
  background:var(--panel-2);color:var(--text);text-transform:uppercase;
  letter-spacing:.04em;white-space:nowrap;margin-top:2px;
}
.k-recommendation,.k-open_loop{background:rgba(199,138,44,.12);color:var(--warn)}
.k-risk{background:rgba(166,71,71,.1);color:var(--danger)}
.k-pattern,.k-change{background:rgba(30,91,79,.1);color:var(--audio)}
.k-policy,.k-project_update,.k-relationship_update{background:rgba(184,115,51,.1);color:var(--vision)}
.detail{color:var(--mut);font-size:13px;margin-top:3px}
.ev{margin-top:7px;font-size:12px;color:var(--mut)}
.ev .evrow{padding:2px 0 2px 10px;border-left:2px solid rgba(184,115,51,.35);margin-top:2px}
.hgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.hcard{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:14px 16px;box-shadow:var(--shadow);animation:fadeUp .35s var(--ease) both;
}
.hlabel{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.hval{font-family:var(--display);font-size:1.65rem;font-weight:400;margin:8px 0 4px;letter-spacing:-.02em;color:var(--navy)}
.hsub{color:var(--mut);font-size:12px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pill{background:var(--panel-2);border:1px solid var(--line);border-radius:999px;padding:2px 9px;font-size:12px}
.dead-jobs{font-size:12px;color:var(--warn,#b45309);max-width:280px}
.dead-jobs summary{cursor:pointer;list-style:none;white-space:nowrap}
.dead-jobs-list{margin-top:6px;padding:8px 10px;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;max-height:180px;overflow:auto;font-size:11px;color:var(--ink,#1a1a1a);
  box-shadow:var(--shadow);position:absolute;z-index:40;min-width:260px}
.dead-jobs-list .dj{padding:4px 0;border-bottom:1px solid var(--line)}
.dead-jobs-list .dj:last-child{border-bottom:0}
.dead-jobs-list .dj .err{color:var(--mut);display:block;word-break:break-word}
@media(max-width:720px){
  #q{width:100%;order:5}
  .meta-bar{width:100%}
}
</style></head><body>
<div class="chrome">
  <div class="top">
    <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
    <span class="page-sub">Memory</span>
    @@NAV@@
    <input id="q" placeholder="search memories by meaning…">
    <span class="spacer"></span>
    <div class="meta-bar">
      <span id="jobs" title="background worker"></span>
      <details id="deadJobsBox" class="dead-jobs" style="display:none">
        <summary id="deadJobsSummary">Dead-letter</summary>
        <div id="deadJobsList" class="dead-jobs-list"></div>
      </details>
      <span id="stat"></span>
    </div>
    <button class="btn" id="rebuild" onclick="rebuild()" style="display:none">Rebuild turns</button>
    <button class="btn" id="reflectrun" onclick="runReflect()" style="display:none">Run reflection</button>
    <button class="btn" onclick="load()">Refresh</button>
  </div>
  @@APPROVAL@@
  <div class="chrome-tools" id="chromeTools">
    <div class="mode-seg mode-toggle">
      <span class="chip on" id="modeArchive" onclick="setLayer('archive')">Archive</span>
      <span class="chip" id="modeConst" onclick="setLayer('constellation')">Constellation</span>
    </div>
    <div class="tabs" id="archiveTabs">
      <span class="chip on" data-mod="" onclick="pickMod(this)">All</span>
      <span class="chip" data-mod="audio" onclick="pickMod(this)">Audio</span>
      <span class="chip" data-mod="vision" onclick="pickMod(this)">Vision</span>
      <span class="chip" data-mod="" data-source="desktop." onclick="pickMod(this)">Desktop</span>
      <span class="chip" id="actchip" onclick="pickActivity()">Activity</span>
      <span class="chip" id="turnchip" onclick="pickTurns()">Turns</span>
      <span class="chip" id="sesschip" onclick="pickSessions()">Sessions</span>
      <span class="chip" id="factchip" onclick="pickFacts()">Tasks</span>
      <span class="chip" id="reflectchip" onclick="pickReflect()">Reflection</span>
      <span class="chip" id="attnchip" onclick="pickAttention()">Attention</span>
      <span class="chip" id="egresschip" onclick="pickEgress()">Egress</span>
      <span class="chip" id="healthchip" onclick="pickHealth()">Audio Health</span>
      <span class="chip" id="learnchip" onclick="pickLearning()">Learning</span>
      <span class="chip" id="lowchip" onclick="toggleLow()">Low-confidence</span>
    </div>
  </div>
</div>
<div class="layout">
<div style="display:flex;flex-direction:column;min-height:0;min-width:0;flex:1">
<div id="list"><div class="empty">loading…</div></div>
<div id="constPane"><div id="horizonStrip" class="hsub" style="padding:8px 12px 0;gap:8px;flex-wrap:wrap"></div><div id="modeChips" class="hsub" style="padding:6px 12px 0;gap:6px"></div><div class="const-frame"><canvas id="memConst"></canvas></div></div>
</div>
<aside id="consoleAmbient"><h2>In the margin</h2><div id="ambientBox"></div></aside>
</div>
@@UI_JS@@
<script>
let mod="", src="", low=false, view="raw", timer=null, layer="archive", constCtl=null;
MnemosMemory.set('lastRoute','/memory');
(function restoreConsole(){
  const st=MnemosMemory.get('console',{});
  if(st.q) document.getElementById('q').value=st.q;
  if(st.layer==='constellation') layer='constellation';
  if(st.view) view=st.view;
  if(st.mod!=null) mod=st.mod;
  if(st.src!=null) src=st.src;
  if(st.low) low=!!st.low;
  try{
    const qp=new URLSearchParams(location.search);
    const m=qp.get('mode')||qp.get('layer');
    if(m==='constellation') layer='constellation';
    else if(m==='archive') layer='archive';
  }catch(e){}
})();
function persistConsole(){
  MnemosMemory.set('console',{q:document.getElementById('q').value,layer,view,mod,src,low,
    expanded:MnemosMemory.get('console.expanded',[])});
}
function setLayer(name){
  layer=name; persistConsole();
  document.getElementById('modeArchive').classList.toggle('on', layer==='archive');
  document.getElementById('modeConst').classList.toggle('on', layer==='constellation');
  document.getElementById('archiveTabs').style.display=layer==='archive'?'flex':'none';
  document.getElementById('list').style.display=layer==='archive'?'flex':'none';
  document.getElementById('constPane').classList.toggle('on', layer==='constellation');
  try{
    const u=new URL(location.href);
    if(layer==='constellation') u.searchParams.set('mode','constellation');
    else u.searchParams.delete('mode');
    history.replaceState(null,'',u.pathname+(u.search||'')+(u.hash||''));
  }catch(e){}
  if(layer==='constellation') loadConstellation();
}
let constVersion=null, constPoll=null, constStreamOn=false, constLoading=false;
async function constCheck(){
  if(layer!=='constellation') return;
  try{
    const v=(await (await fetch('/graph/version')).json()).version;
    if(constVersion!==null && v!==constVersion) await loadConstellation();
    constVersion=v;
  }catch(e){}
}
function renderHorizonStrip(data){
  const host=document.getElementById('horizonStrip');
  if(!host) return;
  const hz=(data&&data.horizon)||{};
  const items=hz.items||[];
  if(!items.length){
    host.innerHTML='<span class="mut" style="font-size:12px">Horizon quiet</span>';
    return;
  }
  host.innerHTML=items.map(it=>{
    const why=(it.reason&&it.reason[0])?it.reason[0]:'';
    return '<span class="pill" title="'+MnemosEsc(why)+'" style="cursor:pointer" data-hid="'
      +MnemosEsc(it.id||'')+'">'
      +'<b>in '+(it.when_label||'?')+'</b> · '+MnemosEsc(it.label||it.id||'')
      +' <span class="mut" style="margin-left:4px">×</span></span>';
  }).join('');
  host.querySelectorAll('[data-hid]').forEach(el=>{
    el.onclick=async()=>{
      const id=el.getAttribute('data-hid');
      if(!id) return;
      try{
        await fetch('/field/feedback',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id, outcome:'dismiss'})});
      }catch(e){}
      loadConstellation();
    };
  });
}
function renderModeChips(data){
  const host=document.getElementById('modeChips');
  if(!host) return;
  const cur=(data&&data.mode)||{};
  const modes=(data&&data.modes)||[];
  if(!modes.length){ host.innerHTML=''; return; }
  let cap=document.getElementById('modeCaption');
  if(!cap){
    cap=document.createElement('div');
    cap.id='modeCaption';
    cap.className='mode-caption';
    host.parentElement.insertBefore(cap, host.nextSibling);
  }
  const label=cur.label||(cur.id?String(cur.id):'Auto');
  const src=cur.source==='manual'?'':(cur.source?(' · '+cur.source):'');
  cap.textContent='Ranking for: '+label+src;
  host.innerHTML=modes.map(m=>{
    const on=m.id===cur.id;
    return '<button type="button" class="chip'+(on?' on':'')+'" data-mode="'+m.id+'" title="Reweights gravity for this context — does not filter">'
      +MnemosEsc(m.label||m.id)+'</button>';
  }).join('')
    +'<button type="button" class="chip'+(cur.source!=='manual'?' on':'')+'" data-mode="auto" title="Infer context from recent events">Auto</button>';
  host.querySelectorAll('[data-mode]').forEach(btn=>{
    btn.onclick=async()=>{
      try{
        await fetch('/field/mode',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({mode:btn.getAttribute('data-mode')})});
      }catch(e){}
      loadConstellation();
    };
  });
}
async function loadConstellation(){
  if(constLoading) return;
  constLoading=true;
  try{
  const data=await (await fetch('/field/state?limit=28')).json();
  try{
    const v=(await (await fetch('/graph/version')).json()).version;
    constVersion=v;
  }catch(e){}
  renderHorizonStrip(data);
  renderModeChips(data);
  if(!constStreamOn && window.MnemosFieldStream){
    constStreamOn=!!MnemosFieldStream.connect((d)=>{
      if(layer!=='constellation') return;
      if(d.version!=null) constVersion=d.version;
      loadConstellation();
    });
  }
  // Poll is fallback; slow down when SSE is live.
  if(!constPoll) constPoll=setInterval(constCheck, constStreamOn?20000:4000);
  if(constCtl){ constCtl.update(data); return; }
  constCtl=MnemosConstellation.mount(document.getElementById('memConst'), data, {
    persistKey:'console.constellation.cam',
    onSelect(node){
      if(!node) return;
      if(node.kind==='person'){
        const fav=new Set(MnemosMemory.get('favoritePeople',[])||[]);
        if(fav.has(node.id)) fav.delete(node.id); else fav.add(node.id);
        MnemosMemory.set('favoritePeople',[...fav]);
      }
    }
  });
  }finally{ constLoading=false; }
}
async function loadAmbient(){
  try{
    const intel=await (await fetch('/home/intelligence')).json();
    MnemosAmbient.render(document.getElementById('ambientBox'), intel.ambient||[], {
      constellation: constCtl,
    });
  }catch(e){
    MnemosAmbient.render(document.getElementById('ambientBox'), [{text:'Listening…'}]);
  }
}
async function revealProvenance(rowEl){
  const eid=rowEl && rowEl.dataset && rowEl.dataset.eventId;
  if(!eid) return;
  let host=rowEl.querySelector('.prov-host');
  if(!host){ host=document.createElement('div'); host.className='prov-host'; rowEl.appendChild(host); }
  if(host.dataset.open==='1'){ host.innerHTML=''; host.dataset.open='0'; return; }
  host.dataset.open='1';
  host.innerHTML='<div class="meta">Revealing…</div>';
  try{
    const j=await (await fetch('/console/provenance/'+eid)).json();
    const chain=j.chain||{};
    const audio=chain.enhanced_audio||chain.raw_audio||'';
    const corr=(chain.corrections||[]).map(c=>
      (c.stage||'')+((c.before||c.after)?(' “'+(c.before||'')+'” → “'+(c.after||'')+'”'):'')
      +(c.note?(' — '+c.note):'')).join('\n')||'none (verbatim as captured)';
    const steps=[
      {label:'Conversation', body:j.summary?JSON.stringify(j.summary):'utterance '+eid},
      {label:'Audio clip', html: audio ? ('<audio controls src="/artifact?path='+encodeURIComponent(audio)+'"></audio>') : '—'},
      {label:'Transcript', body:chain.transcript||'—'},
      {label:'Visual frame', body:'—'},
      {label:'Reasoning', body:corr},
      {label:'Confidence', body: (chain.capture_quality||'—')+(chain.snr_est!=null?(' · SNR '+chain.snr_est+'dB'):'')},
      {label:'Model used', body:chain.asr_prompt?('ASR bias applied'):'capture pipeline'},
      {label:'Timestamp', body:chain.captured_at?new Date(chain.captured_at*1000).toLocaleString():'—'},
      {label:'Source', body:j.rendered||chain.raw_audio||'—'},
    ];
    MnemosBleed.renderStack(host, steps);
    const exp=new Set(MnemosMemory.get('console.expanded',[])||[]);
    exp.add(String(eid)); MnemosMemory.set('console.expanded',[...exp]);
  }catch(e){
    host.innerHTML='<div class="meta">No provenance chain for this row.</div>';
  }
}
const q=document.getElementById('q'), list=document.getElementById('list');
function setViewUI(){
 document.getElementById('actchip').classList.toggle('on',view==="activity");
 document.getElementById('turnchip').classList.toggle('on',view==="turns");
 document.getElementById('sesschip').classList.toggle('on',view==="sessions");
 document.getElementById('factchip').classList.toggle('on',view==="facts");
 document.getElementById('reflectchip').classList.toggle('on',view==="reflect");
 document.getElementById('attnchip').classList.toggle('on',view==="attention");
 document.getElementById('egresschip').classList.toggle('on',view==="egress");
 document.getElementById('healthchip').classList.toggle('on',view==="health");
 document.getElementById('learnchip').classList.toggle('on',view==="learning");
 const rb=document.getElementById('rebuild');
 rb.style.display=(view==="turns"||view==="activity"||view==="sessions")?'inline-block':'none';
 rb.textContent=view==="activity"?'Rebuild activity':view==="sessions"?'Rebuild sessions':'Rebuild turns';
 document.getElementById('reflectrun').style.display=view==="reflect"?'inline-block':'none';
 q.style.display=(view==="raw")?'inline-block':'none';
}
function pickMod(el){view="raw";mod=el.dataset.mod;src=el.dataset.source||"";setViewUI();
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.toggle('on',c===el));load();}
function pickTurns(){view="turns";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));setViewUI();load();}
function pickActivity(){view="activity";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));setViewUI();load();}
function pickSessions(){view="sessions";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));setViewUI();load();}
function pickFacts(){view="facts";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));setViewUI();load();}
function pickReflect(){view="reflect";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));setViewUI();load();}
function pickAttention(){view="attention";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));
 document.getElementById('attnchip').classList.add('on');setViewUI();load();}
function pickEgress(){view="egress";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));
 document.getElementById('egresschip').classList.add('on');setViewUI();load();}
function pickHealth(){view="health";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));
 document.getElementById('healthchip').classList.add('on');setViewUI();load();}
function pickLearning(){view="learning";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));
 document.getElementById('learnchip').classList.add('on');setViewUI();load();}
function toggleLow(){low=!low;document.getElementById('lowchip').classList.toggle('on',low);
 if(view!=="raw"){view="raw";setViewUI();}load();}
async function rebuild(){document.getElementById('stat').textContent='rebuilding…';
 const ep=view==="activity"?'/console/activity/rebuild'
   :view==="sessions"?'/console/sessions/rebuild':'/console/consolidate';
 await fetch(ep,{method:'POST'});load();}
async function factAction(fact_id,verb){
 await fetch('/facts/'+fact_id+'/'+verb,{method:'POST'});
 loadFacts();
}
async function factEdit(fact_id,current){
 const t=prompt('Edit this fact:',current); if(t==null) return;
 await fetch('/facts/'+fact_id+'/edit',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({text:t})});
 loadFacts();
}
function fmtTime(t){if(!t)return'';const d=new Date(t*1000);
 return d.toLocaleDateString([], {month:'short',day:'numeric'})+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function conf(c){return (c==null)?'':('conf '+Number(c).toFixed(2));}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function art(p){return '/artifact?path='+encodeURIComponent(p);}
function row(e){
 // Desktop-capture rows span two modalities (screen=vision, click=input) —
 // badge them by source so the Desktop tab (and the All view) reads at a glance.
 const desk=(e.source||'').startsWith('desktop.');
 const cls=desk?'b-desktop':(e.modality==='audio')?'b-audio':(e.modality==='vision')?'b-vision':'b-other';
 const label=desk?(e.modality==='input'?'click':'screen'):(e.modality||'?');
 let media='';
 if(e.audio_path) media='<audio controls preload="none" src="'+art(e.audio_path)+'"></audio>';
 if(e.enhanced_audio) media+='<audio controls preload="none" title="enhanced (what Whisper heard)" src="'+art(e.enhanced_audio)+'"></audio>';
 if(e.frame_path) media+='<img class="thumb" src="'+art(e.frame_path)+'" onclick="this.classList.toggle(\'big\')">';
 const bits=[];
 if(e.speaker) bits.push('<span class="spk">'+esc(e.speaker)+(e.speaker_profile?(' · '+esc(e.speaker_profile)):'')+'</span>');
 if(desk&&e.window) bits.push('<span class="spk" title="'+esc(e.window)+'">'
   +esc(e.window.length>48?e.window.slice(0,45)+'…':e.window)+'</span>');
 bits.push(fmtTime(e.time));
 if(e.confidence!=null) bits.push(conf(e.confidence));
 if(e.score!=null) bits.push('match '+Number(e.score).toFixed(2));
 if(e.utterance_type) bits.push('<span class="spk">'+(e.utterance_type==='command'?'⌘ command':'✎ dictation')+'</span>');
 if(e.vision_provider) bits.push('<span class="pill" title="'+esc(e.vision_route||'')+'">'+esc(e.vision_provider)+'</span>');
 if(e.provenance&&e.provenance.n_corrections) bits.push('<span class="spk" title="'+esc(e.provenance_detail||'')+'">🔗 '+e.provenance.n_corrections+' fix'+(e.provenance.n_corrections!=1?'es':'')+'</span>');
 if(e.needs_review) bits.push('<span class="lowtag">⚠ needs review</span>');
 if(e.skipped) bits.push('<span class="lowtag">audio-only ('+esc(e.skipped)+')</span>');
 if(e.low_confidence&&!e.needs_review) bits.push('<span class="lowtag">⚠ low ('+esc(e.quality_reason||'')+')</span>');
 return '<div class="row'+(e.low_confidence||e.skipped?' low':'')+'" data-event-id="'+(e.id||'')+'">'
  +'<span class="badge '+cls+'">'+esc(label)+'</span>'
  +'<div class="body"><div class="text">'+esc(e.text||'(empty)')+'</div>'
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'+media
  +'<div class="prov-host"></div></div></div>';
}
function bindBleedRows(){
  list.querySelectorAll('.row[data-event-id]').forEach(el=>{
    MnemosBleed.bind(el, revealProvenance);
    const exp=MnemosMemory.get('console.expanded',[])||[];
    if(exp.includes(String(el.dataset.eventId))) revealProvenance(el);
  });
}
function fmtDur(s){if(s==null)return'';s=Math.round(s);
 return s>=3600?(Math.floor(s/3600)+'h '+Math.round((s%3600)/60)+'m')
   :s>=60?(Math.floor(s/60)+'m '+(s%60)+'s'):(s+'s');}
function endTime(t){return t?new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'';}
// One "what was I doing?" block: app + focus windows + fold counts + summary,
// expandable to the underlying screen/click events (thumbs included).
function actRow(a){
 const bits=[fmtTime(a.start)+(a.end?(' → '+endTime(a.end)):'')];
 if(a.duration_s!=null) bits.push(fmtDur(a.duration_s));
 const counts=[];
 if(a.n_screens!=null) counts.push(a.n_screens+' screen'+(a.n_screens===1?'':'s'));
 if(a.n_clicks!=null) counts.push(a.n_clicks+' click'+(a.n_clicks===1?'':'s'));
 // Optional multimodal counts (Part 2 contract) — render only if present.
 if(a.n_audio!=null) counts.push(a.n_audio+' audio');
 if(a.n_webcam!=null) counts.push(a.n_webcam+' webcam');
 if(counts.length) bits.push(counts.join(' · '));
 if(a.modalities&&a.modalities.length) bits.push('<span class="spk">'+a.modalities.map(esc).join(' + ')+'</span>');
 const wins=(a.windows||[]).map(w=>'<span class="pill" title="'+esc(w)+'">'
   +esc(w.length>64?w.slice(0,61)+'…':w)+'</span>').join('');
 const ids=(a.event_ids||[]);
 const expand=ids.length?'<div class="acts"><button class="mini" onclick="actExpand(this,\''+ids.join(',')+'\')">▸ '
   +ids.length+' linked event'+(ids.length===1?'':'s')+'</button></div><div class="actev"></div>':'';
 return '<div class="row"><span class="badge b-desktop">'+esc(a.app||'desktop')+'</span>'
  +'<div class="body"><div class="text">'+esc(a.summary||'(no summary)')+'</div>'
  +(wins?'<div class="meta">'+wins+'</div>':'')
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'+expand+'</div></div>';
}
async function actExpand(btn,ids){
 const box=btn.parentElement.nextElementSibling;
 if(box.dataset.open==='1'){box.dataset.open='0';box.innerHTML='';
   btn.innerHTML=btn.innerHTML.replace('▾','▸');return;}
 box.dataset.open='1';btn.innerHTML=btn.innerHTML.replace('▸','▾');
 box.innerHTML='<div class="empty" style="margin:12px">loading…</div>';
 try{
  const j=await (await fetch('/console/activity/events?ids='+ids)).json();
  box.innerHTML=j.events.length?j.events.map(row).join('')
    :'<div class="empty" style="margin:12px">no linked events found.</div>';
 }catch(e){ box.innerHTML='<div class="empty" style="margin:12px">error: '+e+'</div>'; }
}
async function loadActivity(){
 try{
  const j=await (await fetch('/console/activity?limit=200')).json();
  const acts=j.activities||[];
  document.getElementById('stat').textContent=j.count+' activit'+(j.count===1?'y':'ies');
  list.innerHTML=acts.length?acts.map(actRow).join('')
    :'<div class="empty">no activity blocks yet — desktop capture folds them as you work.<br>Click “Rebuild activity” to fold existing desktop events.</div>';
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
function sessRow(s){
 const bits=[];
 if(s.speakers&&s.speakers.length) bits.push('<span class="spk">'+s.speakers.map(esc).join(', ')+'</span>');
 bits.push(fmtTime(s.start)+(s.end?(' → '+endTime(s.end)):''));
 if(s.duration_s!=null) bits.push(fmtDur(s.duration_s));
 if(s.n_turns!=null) bits.push(s.n_turns+' turn'+(s.n_turns===1?'':'s')
   +(s.n_utterances!=null?(' · '+s.n_utterances+' utterance'+(s.n_utterances===1?'':'s')):''));
 return '<div class="row"><span class="badge b-audio">session</span>'
  +'<div class="body"><div class="text">'+esc(s.text||'(empty)')+'</div>'
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div></div></div>';
}
async function loadSessions(){
 try{
  const j=await (await fetch('/console/sessions?limit=200')).json();
  const rows=j.sessions||[];
  document.getElementById('stat').textContent=j.count+' session'+(j.count===1?'':'s');
  list.innerHTML=rows.length?rows.map(sessRow).join('')
    :'<div class="empty">no sessions yet — click “Rebuild sessions”.</div>';
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
function turnRow(t){
 const clips=(t.audio_paths||[]).map(p=>'<audio controls preload="none" src="'+art(p)+'"></audio>').join('');
 const range=fmtTime(t.start)+(t.n_utterances>1?(' → '+new Date(t.end*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})):'');
 const bits=[];
 if(t.speaker) bits.push('<span class="spk">'+esc(t.speaker)+'</span>');
 bits.push(range);
 bits.push(t.n_utterances+' utterance'+(t.n_utterances===1?'':'s'));
 if(t.duration_s) bits.push(t.duration_s+'s');
 return '<div class="row"><span class="badge b-audio">turn</span>'
  +'<div class="body"><div class="text">'+esc(t.text||'(empty)')+'</div>'
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'+clips+'</div></div>';
}
async function loadTurns(){
 try{
  const r=await fetch('/console/turns?limit=300'); const j=await r.json();
  document.getElementById('stat').textContent=j.count+' turns';
  list.innerHTML = j.turns.length ? j.turns.map(turnRow).join('')
    : '<div class="empty">no turns yet — click “Rebuild turns”.</div>';
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
function factRow(f){
 const kind=f.kind;
 const parties = kind==='commitment'
   ? [f.from_person, f.to_person&&('→ '+f.to_person)].filter(Boolean).join(' ')
   : (f.owner||'');
 const bits=[];
 if(parties) bits.push('<span class="spk">'+esc(parties)+'</span>');
 if(f.due) bits.push('due '+esc(f.due));
 if(f.confidence!=null) bits.push('conf '+Number(f.confidence).toFixed(2));
 if(f.source) bits.push(esc(f.source));
 if(f.review) bits.push('<span class="rev">'+esc(f.review)+'</span>');
 const play = f.play_path || f.enhanced_audio || f.source_audio;
 const aid = 'fact-audio-'+f.fact_id;
 let transcriptHtml = '';
 if(f.span_highlight && f.span_highlight.match!=null){
  const hl=f.span_highlight;
  transcriptHtml = '<div class="span-transcript">'
   +esc(hl.before||'')+'<mark class="span-hl">'+esc(hl.match||'')+'</mark>'
   +esc(hl.after||'')+'</div>';
 } else if(f.source_span){
  transcriptHtml = '“'+esc(f.source_span)+'”';
 }
 const clip = play
  ? ('<button type="button" class="play-moment" onclick="playFactMoment(\''+aid+'\')">Play the moment</button>'
     +'<audio id="'+aid+'" controls preload="none" src="'+art(play)+'"></audio>')
  : '';
 const prov = (transcriptHtml||clip) ? '<div class="prov">'+transcriptHtml+clip+'</div>' : '';
 const badge = kind==='commitment' ? 'b-vision' : 'b-audio';
 const t=(f.text||'').replace(/'/g,"\\'");
 return '<div class="row"><span class="badge '+badge+'">'+esc(kind)+'</span>'
  +'<div class="body"><div class="text">'+esc(f.text||'')+'</div>'
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'+prov
  +'<div class="acts">'
  +'<button class="mini done" onclick="factAction('+f.fact_id+',\'approve\')">✓ Approve</button>'
  +'<button class="mini done" onclick="factAction('+f.fact_id+',\'done\')">● Done</button>'
  +'<button class="mini" onclick="factEdit('+f.fact_id+',\''+t+'\')">✎ Edit</button>'
  +'<button class="mini drop" onclick="factAction('+f.fact_id+',\'dismiss\')">✕ Dismiss</button>'
  +'</div></div></div>';
}
function playFactMoment(aid){
 const audio=document.getElementById(aid);
 if(!audio) return;
 try{ audio.play(); }catch(e){}
 audio.scrollIntoView({block:'nearest'});
}
async function loadFacts(){
 try{
  // The review queue: open facts not yet dismissed. Newest first.
  const j=await (await fetch('/facts?status=open&limit=300')).json();
  const facts=j.facts||[];
  const tasks=facts.filter(f=>f.kind==='task');
  const comms=facts.filter(f=>f.kind==='commitment');
  document.getElementById('stat').textContent=
    tasks.length+' tasks · '+comms.length+' commitments';
  const sec=(title,arr)=> arr.length
    ? '<div class="sechead">'+title+'</div>'+arr.map(factRow).join('') : '';
  const html=sec('Tasks',tasks)+sec('Commitments',comms);
  list.innerHTML = html || '<div class="empty">no open tasks or commitments yet — '
    +'they appear here as @@BRAND@@ extracts them from conversation.</div>';
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
function reflItem(it){
 const bits=[];
 if(it.subject) bits.push('<span class="spk">'+esc(it.subject)+'</span>');
 if(it.confidence!=null) bits.push('conf '+Number(it.confidence).toFixed(2));
 if(it.review) bits.push('<span class="rev">'+esc(it.review)+'</span>');
 if(it.converted_fact_id) bits.push('→ task #'+it.converted_fact_id);
 const detail = it.detail ? '<div class="detail">'+esc(it.detail)+'</div>' : '';
 const ev = (it.evidence&&it.evidence.length)
   ? '<div class="ev">evidence:'+it.evidence.map(e=>'<div class="evrow">['+e.fact_id+'] '
       +esc(e.text)+(e.source?(' · '+esc(e.source)):'')+'</div>').join('')+'</div>' : '';
 const t=(it.text||'').replace(/'/g,"\\'");
 const conv = it.converted_fact_id ? ''
   : '<button class="mini done" onclick="itemConvert('+it.id+')">→ Task</button>';
 return '<div class="row"><span class="kind k-'+esc(it.kind)+'">'+esc(it.kind)+'</span>'
  +'<div class="body"><div class="text">'+esc(it.text||'')+'</div>'+detail
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'+ev
  +'<div class="acts">'
  +'<button class="mini done" onclick="itemAction('+it.id+',\'approve\')">✓ Approve</button>'
  +'<button class="mini" onclick="itemEdit('+it.id+',\''+t+'\')">✎ Edit</button>'+conv
  +'<button class="mini drop" onclick="itemAction('+it.id+',\'dismiss\')">✕ Dismiss</button>'
  +'</div></div></div>';
}
async function loadReflect(){
 try{
  const j=await (await fetch('/reflections?scope=daily')).json();
  const r=j.reflection;
  if(!r){ document.getElementById('stat').textContent='no reflections';
    list.innerHTML='<div class="empty">no reflection yet — click “Run reflection”.</div>'; return; }
  const when=fmtTime(r.created_at);
  const head='<div class="refl"><div class="sechead">Daily reflection · '+esc(when)
    +(r.confidence!=null?(' · conf '+Number(r.confidence).toFixed(2)):'')+'</div>'
    +'<div class="sum">'+esc(r.summary||'(no summary)')+'</div></div>';
  const items=r.items||[];
  document.getElementById('stat').textContent=items.length+' insight'+(items.length===1?'':'s');
  list.innerHTML=head+(items.length?items.map(reflItem).join('')
    :'<div class="empty">no insights in this reflection.</div>');
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
async function itemAction(id,verb){
 await fetch('/reflection_items/'+id+'/'+verb,{method:'POST'}); loadReflect();
}
// --- Learning tab: what Mnemos harvested from your verdicts (Workstream A) --
function learnRow(p){
 const bits=[fmtTime(p.created_at)];
 bits.push('<span class="spk">'+esc(p.verdict)+'</span>');
 bits.push(esc(p.verdict_source||''));
 if(p.model_tag) bits.push(esc(p.model_tag));
 if(!p.human_confirmed) bits.push('<span class="lowtag">unconfirmed (shadow)</span>');
 if(!p.shadow_eligible) bits.push('<span class="pill" title="personal-classed — never sent to cloud shadow eval">local-only</span>');
 const target=p.final_target?'<div class="detail">→ '+esc(p.final_target)+'</div>':'';
 const confirm=(!p.human_confirmed)
   ?'<button class="mini done" onclick="learnConfirm(\''+p.id+'\')">✓ Confirm</button>':'';
 return '<div class="row"><span class="kind">'+esc(p.task_type)+'</span>'
  +'<div class="body"><div class="text">'+esc(p.input_text||'(empty)')+'</div>'+target
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'
  +'<div class="acts">'+confirm
  +'<button class="mini drop" onclick="learnDelete(\''+p.id+'\')">✕ Delete</button>'
  +'</div></div></div>';
}
function exemplarRow(x){
 const bits=[fmtTime(x.created_at),'<span class="spk">'+esc(x.quality_tier||'')+'</span>',
   'used '+(x.use_count||0)+'×'];
 return '<div class="row"><span class="kind">'+esc(x.task_type)+'</span>'
  +'<div class="body"><div class="text">'+esc((x.input_text||'').slice(0,240))+'</div>'
  +'<div class="detail">→ '+esc((x.target_text||'').slice(0,240))+'</div>'
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'
  +'<div class="acts"><button class="mini drop" onclick="exemplarDelete(\''+x.exemplar_id+'\')">✕ Delete</button></div>'
  +'</div></div>';
}
async function loadLearning(){
 try{
  const [sj,pj,ej,shj]=await Promise.all([
    (await fetch('/learning/stats')).json(),
    (await fetch('/learning/pairs?limit=200')).json(),
    (await fetch('/learning/exemplars?limit=100')).json(),
    (await fetch('/learning/shadow')).json()]);
  const wk=Object.values(sj.week||{}).reduce((a,t)=>a+(t.total||0),0);
  const tot=Object.values(sj.total||{}).reduce((a,t)=>a+(t.total||0),0);
  const es=ej.stats||{};
  document.getElementById('stat').textContent=wk+' this week · '+tot+' total · '
    +(es.count||0)+' exemplar'+((es.count||0)===1?'':'s');
  const types={};
  (pj.pairs||[]).forEach(p=>{(types[p.task_type]=types[p.task_type]||[]).push(p);});
  const cards='<div class="hgrid">'+Object.entries(sj.total||{}).map(([k,v])=>
    '<div class="hcard"><div class="hlabel">'+esc(k)+'</div><div class="hval">'+(v.total||0)
    +'</div><div class="hsub">'+((sj.week||{})[k]?((sj.week[k].total||0)+' this week'):'quiet this week')
    +'</div></div>').join('')+'</div>';
  const allOff=!!((es.gates||{})._all||{}).off;
  const killBtn='<div class="acts" style="margin:4px 0 8px">'
    +'<button class="mini'+(allOff?' done':' drop')+'" onclick="exemplarKill('+(!allOff)+')">'
    +(allOff?'▶ Re-enable exemplar injection':'⏸ Pause exemplar injection')+'</button>'
    +(es.enabled?'':'<span class="lowtag" style="margin-left:8px">QUILL_EXEMPLARS=0 (store off)</span>')
    +'</div>';
  const exSec=(ej.rows&&ej.rows.length)
    ?'<div class="sechead">What @@BRAND@@ has learned (exemplars)</div>'+killBtn
      +ej.rows.map(exemplarRow).join('')
    :(es.enabled?'<div class="sechead">Exemplars</div>'+killBtn
      +'<div class="empty">No exemplars yet — 👍 or ✏️ verdicts mint them.</div>':'');
  let shSec='';
  if(shj&&shj.enabled){
    const at=shj.agreement_by_task||{};
    const cards2=Object.entries(at).map(([k,v])=>
      '<div class="hcard"><div class="hlabel">shadow · '+esc(k)+'</div>'
      +'<div class="hval">'+(v.agree_rate==null?'—':Math.round(v.agree_rate*100)+'%')+'</div>'
      +'<div class="hsub">agree rate · '+v.graded+' graded</div></div>').join('');
    const reasons=(shj.top_reason_codes||[]).map(r=>'<span class="pill">'+esc(r[0])+' ×'+r[1]+'</span>').join(' ');
    shSec='<div class="sechead">Shadow evaluation (last '+shj.window_days+' day'+(shj.window_days===1?'':'s')+')</div>'
      +(cards2?'<div class="hgrid">'+cards2+'</div>':'<div class="empty">No shadow grades yet — runs while the machine is idle.</div>')
      +(reasons?'<div class="hsub" style="margin:8px 2px">'+reasons+' · '+(shj.tokens_spent||0)+' tokens spent</div>':'');
  }
  const rows=Object.entries(types).map(([k,arr])=>
    '<div class="sechead">'+esc(k)+'</div>'+arr.map(learnRow).join('')).join('');
  list.innerHTML=(tot?cards:'')+shSec+exSec+(rows||'<div class="empty">Nothing harvested yet — '
    +'approve, edit, or dismiss anything (tasks, chat answers, insights) and it lands here. '
    +'Every row is yours to delete.</div>');
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
async function exemplarDelete(id){
 await fetch('/learning/exemplars/'+id,{method:'DELETE'}); loadLearning();
}
async function exemplarKill(off){
 await fetch('/learning/exemplars/gate',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({task_type:'_all',off:off})}); loadLearning();
}
async function learnDelete(id){
 await fetch('/learning/pairs/'+id,{method:'DELETE'}); loadLearning();
}
async function learnConfirm(id){
 await fetch('/learning/pairs/'+id+'/confirm',{method:'POST'}); loadLearning();
}
async function itemEdit(id,current){
 const t=prompt('Edit this insight:',current); if(t==null) return;
 await fetch('/reflection_items/'+id+'/edit',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})}); loadReflect();
}
async function itemConvert(id){
 await fetch('/reflection_items/'+id+'/convert',{method:'POST'}); loadReflect();
}
async function runReflect(){
 document.getElementById('stat').textContent='reflecting…';
 try{ await fetch('/reflect/run?scope=daily',{method:'POST'}); }catch(e){}
 loadReflect();
}
function hcard(label,val,sub){
 return '<div class="hcard"><div class="hlabel">'+label+'</div>'
  +'<div class="hval">'+val+'</div>'+(sub?'<div class="hsub">'+sub+'</div>':'')+'</div>';
}
function killSwitchPanel(rows){
  if(!rows||!rows.length) return '';
  const items=rows.map(s=>{
    const on=!!s.on;
    const nd=s.non_default?' · non-default':'';
    return '<label style="display:flex;align-items:center;justify-content:space-between;'
      +'gap:12px;padding:8px 0;border-top:1px solid var(--line);font-size:13px">'
      +'<span><b style="color:var(--navy)">'+esc(s.label||s.env)+'</b>'
      +'<span class="mut" style="display:block;font:11px var(--mono)">'
      +esc(s.env)+(on?' · ON':' · off')+nd+'</span></span>'
      +'<input type="checkbox" '+(on?'checked':'')
      +' onchange="toggleKillSwitch(\''+esc(s.env)+'\',this.checked)"></label>';
  }).join('');
  return '<div class="refl" style="margin-top:14px"><div class="sechead">Kill switches</div>'
    +'<div class="sum" style="margin-bottom:6px">Behavior gates — flip without restart. '
    +'Persisted to data/kill_switches.json.</div>'+items+'</div>';
}
async function toggleKillSwitch(env,on){
  document.getElementById('stat').textContent='updating '+env+'…';
  try{
    await fetch('/console/hardening/kill-switch',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({env:env,on:!!on})});
  }catch(e){}
  loadAttention();
}
async function loadEgress(){
  try{
  const eg=await (await fetch('/console/egress?recent=40')).json();
  let models={};
  try{ models=await (await fetch('/console/models')).json(); }catch(e){}
  const by=eg.by_class||{};
  const sess=(eg.session||{});
  const sessBy=sess.by_class||{};
  const order=['public','internal','personal','sensitive','never-send'];
  const pill=(cls,n)=>'<span class="pill">'+esc(cls)+' '+n+'</span>';
  const classPills=order.filter(c=>by[c]).map(c=>pill(c,by[c])).join('')
    || '<span class="mut">no cloud calls with privacy_max yet</span>';
  const sessPills=order.filter(c=>sessBy[c]).map(c=>pill(c,sessBy[c])).join('')
    || '<span class="mut">—</span>';
  const rows=(eg.recent||[]).map(r=>{
    const when=r.time?new Date(r.time*1000).toLocaleString():'—';
    const act=r.privacy_action||(r.ok===false&&r.privacy_max==='never-send'?'refuse':'');
    const badge=act==='refuse'
      ? '<span class="pill" style="color:var(--danger,#a33)">refused</span>'
      : (act==='redact'?'<span class="pill">redacted</span>':'');
    return '<div class="row bleed" style="padding:10px 0;border-top:1px solid var(--line)">'
      +'<div class="t"><b style="color:var(--navy)">'+esc(r.privacy_max||'—')+'</b>'
      +' · '+esc(r.task||'')+' · '+esc(r.provider||'')+'/'+esc(r.model||'')
      +'<div class="meta">'+esc(when)
      +(r.input_tokens!=null?(' · '+r.input_tokens+' in'):'')
      +(r.cost_usd!=null?(' · $'+Number(r.cost_usd).toFixed(4)):'')
      +' '+badge+'</div></div></div>';
  }).join('') || '<div class="empty">No external calls recorded yet.</div>';
  document.getElementById('stat').textContent=
    (eg.max_seen?('max '+eg.max_seen+' · '):'')
    +(eg.refused||0)+' refused · '+(Object.values(by).reduce((a,b)=>a+b,0))+' cloud w/ class';
  const priv=(models.privacy||{});
  list.innerHTML='<div class="refl" style="margin-bottom:12px">'
    +'<div class="sechead">What left the machine</div>'
    +'<div class="sum">Highest privacy_class on each external model call '
    +'(plan 6.2). never-send is refused before bytes leave; sensitive/personal '
    +'are redacted.</div></div>'
    +'<div class="hgrid">'
    +hcard('Trail max', eg.max_seen||'—', classPills)
    +hcard('Refused', String(eg.refused||0), 'never-send blocked at gate')
    +hcard('Session max', sess.max_seen||priv.max_seen||'—', sessPills)
    +hcard('Session cloud', String(sess.cloud_calls||priv.cloud_calls||0),
           (sess.refused||priv.refused||0)+' refused this process')
    +'</div>'
    +'<div class="sechead" style="margin-top:18px">Recent egress</div>'
    +rows;
  }catch(e){ list.innerHTML='<div class="empty">error loading egress: '+e+'</div>'; }
}
async function loadHealth(){
  try{
  const h=await (await fetch('/console/audio-health')).json();
  let cog={metrics:{}};
  try{ cog=await (await fetch('/console/cognition')).json(); }catch(e){}
  const mins=Math.round((h.window_s||3600)/60);
  document.getElementById('stat').textContent=h.utterances+' utterances · last '+mins+'m';
  const pct=(x)=> x==null?'—':((Math.round(x*1000)/10)+'%');
  const lat=(o)=> (o&&o.avg!=null)
    ? (o.avg+'ms avg'+(o.p95!=null?(' · '+o.p95+'ms p95'):'')+(o.max!=null?(' · '+o.max+'ms max'):''))
    : '—';
  const ph=h.per_hour||{};
  const drops=h.drops_by_reason||{};
  const dropList=Object.keys(drops).length
    ? Object.keys(drops).map(k=>'<span class="pill">'+esc(k)+' '+drops[k]+'</span>').join('')
    : '<span class="mut">none</span>';
  const q=h.quality_dist||{};
  list.innerHTML='<div class="hgrid">'
   +hcard('Utterances / hr', (ph.utterances!=null?ph.utterances:'—'),
          h.kept+' kept · '+h.dropped+' dropped')
   +hcard('Dropped / hr', (ph.dropped!=null?ph.dropped:'—'), dropList)
   +hcard('ASR latency', lat(h.asr_latency_ms), 'Whisper transcribe wall-time')
   +hcard('End-to-end', lat(h.total_latency_ms), 'speech-end → published')
   +hcard('Quality mix',
          'good '+(q.good||0)+' · noisy '+(q.noisy||0)+' · bad '+(q.bad||0),
          'avg SNR '+(h.avg_snr!=null?h.avg_snr+'dB':'—')
            +' · clip '+(h.avg_clipping!=null?h.avg_clipping+'%':'—'))
   +hcard('Low-confidence', pct(h.low_confidence_rate), 'of kept transcripts')
   +hcard('Speaker unknown', pct(h.speaker_unknown_rate), 'of attributed utterances')
   +offerCards(cog.metrics||{})
   +'</div>';
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
async function loadAttention(){
  try{
  const a=await (await fetch('/console/attention?days=7')).json();
  const pct=(x)=> x==null?'—':((Math.round(x*1000)/10)+'%');
  const f=a.fulfillment||{};
  const c=a.corpus||{};
  const surf=a.by_surface||{};
  const a1=a.a1||{};
  const a2=a.a2||{};
  const a3=a.a3||{};
  const a4=a.a4||{};
  const tr=a1.traces||{};
  const rp=a1.replay||{};
  const feeder=a2.feeder||{};
  const learn=a4.learn||{};
  const hz=a4.horizon||{};
  const meta=a4.meta||{};
  const promo=a4.promote||{};
  const lastPromo=promo.last||{};
  const rzn=a4.reasoners||{};
  const lastRzn=(rzn.last||{});
  const eco=a.c||{};
  const ecoLc=(eco.lifecycle&&eco.lifecycle.counts)||{};
  const ecoLance=eco.lance||{};
  const ecoForgot=(eco.forgotten_this_month||[]);
  const fTrk=a.f||{};
  const fPred=fTrk.predictors||{};
  const fHard=fTrk.hardening||{};
  const fTasks=fPred.tasks||{};
  const fApp=(fTasks.next_app&&fTasks.next_app.active)||{};
  const fBat=fHard.battery||null;
  const fNonDef=(fHard.non_default||[]);
  document.getElementById('stat').textContent=
    (a.field_impressions||0)+' field impressions · '+(a.misses||0)+' misses · last '+(a.days||7)+'d';
  const kindPills=Object.keys(c.by_kind||{}).map(k=>
    '<span class="pill">'+esc(k)+' '+(c.by_kind[k])+'</span>').join('') || '<span class="mut">—</span>';
  const nudge=a.self_report_due
    ? '<div class="refl" style="margin-bottom:12px"><div class="sechead">Weekly check-in due</div>'
      +'<div class="sum">Cognitive load + trust — <a href="/selfreport">open self-report</a>'
      +(a.self_report_last_ts?(' · last '
        +new Date(a.self_report_last_ts*1000).toLocaleDateString()):' · never filed')
      +'</div></div>'
    : '';
  const corpusOk=c.ok?'frozen ✓':'needs attention';
  const gateLabel=rp.status==null?'not run yet'
    :(rp.status==='pass'?'PASS τ='+(rp.mean_tau!=null?rp.mean_tau:'—')
      :(rp.status==='fail'?'FAIL τ='+(rp.mean_tau!=null?rp.mean_tau:'—')
        :'insufficient data'));
  const seedPills=(feeder.top_seeds||[]).map(s=>
    '<span class="pill">'+esc(s.id)+' '+(s.weight)+'</span>').join('') || '<span class="mut">none</span>';
  list.innerHTML=nudge+'<div class="hgrid">'
   +hcard('Field engagement', pct(a.field_engagement_rate),
          (a.field_engaged||0)+' of '+(a.field_impressions||0)+' closed')
   +hcard('Misses', String(a.misses||0),
          'chat asked about a node absent from field')
   +hcard('Offers', String(a.offers||surf.offer||0),
          'accept-rate '+pct(a.offer_accept_rate)
          +' · accepted '+(a.offer_accepted||0)
          +' · dismissed '+(a.offer_dismissed||0))
   +hcard('Surfaces',
          'field '+(surf.field||0)+' · ground '+(surf.grounding||0)
          +' · offer '+(surf.offer||0),
          'reaction '+(surf.reaction||0))
   +hcard('Fulfillment', pct(f.fulfillment_rate),
          (f.counts&&f.counts.done||0)+' done · '
          +(f.counts&&f.counts.cancelled||0)+' dropped · '
          +(f.overdue_open||0)+' overdue open'
          +(f.fulfillment_delta!=null
            ?(' · Δ '+(f.fulfillment_delta>=0?'+':'')+f.fulfillment_delta+' vs baseline')
            :(f.baseline?' · baseline set':' · no baseline')))
   +hcard('On-time', pct(f.on_time_rate),
          'median open age '+(f.median_open_age_days!=null?f.median_open_age_days+'d':'—'))
   +hcard('Golden corpus', String(c.n||0)+' cases',
          corpusOk+' · '+kindPills)
   +hcard('Traces (A1)', String(tr.total||0),
          'person '+(tr.person||0)+' · entity '+(tr.entity||0)
          +' · fact '+(tr.fact||0))
   +hcard('Replay gate', gateLabel,
          'threshold '+(a1.gate!=null?a1.gate:0.6)
          +' · renders '+(rp.renders!=null?rp.renders:'—')
          +(a1.due?' · due':''))
   +hcard('Field v2 (A2)', a2.field_v2?'ON':'off',
          (a2.field_v2?'ranking by traces+activation':'gravity ranks; shadow logged')
          +' · edges '+(a2.conductive_edges!=null?a2.conductive_edges:0))
   +hcard('Now-Context', String(feeder.seed_count||0)+' seeds',
          (feeder.attached?'feeder live':'feeder idle')
          +' · gen '+(feeder.generation!=null?feeder.generation:0)
          +' · '+seedPills)
   +hcard('Working Memory (A3)', a3.enabled===false?'off':(String(a3.n_slots||0)+' slots'),
          (a3.enabled===false?'QUILL_WM=0 — quota path'
            :(a3.selection&&a3.selection.fallback
              ?('FALLBACK · '+(a3.selection.reason||'quota'))
              :'MMR + hysteresis'))
          +' · γ '+(a3.gamma!=null?a3.gamma:0.35)
          +((a3.mode&&a3.mode.label)?(' · mode '+a3.mode.label):''))
   +hcard('Horizon (A4)', String((hz.items||[]).length)+' items',
          (hz.enabled===false?'off':('min_p '+(hz.min_p!=null?hz.min_p:0.5)))
          +((hz.items&&hz.items[0])
            ?(' · '+esc((hz.items[0].when_label||'')+' '+(hz.items[0].label||'')))
            :' · none yet'))
   +hcard('Learning β', learn.learn_enabled?'ON':'off (kill switch)',
          'updates '+(learn.n_updates!=null?learn.n_updates:0)
          +' · drift '+(learn.drift!=null?learn.drift:0)
          +' · day '+(learn.day_drift!=null?Number(learn.day_drift).toFixed(4):'0'))
   +hcard('β promote', lastPromo.status||(promo.due?'due':'—'),
          (lastPromo.reason||'')
          +(lastPromo.cand_acc!=null?(' · cand '+lastPromo.cand_acc):'')
          +(lastPromo.prior_acc!=null?(' vs prior '+lastPromo.prior_acc):''))
   +hcard('Meta-memory', String(meta.at_risk||0)+' at-risk',
          'stale '+(meta.stale||0)+' · forget '+(meta.forget||0)
          +' · dropped '+(meta.dropped||0)+' · Q '+(meta.questions||0)
          +' · fade '+(meta.fading||0)+' · weak '+(meta.weakening||0))
   +hcard('Reasoners (D)', rzn.enabled===false?'off':('budget '+(rzn.daily_remaining!=null?rzn.daily_remaining:'—')),
          (lastRzn.reason||'idle')
          +((lastRzn.proposal&&lastRzn.proposal.reasoner)
            ?(' · '+lastRzn.proposal.reasoner):'')
          +(rzn.fulfillment_delta!=null
            ?(' · fulfill Δ '+rzn.fulfillment_delta):''))
   +hcard('Economy (C)', eco.enabled===false?'off':(eco.compaction?'compact ON':'observe'),
          'fresh '+(ecoLc.fresh||0)+' · absorbed '+(ecoLc.absorbed||0)
          +' · compacted '+(ecoLc.compacted||0)
          +(eco.due?' · due':''))
   +hcard('Lance index', ecoLance.exists===false?'empty'
          :(String(ecoLance.versions!=null?ecoLance.versions:'—')+' vers'),
          'rows '+(ecoLance.rows!=null?ecoLance.rows:'—')
          +' · every '+(ecoLance.optimize_every!=null?ecoLance.optimize_every:'—'))
   +hcard('Forgotten (30d)', String(ecoForgot.length),
          ecoForgot.length
            ?('latest event '+(ecoForgot[0].id||ecoForgot[0].event_id||'—'))
            :'none compacted this month')
   +hcard('Predictors (F)', fPred.enabled===false?'off'
          :(fApp.version||'heuristic-v1'),
          'next_app · next_contact · next_document'
          +((fTasks.next_app&&fTasks.next_app.preview&&fTasks.next_app.preview[0])
            ?(' · top '+esc(String(fTasks.next_app.preview[0].label
              ||fTasks.next_app.preview[0].key||'')))
            :' · console-only'))
   +hcard('Restore drill', fHard.drill_due?'due'
          :((fHard.last_drill&&fHard.last_drill.ok)?'ok':'—'),
          (fBat&&fBat.percent!=null
            ?('battery '+fBat.percent+'%'+(fBat.plugged?' plugged':'')+' · ')
            :'')
          +(fNonDef.length?('non-default '+fNonDef.length):'defaults match'))
   +hcard('Corpus path', esc((c.path||'data/bench/attention/golden.jsonl')),
          c.frozen?'MANIFEST stamped':'run freeze_attention_corpus.py --freeze')
   +'</div>'
   +killSwitchPanel(fHard.kill_switches||[])
   +'<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">'
   +'<button class="btn" onclick="runAttnBackfill()">Backfill traces</button>'
   +'<button class="btn" onclick="runAttnReplay()">Run replay gate</button>'
   +'<button class="btn" onclick="runCtxFeed()">Refresh context</button>'
   +'<button class="btn" onclick="runMetaMemory()">Run meta-memory</button>'
   +'<button class="btn" onclick="runPromote()">Run β promote</button>'
   +'<button class="btn" onclick="runReasoners()">Run reasoners (dry)</button>'
   +'<button class="btn" onclick="runEconomySweep()">Economy sweep</button>'
   +'<button class="btn" onclick="runLanceOptimize()">Lance optimize</button>'
   +'<button class="btn" onclick="runPredictorBench()">Predictor bench</button>'
   +'<button class="btn" onclick="runRestoreDrill()">Restore drill</button>'
   +'<button class="btn" onclick="stampFulfillment()">Stamp fulfillment baseline</button>'
   +'<button class="btn" onclick="revertLearn()">Revert β to prior</button>'
   +'</div>'
   +(ecoForgot.length
     ? ('<div class="refl" style="margin-top:14px"><div class="sechead">Forgotten this month</div>'
        +ecoForgot.slice(0,12).map(f=>{
          const eid=f.event_id||f.id;
          const sum=esc((f.summary||f.stub||('event '+eid)||'').toString().slice(0,140));
          return '<div class="sum" style="display:flex;gap:10px;align-items:center;justify-content:space-between;margin:6px 0">'
            +'<span>'+sum+' <span class="mut">#'+esc(String(eid||''))+'</span></span>'
            +'<button class="btn" onclick="restoreForgotten('+Number(eid)+')">Restore</button></div>';
        }).join('')
        +'</div>')
     : '')
   +'<p class="mut" style="margin-top:14px;font-size:12px">P0–A4 harness. '
   +(a2.field_v2
     ? 'Field v2 is ON — context lights the neighborhood. '
     : 'Field v2 is off — set QUILL_FIELD_V2=1 to rank by activation. ')
   +(a3.enabled===false
     ? 'WM is off — set QUILL_WM=1 for one attention. '
     : 'WM is ON — field, chat WORKING SET, and planner share slots. ')
   +(learn.learn_enabled
     ? 'Learning is ON — β updates from closed impressions. '
     : 'Learning is off — set QUILL_ATTENTION_LEARN=1 to train β. ')
   +'<a href="/field/state">/field/state</a> · <a href="/field/predictions">/field/predictions</a> · '
   +'<a href="/memory/changes">Memory changes</a> · '
   +'<a href="/selfreport">Self-report</a></p>';
  document.querySelectorAll('#archiveTabs .chip').forEach(ch=>ch.classList.remove('on'));
  document.getElementById('attnchip').classList.add('on');
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
async function runAttnBackfill(){
 document.getElementById('stat').textContent='backfilling traces…';
 try{ await fetch('/console/attention/backfill',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runAttnReplay(){
 document.getElementById('stat').textContent='running replay gate…';
 try{ await fetch('/console/attention/replay',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runCtxFeed(){
 document.getElementById('stat').textContent='refreshing Now-Context…';
 try{ await fetch('/console/attention/feed',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runMetaMemory(){
 document.getElementById('stat').textContent='running meta-memory…';
 try{ await fetch('/console/attention/meta',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runPromote(){
 document.getElementById('stat').textContent='running β promote gate…';
 try{ await fetch('/console/attention/promote',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runReasoners(){
 document.getElementById('stat').textContent='running reasoners (dry)…';
 try{ await fetch('/console/reasoners/run',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function stampFulfillment(){
 document.getElementById('stat').textContent='stamping fulfillment baseline…';
 try{ await fetch('/console/fulfillment/baseline',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runEconomySweep(){
 document.getElementById('stat').textContent='running economy sweep…';
 try{ await fetch('/console/economy/sweep',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runLanceOptimize(){
 document.getElementById('stat').textContent='optimizing Lance index…';
 try{ await fetch('/console/economy/lance/optimize',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runPredictorBench(){
 document.getElementById('stat').textContent='running predictor bench…';
 try{ await fetch('/console/predictors/bench',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runRestoreDrill(){
 document.getElementById('stat').textContent='running restore drill…';
 try{ await fetch('/console/hardening/drill',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function restoreForgotten(eventId){
 if(!eventId) return;
 document.getElementById('stat').textContent='restoring event '+eventId+'…';
 try{ await fetch('/console/economy/restore?event_id='+eventId,{method:'POST'}); }catch(e){}
 loadAttention();
}
async function revertLearn(){
 document.getElementById('stat').textContent='reverting β to prior…';
 try{ await fetch('/console/attention/learn/revert',{method:'POST'}); }catch(e){}
 loadAttention();
}
// #10: task-offer surfaced-rate ('getting chatty') + accept-rate (offers landing).
function offerCards(m){
 const pct=(x)=> x==null?'—':((Math.round(x*1000)/10)+'%');
 const off=m['proactive_offer'], out=m['offer_outcome'];
 let cards='';
 if(off && off.total){
   cards+=hcard('Offers surfaced', pct(off.rate),
                off.hits+' of '+off.total+' heard tasks (rest held)');
 }
 if(out && out.total){
   cards+=hcard('Offer accept-rate', pct(out.rate),
                out.hits+' of '+out.total+' surfaced offers accepted');
 } else if(off && off.total){
   cards+=hcard('Offer accept-rate', '—', 'no offers answered yet');
 }
 return cards;
}
async function load(){
 persistConsole();
 if(layer==='constellation'){ return loadConstellation(); }
 if(view==="facts"){ return loadFacts(); }
 if(view==="reflect"){ return loadReflect(); }
 if(view==="attention"){ return loadAttention(); }
 if(view==="egress"){ return loadEgress(); }
 if(view==="health"){ return loadHealth(); }
 if(view==="learning"){ return loadLearning(); }
 if(view==="turns"){ return loadTurns(); }
 if(view==="activity"){ return loadActivity(); }
 if(view==="sessions"){ return loadSessions(); }
 const u='/console/events?limit=300&low_only='+low+'&modality='+encodeURIComponent(mod)
   +'&source='+encodeURIComponent(src)+'&q='+encodeURIComponent(q.value.trim());
 try{
  const r=await fetch(u); const j=await r.json();
  document.getElementById('stat').textContent=j.count+' shown · '+j.total+' total';
  list.innerHTML = j.events.length ? j.events.map(row).join('')
    : emptyArchiveHtml();
  bindBleedRows();
 }catch(e){ list.innerHTML='<div class="empty">Could not load archive: '+e+'</div>'; }
}
function emptyArchiveHtml(){
  const parts=[];
  if(q.value.trim()) parts.push('search “'+esc(q.value.trim())+'”');
  if(mod) parts.push(mod+' only');
  if(src) parts.push('source '+src.replace(/\.$/,''));
  if(low) parts.push('low-confidence');
  const why=parts.length?('Filters: '+parts.join(' · ')+'.'):'No memories in this view yet.';
  return '<div class="empty">'+why
    +'<br><button type="button" class="btn" style="margin-top:12px" onclick="clearArchiveFilters()">Clear filters</button></div>';
}
function clearArchiveFilters(){
  mod=''; src=''; low=false; view='raw';
  q.value='';
  document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));
  const all=document.querySelector('#archiveTabs .chip[data-mod=""]:not([data-source])');
  if(all) all.classList.add('on');
  document.getElementById('lowchip')&&document.getElementById('lowchip').classList.remove('on');
  persistConsole(); load();
}
async function jobs(){
 try{
  const j=await (await fetch('/console/jobs')).json(); const s=j.stats||{};
  const parts=[]; if(s.pending)parts.push(s.pending+' pending'); if(s.running)parts.push('running');
  if(s.dead)parts.push(s.dead+' dead');
  else if(s.error)parts.push(s.error+' err');
  document.getElementById('jobs').textContent=parts.length?('worker: '+parts.join(', ')):'worker idle';
  const box=document.getElementById('deadJobsBox');
  const list=document.getElementById('deadJobsList');
  const sum=document.getElementById('deadJobsSummary');
  const dead=j.dead||[];
  if(box && list && sum){
    if(dead.length){
      box.style.display='';
      sum.textContent='Dead-letter ('+dead.length+')';
      list.innerHTML=dead.map(d=>{
        const err=esc(d.error||'');
        const when=d.updated_at?new Date(d.updated_at*1000).toLocaleString():'';
        return '<div class="dj"><b>#'+d.id+'</b> '+esc(d.kind||'')
          +' · '+d.attempts+'/'+(j.max_attempts||5)+' · '+esc(when)
          +'<span class="err">'+err+'</span></div>';
      }).join('');
    } else {
      box.style.display='none';
      list.innerHTML='';
    }
  }
 }catch(e){}
}
q.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{persistConsole();load();},250);});
setInterval(jobs, 2000); jobs();
loadAmbient();
setLayer(layer);
load();
(function stickyChromeTools(){
  const tools=document.getElementById('chromeTools');
  const list=document.getElementById('list');
  if(!tools||!list) return;
  let last=list.scrollTop, tucked=false;
  list.addEventListener('scroll',()=>{
    const y=list.scrollTop;
    const down=y>last+4;
    const up=y<last-4;
    if(down && y>48 && !tucked){ tools.classList.add('tucked'); tucked=true; }
    else if(up && tucked){ tools.classList.remove('tucked'); tucked=false; }
    last=y;
  },{passive:true});
})();
</script></body></html>""")


# ---------------------------------------------------------------------------
# People v3 P3 (WS-A): voice-track escrow — label a provisional speaker.
# Appended at end of file on purpose (parallel-agent merge friendliness).


class SpeakerLabelIn(BaseModel):
    label: str   # provisional track label, e.g. "Speaker 3"
    name: str    # the person this voice belongs to


@router.post("/speakers/label")
def speakers_label(body: SpeakerLabelIn) -> dict:
    """Bind an unbound voice track ("Speaker N") to a named person and queue
    the durable rebind job that reactivates everything escrowed against it.
    404 while QUILL_PEOPLE_ESCROW is off (feature dark by default)."""
    from app.services import people_escrow
    from app.storage import get_store

    if not people_escrow.enabled():
        raise HTTPException(
            status_code=404,
            detail="people escrow is disabled (set QUILL_PEOPLE_ESCROW=1)")
    res = people_escrow.label_speaker(get_store(), body.label, body.name)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error") or "bind failed")
    return res


@router.get("/speakers/escrow")
def speakers_escrow_status() -> dict:
    """Escrow observability: per-track escrowed-row counts + bind state."""
    from app.services import people_escrow
    from app.storage import get_store

    if not people_escrow.enabled():
        raise HTTPException(
            status_code=404,
            detail="people escrow is disabled (set QUILL_PEOPLE_ESCROW=1)")
    return people_escrow.escrow_status(get_store())
