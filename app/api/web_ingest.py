"""Web Perceive — browser audio capture into the shared VAD -> ASR pipeline.

    WS  /ingest/audio   binary 16 kHz mono s16le PCM from the /capture page
    GET /capture        the capture page (mic + tab audio, consent-gated)

The browser is a third feeder of `AudioPipeline` (after the sounddevice
callback and the WASAPI loopback thread): frames arrive over a WebSocket,
get re-chunked to Silero's window, and go through `pipeline.feed()`. VAD,
quality gates, denoise, ingest filter, ASR, speaker ID, provenance and the
Event bus all run unchanged server-side.

Protocol (one connection per source):
  1. client -> {"type":"hello","source":"mic"|"tab","sample_rate":16000,
                "format":"s16le","session_id":"..."}
  2. server -> {"type":"ready", ...} after the consent gate + pipeline warmup
  3. client -> binary s16le PCM frames (any size; server re-chunks)
     client -> {"type":"pause"} / {"type":"resume"} / {"type":"stop"}
     client -> {"type":"utterance","start_ts":...,"end_ts":...} followed by
     ONE binary frame holding a complete padded utterance (client-side VAD,
     Phase 4) — it bypasses server VAD framing via feed_utterance(). Mirror
     the server's thresholds from GET /capture/config so the two never
     disagree. Headerless binary still goes through server VAD, so the modes
     negotiate per-frame.
  4. server -> {"type":"throttle",...} when ASR falls behind (client widens
     its send interval); {"type":"bye","utterances":N,"seconds":S} on stop.

Auth is enforced IN the handler: LanApiAuthMiddleware is a BaseHTTPMiddleware
and never sees WebSocket upgrades (see api_auth.ws_request_authorized).
Consent rides the existing capture_consent classes: web mic -> "mic",
tab/meeting audio -> "system_audio" (it records other participants).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketDisconnect

from app.services.audio import AudioPipeline

router = APIRouter()

# kind -> (event source tag, skipped tag, capture_consent class)
KINDS = {
    "mic": ("audio.web_mic", "audio.web_mic.skipped", "mic"),
    "tab": ("audio.web_tab", "audio.web_tab.skipped", "system_audio"),
}

KEEP_WARM_S = 60.0          # reconnect grace after a dropped socket
HELLO_TIMEOUT_S = 15.0
DRAIN_TIMEOUT_S = 20.0
THROTTLE_QUEUE_DEPTH = 24   # pending utterances before we ask clients to slow
THROTTLE_INTERVAL_S = 5.0
MAX_FRAME_BYTES = 1 << 20   # 1 MB ~ 32 s of audio; larger frames are abuse


class _RemoteFeed:
    """One warm remote pipeline per source kind (single-user instance)."""

    def __init__(self, kind: str) -> None:
        source, skip_source, consent = KINDS[kind]
        self.kind = kind
        self.consent = consent
        self.pipeline = AudioPipeline(
            capture="remote", source=source, skip_source=skip_source)
        self.running = False
        self.epoch = 0              # bumped per connection; owner check
        self.paused = False
        self.pending = np.zeros(0, dtype=np.float32)   # re-chunk remainder
        self.idle_timer: threading.Timer | None = None
        self.last_throttle = 0.0


_feeds: dict[str, _RemoteFeed] = {}
_feeds_lock = threading.Lock()


def _acquire(kind: str) -> tuple[_RemoteFeed, int]:
    """Claim the feed for a new connection; supersedes any previous owner."""
    with _feeds_lock:
        feed = _feeds.get(kind)
        if feed is None:
            feed = _feeds[kind] = _RemoteFeed(kind)
        if feed.idle_timer is not None:
            feed.idle_timer.cancel()
            feed.idle_timer = None
        feed.epoch += 1
        feed.paused = False
        feed.pending = np.zeros(0, dtype=np.float32)
        return feed, feed.epoch


def _shutdown_feed(feed: _RemoteFeed, epoch: int) -> None:
    """Flush + drain + stop, unless a newer connection reclaimed the feed."""
    with _feeds_lock:
        if feed.epoch != epoch or not feed.running:
            return
        feed.running = False
    try:
        feed.pipeline.flush()
        feed.pipeline.drain(DRAIN_TIMEOUT_S)
        feed.pipeline.stop()
    except Exception as exc:
        print(f"[web_ingest] {feed.kind} shutdown error: {exc}")


def _keep_warm(feed: _RemoteFeed, epoch: int) -> None:
    """Socket dropped without a stop: hold the pipeline for reconnect."""
    with _feeds_lock:
        if feed.epoch != epoch:
            return
        if feed.idle_timer is not None:
            feed.idle_timer.cancel()
        feed.idle_timer = threading.Timer(
            KEEP_WARM_S, _shutdown_feed, args=(feed, epoch))
        feed.idle_timer.daemon = True
        feed.idle_timer.start()


def _feed_pcm(feed: _RemoteFeed, samples: np.ndarray) -> None:
    """Re-chunk arbitrary-sized float32 blocks to Silero's fixed window."""
    frame = feed.pipeline.cfg.frame_samples
    buf = (np.concatenate((feed.pending, samples))
           if len(feed.pending) else samples)
    n = (len(buf) // frame) * frame
    for i in range(0, n, frame):
        feed.pipeline.feed(buf[i:i + frame])
    feed.pending = buf[n:]


def reset_for_tests() -> None:
    with _feeds_lock:
        for feed in _feeds.values():
            if feed.idle_timer is not None:
                feed.idle_timer.cancel()
            if feed.running:
                try:
                    feed.pipeline.stop()
                except Exception:
                    pass
        _feeds.clear()


async def _reject(ws: WebSocket, error: str, detail: str) -> None:
    """Post-accept refusal: name the reason, then close with policy code."""
    try:
        await ws.send_json({"type": "error", "error": error, "detail": detail})
    except Exception:
        pass
    try:
        await ws.close(code=1008)
    except Exception:
        pass


@router.websocket("/ingest/audio")
async def ingest_audio(ws: WebSocket) -> None:
    from app.services import api_auth
    if not api_auth.ws_request_authorized(ws):
        # Close pre-accept: an unauthenticated LAN client learns nothing.
        await ws.close(code=1008)
        return
    await ws.accept()

    try:
        hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT_S)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await _reject(ws, "no_hello", "expected a hello frame")
        return
    except Exception:
        await _reject(ws, "bad_hello", "hello must be a JSON text frame")
        return
    kind = str(hello.get("source") or "")
    if hello.get("type") != "hello" or kind not in KINDS:
        await _reject(ws, "bad_hello", "source must be 'mic' or 'tab'")
        return
    if int(hello.get("sample_rate") or 0) != 16000 or \
            (hello.get("format") or "s16le") != "s16le":
        await _reject(ws, "bad_format", "send 16 kHz mono s16le PCM")
        return

    # Browser permission is not Sparrow consent: same gate as the desktop.
    from app.services import capture_consent
    source, _skip, consent_class = KINDS[kind]
    if not capture_consent.allows(consent_class):
        await _reject(
            ws, "consent_required",
            f"Capture source '{consent_class}' is off until Privacy consent. "
            "Open the recording controls and opt in.")
        return

    feed, epoch = _acquire(kind)
    if not feed.running:
        try:
            # Model load can take seconds — never on the event loop.
            await asyncio.to_thread(feed.pipeline.start)
            feed.running = True
        except Exception as exc:
            await _reject(ws, "pipeline_failed", str(exc))
            return
    base_utts = feed.pipeline.utterances_total
    samples_fed = 0
    sr = feed.pipeline.cfg.sample_rate
    utt_header: dict | None = None      # armed by an "utterance" text frame
    await ws.send_json({
        "type": "ready", "source": source,
        "frame_samples": feed.pipeline.cfg.frame_samples,
        "session_id": hello.get("session_id") or "",
        # Feature-negotiated VAD (Phase 4): "client" means the browser ships
        # only detected speech via "utterance" headers; "server" streams raw
        # frames through VAD framing here. Echoed for telemetry — the actual
        # dispatch is per-frame (headered vs. headerless binary).
        "vad": ("client" if hello.get("vad") == "client" else "server"),
    })

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                _keep_warm(feed, epoch)
                return
            if feed.epoch != epoch:
                await _reject(ws, "superseded",
                              "a newer connection took over this source")
                return
            data = msg.get("bytes")
            if data is not None:
                if len(data) > MAX_FRAME_BYTES:
                    await _reject(ws, "frame_too_large",
                                  f"max {MAX_FRAME_BYTES} bytes per frame")
                    return
                if feed.paused or not data:
                    utt_header = None
                    continue
                pcm = np.frombuffer(data, dtype="<i2").astype(np.float32)
                pcm /= 32768.0
                if utt_header is not None:
                    # Client clocks skew; a bad stamp would poison the
                    # speech-end -> published latency telemetry.
                    now = time.time()
                    def _sane(t):
                        return (t if isinstance(t, (int, float))
                                and now - 600 <= t <= now + 5 else None)
                    feed.pipeline.feed_utterance(
                        pcm, start_ts=_sane(utt_header.get("start_ts")),
                        end_ts=_sane(utt_header.get("end_ts")))
                    utt_header = None
                else:
                    _feed_pcm(feed, pcm)
                samples_fed += len(pcm)
                depth = feed.pipeline.queue_depth()
                now = time.time()
                if (depth > THROTTLE_QUEUE_DEPTH
                        and now - feed.last_throttle > THROTTLE_INTERVAL_S):
                    feed.last_throttle = now
                    await ws.send_json({"type": "throttle",
                                        "queue_depth": depth})
                continue
            text = msg.get("text")
            if text is None:
                continue
            try:
                ctl = json.loads(text)
            except Exception:
                continue
            op = ctl.get("type")
            if op == "utterance":
                utt_header = ctl
            elif op == "pause":
                # Same one-click semantics as /capture/pause: nothing said
                # while paused is captured; in-flight speech is finalized so
                # the last words before the click are not lost.
                feed.paused = True
                feed.pending = np.zeros(0, dtype=np.float32)
                await asyncio.to_thread(feed.pipeline.flush)
                await ws.send_json({"type": "paused"})
            elif op == "resume":
                feed.paused = False
                await ws.send_json({"type": "resumed"})
            elif op == "stop":
                await asyncio.to_thread(_shutdown_feed, feed, epoch)
                await ws.send_json({
                    "type": "bye",
                    "utterances": feed.pipeline.utterances_total - base_utts,
                    "seconds": round(samples_fed / sr, 1),
                })
                await ws.close()
                return
    except WebSocketDisconnect:
        _keep_warm(feed, epoch)
    except Exception as exc:
        print(f"[web_ingest] {kind} connection error: {exc}")
        _keep_warm(feed, epoch)


@router.get("/capture/vad-model")
def capture_vad_model():
    """The Silero VAD ONNX model for in-browser segmentation (Phase 4).

    Served from the installed silero-vad package so the browser always runs
    the exact model the server would — client and server segmentation can
    never drift apart across upgrades. The 16 kHz opset-15 export is chosen
    for onnxruntime-web (WASM) compatibility and size (~1.3 MB)."""
    from pathlib import Path

    from fastapi.responses import FileResponse
    import silero_vad
    data = Path(silero_vad.__file__).parent / "data"
    for name in ("silero_vad_16k_op15.onnx", "silero_vad.onnx"):
        p = data / name
        if p.is_file():
            return FileResponse(
                str(p), media_type="application/octet-stream",
                headers={"Cache-Control": "max-age=86400"})
    raise HTTPException(404, "silero-vad model files not found")


@router.get("/capture/config")
def capture_config() -> dict:
    """VAD parameters for client-side segmentation (Phase 4): the browser
    mirrors these so client and server never disagree on what speech is."""
    from app.config import settings
    a = settings.audio
    return {
        "sample_rate": a.sample_rate,
        "frame_samples": a.frame_samples,
        "vad_threshold": a.vad_threshold,
        "min_silence_ms": a.min_silence_ms,
        "speech_pad_ms": a.speech_pad_ms,
        "max_utterance_s": a.max_utterance_s,
    }


@router.post("/speakers/enroll/web")
async def speakers_enroll_web(request: Request,
                              name: str = Query(...)) -> dict:
    """Web twin of POST /speakers/enroll: the capture page records ~10 s of
    16 kHz mono s16le PCM in the browser and posts the raw bytes here, so a
    hosted user gets a named voiceprint instead of anonymous speaker clusters.

    Normal HTTP — LanApiAuthMiddleware and CSRF apply (unlike the WS)."""
    name = name.strip()
    if not name or len(name) > 80:
        raise HTTPException(400, "name required (max 80 chars)")
    from app.services import capture_consent
    if not capture_consent.allows("mic"):
        raise HTTPException(
            403, "Capture source 'mic' is off until Privacy consent. "
                 "Open the recording controls and opt in.")
    body = await request.body()
    if len(body) % 2:
        raise HTTPException(400, "expected s16le PCM (even byte count)")
    from app.config import settings
    sr = settings.audio.sample_rate
    seconds = len(body) / 2 / sr
    if seconds < 3.0:
        raise HTTPException(400, "sample too short — record at least 3 s")
    if seconds > 60.0:
        raise HTTPException(400, "sample too long — 60 s max")
    pcm = np.frombuffer(body, dtype="<i2").astype(np.float32) / 32768.0
    from app.services.speakers import speakers as spk
    spk.enroll(name, pcm, sr)
    return {"ok": True, "seconds": round(seconds, 1),
            "enrolled": spk.enrolled_names()}


@router.get("/capture", response_class=HTMLResponse)
def capture_page() -> HTMLResponse:
    from app.api.capture_page import CAPTURE_PAGE
    # Inline JS shell — same no-store policy as the other SSR pages.
    return HTMLResponse(
        CAPTURE_PAGE,
        headers={"Cache-Control": "no-store, must-revalidate",
                 "Pragma": "no-cache"},
    )
