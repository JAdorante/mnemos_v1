"""Web Perceive — the capture seam (Phase 0) and the WS ingest path (Phase 1).

Phase 0 must hold three promises: `feed()` frames chunks exactly the way the
sounddevice callback did, `capture="remote"` opens no device, and stop/pause
never silently drops in-progress speech (flush). Phase 1 must land a browser
PCM stream on the Event bus with the right source tag — and must enforce auth
itself, because LanApiAuthMiddleware (BaseHTTPMiddleware) never sees
WebSocket upgrades.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

import numpy as np  # noqa: E402

from app.services import asr  # noqa: E402
from app.services import audio as audio_mod  # noqa: E402


class _StubEngine:
    engine_id = "stub:v1"
    model_id = "stub"
    supports_context = False
    confidence_kind = asr.AVG_LOGPROB

    def __init__(self, text="hello from the browser"):
        self.text = text

    def transcribe(self, samples, sample_rate, context=None):
        return asr.ASRResult(
            text=self.text, avg_confidence=-0.21,
            confidence_kind=self.confidence_kind, engine_id=self.engine_id)


class _ScriptVAD:
    """Energy-gate stand-in for Silero: start on a loud frame, end on quiet."""

    def __init__(self):
        self._in = False

    def __call__(self, mono, return_seconds=False):
        loud = float(np.abs(mono).max()) > 0.05
        if loud and not self._in:
            self._in = True
            return {"start": 0}
        if not loud and self._in:
            self._in = False
            return {"end": 0}
        return None

    def reset_states(self):
        self._in = False


def _stub_load(self):
    self._engine = _StubEngine()
    self._vad = _ScriptVAD()


def _quiet_settings():
    """Worker-loop settings: no quality/denoise/speakers/telemetry/WAV IO."""
    base = audio_mod.settings
    return SimpleNamespace(
        audio_quality=SimpleNamespace(enabled=False, skip_bad=False),
        denoise=SimpleNamespace(enabled=False, routes=(), rescore=False),
        asr_bias=SimpleNamespace(enabled=False, recent_turns=3),
        ingest=base.ingest,
        storage=SimpleNamespace(save_audio=False),
        speakers=SimpleNamespace(enabled=False),
        telemetry=SimpleNamespace(enabled=False),
    )


_NO_LEDGER = SimpleNamespace(capture_started=lambda *a, **k: None,
                             capture_stopped=lambda *a, **k: None)

FRAME = audio_mod.AudioCfg.frame_samples
LOUD = np.full(FRAME, 0.5, dtype=np.float32)
QUIET = np.zeros(FRAME, dtype=np.float32)


class FeedSeamTests(unittest.TestCase):
    """Phase 0 — the extracted seam, no server involved."""

    def _pipeline(self):
        p = audio_mod.AudioPipeline(sink=lambda ev: None)
        p._vad = _ScriptVAD()
        p._engine = _StubEngine()
        return p

    def test_feed_frames_one_utterance(self):
        p = self._pipeline()
        for _ in range(20):
            p.feed(LOUD.copy())
        p.feed(QUIET.copy())
        self.assertEqual(p.utterances_total, 1)
        utt, t_start, t_end, vad_ms = p._utterances.get_nowait()
        # start frame + 19 in-speech frames + the closing quiet frame
        self.assertEqual(len(utt), FRAME * 21)
        self.assertLessEqual(t_start, t_end)

    def test_on_audio_still_reduces_multichannel_to_mono(self):
        p = self._pipeline()
        stereo = np.column_stack([LOUD, np.zeros_like(LOUD)])
        p._on_audio(stereo, FRAME, None, None)
        self.assertTrue(p._in_speech)          # channel 0 (loud) was fed
        self.assertEqual(len(p._buffer[0].shape), 1)

    def test_flush_finalizes_in_progress_speech(self):
        p = self._pipeline()
        for _ in range(10):
            p.feed(LOUD.copy())
        self.assertEqual(p.utterances_total, 0)   # VAD never saw the end
        p.flush()
        self.assertEqual(p.utterances_total, 1)
        self.assertFalse(p._in_speech)
        p.flush()                                  # idempotent when quiet
        self.assertEqual(p.utterances_total, 1)

    def test_remote_start_opens_no_device(self):
        with patch.object(audio_mod.AudioPipeline, "_load", _stub_load), \
                patch("app.services.usage_ledger.usage", _NO_LEDGER):
            p = audio_mod.AudioPipeline(capture="remote", source="audio.web_mic")
            p.start()
            try:
                self.assertIsNone(p._stream)       # no sounddevice stream
                self.assertIsNone(p._reader)       # no loopback thread
                self.assertTrue(p._worker.is_alive())
            finally:
                p.stop()

    def test_remote_stop_flushes_and_drains(self):
        events = []
        with patch.object(audio_mod.AudioPipeline, "_load", _stub_load), \
                patch.object(audio_mod, "settings", _quiet_settings()), \
                patch("app.services.meeting_session.should_ingest",
                      return_value=True), \
                patch("app.services.usage_ledger.usage", _NO_LEDGER):
            p = audio_mod.AudioPipeline(capture="remote", sink=events.append,
                                        source="audio.web_mic")
            p.start()
            for _ in range(20):                    # 640 ms of speech, no end
                p.feed(LOUD.copy())
            p.stop()                               # flush + drain + stop
        deadline = time.time() + 5
        while not events and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].raw, "hello from the browser")

    def test_queue_depth_reports_pending(self):
        p = self._pipeline()
        self.assertEqual(p.queue_depth(), 0)
        for _ in range(10):
            p.feed(LOUD.copy())
        p.feed(QUIET.copy())
        self.assertEqual(p.queue_depth(), 1)


class WsIngestTests(unittest.TestCase):
    """Phase 1 — the WebSocket feeder, end to end through the real app."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def setUp(self):
        from app.api import web_ingest
        web_ingest.reset_for_tests()
        self.events = []
        self._patches = [
            patch.object(audio_mod.AudioPipeline, "_load", _stub_load),
            patch.object(audio_mod, "settings", _quiet_settings()),
            patch.object(audio_mod, "bus",
                         SimpleNamespace(publish_nowait=self.events.append)),
            patch("app.services.capture_consent.allows", return_value=True),
            patch("app.services.meeting_session.should_ingest",
                  return_value=True),
            patch("app.services.usage_ledger.usage", _NO_LEDGER),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        from app.api import web_ingest
        web_ingest.reset_for_tests()
        for p in self._patches:
            p.stop()

    @staticmethod
    def _hello(source="mic"):
        return {"type": "hello", "source": source, "sample_rate": 16000,
                "format": "s16le", "session_id": "test-session"}

    @staticmethod
    def _pcm_utterance(speech_s=1.0, silence_s=0.7):
        """s16le bytes: a loud tone, then silence, so the scripted VAD closes."""
        sr = audio_mod.AudioCfg.sample_rate
        t = np.arange(int(sr * speech_s)) / sr
        speech = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
        silence = np.zeros(int(sr * silence_s), dtype="<i2")
        return np.concatenate([speech, silence]).tobytes()

    def _recv_until(self, ws, wanted, limit=20):
        for _ in range(limit):
            msg = ws.receive_json()
            if msg.get("type") == wanted:
                return msg
        self.fail(f"never received {wanted!r}")

    def test_pcm_stream_lands_on_the_bus_with_web_source(self):
        pcm = self._pcm_utterance()
        with self.client.websocket_connect("/ingest/audio") as ws:
            ws.send_json(self._hello())
            ready = self._recv_until(ws, "ready")
            self.assertEqual(ready["source"], "audio.web_mic")
            # Misaligned chunks on purpose — the server must re-chunk to 512.
            step = 2000
            for i in range(0, len(pcm), step):
                ws.send_bytes(pcm[i:i + step])
            ws.send_json({"type": "stop"})
            bye = self._recv_until(ws, "bye")
        self.assertGreaterEqual(bye["utterances"], 1)
        self.assertAlmostEqual(bye["seconds"], 1.7, delta=0.1)
        deadline = time.time() + 5
        while not self.events and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.events, "no transcript event published")
        ev = self.events[0]
        self.assertEqual(ev.source, "audio.web_mic")
        self.assertEqual(ev.raw, "hello from the browser")

    def test_tab_source_is_tagged_web_tab(self):
        with self.client.websocket_connect("/ingest/audio") as ws:
            ws.send_json(self._hello("tab"))
            ready = self._recv_until(ws, "ready")
            self.assertEqual(ready["source"], "audio.web_tab")
            ws.send_json({"type": "stop"})
            self._recv_until(ws, "bye")

    def test_consent_gate_refuses_before_any_audio(self):
        with patch("app.services.capture_consent.allows", return_value=False):
            with self.client.websocket_connect("/ingest/audio") as ws:
                ws.send_json(self._hello())
                msg = ws.receive_json()
                self.assertEqual(msg["type"], "error")
                self.assertEqual(msg["error"], "consent_required")

    def test_tab_consent_maps_to_system_audio_class(self):
        asked = []
        with patch("app.services.capture_consent.allows",
                   side_effect=lambda s: not asked.append(s)):
            with self.client.websocket_connect("/ingest/audio") as ws:
                ws.send_json(self._hello("tab"))
                ws.receive_json()
        self.assertEqual(asked, ["system_audio"])

    def test_bad_hello_is_rejected(self):
        with self.client.websocket_connect("/ingest/audio") as ws:
            ws.send_json({"type": "hello", "source": "webcam"})
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")

    def test_pause_drops_audio_resume_restores_it(self):
        pcm = self._pcm_utterance()
        with self.client.websocket_connect("/ingest/audio") as ws:
            ws.send_json(self._hello())
            self._recv_until(ws, "ready")
            ws.send_json({"type": "pause"})
            self._recv_until(ws, "paused")
            ws.send_bytes(pcm)                     # said while paused: dropped
            ws.send_json({"type": "resume"})
            self._recv_until(ws, "resumed")
            ws.send_bytes(pcm)
            ws.send_json({"type": "stop"})
            bye = self._recv_until(ws, "bye")
        # Only the post-resume audio was counted.
        self.assertAlmostEqual(bye["seconds"], 1.7, delta=0.1)
        self.assertEqual(bye["utterances"], 1)

    def test_lan_client_without_token_never_reaches_ready(self):
        from app.services import api_auth
        with patch.object(api_auth, "client_is_loopback", return_value=False), \
                patch.object(api_auth, "bind_is_loopback", return_value=False), \
                patch.object(api_auth, "get_api_token", return_value="sekrit"):
            try:
                with self.client.websocket_connect("/ingest/audio") as ws:
                    ws.send_json(self._hello())
                    msg = ws.receive_json()
                    self.assertNotEqual(msg.get("type"), "ready")
            except Exception:
                pass                               # closed pre-accept: correct

    def test_lan_client_with_query_token_is_accepted(self):
        from app.services import api_auth
        with patch.object(api_auth, "client_is_loopback", return_value=False), \
                patch.object(api_auth, "bind_is_loopback", return_value=False), \
                patch.object(api_auth, "get_api_token", return_value="sekrit"):
            with self.client.websocket_connect(
                    "/ingest/audio?token=sekrit") as ws:
                ws.send_json(self._hello())
                ready = self._recv_until(ws, "ready")
                self.assertEqual(ready["source"], "audio.web_mic")
                ws.send_json({"type": "stop"})
                self._recv_until(ws, "bye")

    def test_client_vad_utterance_bypasses_server_vad(self):
        """Phase 4 rail: an "utterance" header + one binary frame goes straight
        to the queue — no loud/quiet shaping needed, VAD never runs on it."""
        sr = audio_mod.AudioCfg.sample_rate
        # Pure silence: server VAD would never segment this. feed_utterance must.
        silent = np.zeros(sr, dtype="<i2").tobytes()
        with self.client.websocket_connect("/ingest/audio") as ws:
            ws.send_json(self._hello())
            self._recv_until(ws, "ready")
            ws.send_json({"type": "utterance",
                          "start_ts": time.time() - 1.0,
                          "end_ts": time.time()})
            ws.send_bytes(silent)
            ws.send_json({"type": "stop"})
            bye = self._recv_until(ws, "bye")
        self.assertEqual(bye["utterances"], 1)

    def test_capture_config_mirrors_server_vad(self):
        r = self.client.get("/capture/config")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["sample_rate"], audio_mod.AudioCfg.sample_rate)
        self.assertEqual(d["frame_samples"], audio_mod.AudioCfg.frame_samples)
        self.assertEqual(d["vad_threshold"], audio_mod.AudioCfg.vad_threshold)

    def test_feed_utterance_defaults_timestamps(self):
        p = audio_mod.AudioPipeline(sink=lambda ev: None)
        before = time.time()
        p.feed_utterance(np.zeros(16000, dtype=np.float32))
        utt, t0, t1, vad_ms = p._utterances.get_nowait()
        self.assertGreaterEqual(t0, before)
        self.assertIsNone(vad_ms)
        self.assertEqual(p.utterances_total, 1)

    def test_capture_page_is_served(self):
        r = self.client.get("/capture")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/ingest/audio", r.text)
        self.assertIn("getDisplayMedia", r.text)
        # Phase 4 wiring: the page knows the worker, model and config URLs.
        self.assertIn("vad_worker.js", r.text)
        self.assertIn("/capture/vad-model", r.text)
        self.assertIn("/capture/config", r.text)
        self.assertIn('header class="top"', r.text)
        self.assertIn("mnemosNav", r.text)

    def test_vad_model_is_served_from_the_installed_package(self):
        r = self.client.get("/capture/vad-model")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.content), 1_000_000)   # a real ONNX model
        self.assertEqual(r.content[0], 0x08)            # protobuf ir_version tag

    def test_ready_echoes_negotiated_vad_mode(self):
        for asked, expect in (("client", "client"), ("server", "server"),
                              (None, "server")):
            with self.client.websocket_connect("/ingest/audio") as ws:
                hello = self._hello()
                if asked is not None:
                    hello["vad"] = asked
                ws.send_json(hello)
                ready = self._recv_until(ws, "ready")
                self.assertEqual(ready["vad"], expect)
                ws.send_json({"type": "stop"})
                self._recv_until(ws, "bye")

    def test_vad_worker_static_file_exists(self):
        from pathlib import Path
        import app
        p = Path(app.__file__).parent / "static" / "vad_worker.js"
        self.assertTrue(p.is_file())
        src = p.read_text(encoding="utf-8")
        # The two constants the whole protocol hangs on.
        self.assertIn("FRAME = 512", src)
        self.assertIn("CTX = 64", src)
        # VADIterator's hysteresis gap is 0.15 below threshold.
        self.assertIn("thr - 0.15", src)


class EnrollWebTests(unittest.TestCase):
    """The enrollment web twin: raw browser PCM -> named voiceprint."""

    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.web_ingest import router
        # Bare app (no CSRF middleware) — the established endpoint-test shape.
        bare = FastAPI()
        bare.include_router(router)
        cls.client = TestClient(bare)

    def _spk(self):
        calls = []
        stub = SimpleNamespace(
            enroll=lambda name, pcm, sr: calls.append((name, len(pcm), sr)),
            enrolled_names=lambda: ["Justin"])
        return stub, calls

    def test_enroll_posts_a_voiceprint(self):
        stub, calls = self._spk()
        pcm = np.zeros(16000 * 4, dtype="<i2").tobytes()
        with patch("app.services.capture_consent.allows", return_value=True), \
                patch("app.services.speakers.speakers", stub):
            r = self.client.post("/speakers/enroll/web?name=Justin",
                                 content=pcm)
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertAlmostEqual(d["seconds"], 4.0, delta=0.1)
        self.assertEqual(d["enrolled"], ["Justin"])
        self.assertEqual(calls, [("Justin", 16000 * 4, 16000)])

    def test_enroll_requires_mic_consent(self):
        with patch("app.services.capture_consent.allows", return_value=False):
            r = self.client.post("/speakers/enroll/web?name=Justin",
                                 content=np.zeros(16000 * 4, "<i2").tobytes())
        self.assertEqual(r.status_code, 403)

    def test_enroll_rejects_short_and_odd_samples(self):
        with patch("app.services.capture_consent.allows", return_value=True):
            short = self.client.post("/speakers/enroll/web?name=J",
                                     content=np.zeros(16000, "<i2").tobytes())
            odd = self.client.post("/speakers/enroll/web?name=J",
                                   content=b"\x00" * 96001)
        self.assertEqual(short.status_code, 400)
        self.assertEqual(odd.status_code, 400)

    def test_enroll_rejects_blank_name(self):
        with patch("app.services.capture_consent.allows", return_value=True):
            r = self.client.post("/speakers/enroll/web?name=%20%20",
                                 content=np.zeros(16000 * 4, "<i2").tobytes())
        self.assertEqual(r.status_code, 400)


class HeadlessTests(unittest.TestCase):
    """QUILL_HEADLESS=1 — a hosted box must never open local devices."""

    def test_start_all_never_touches_device_pipelines(self):
        from app.api import routes
        boom = {"side_effect": AssertionError("device pipeline started")}
        with patch.dict(os.environ, {"QUILL_HEADLESS": "1"}), \
                patch.object(routes._audio, "start", **boom), \
                patch.object(routes._vision, "start", **boom), \
                patch.object(routes._desktop_capture, "start", **boom), \
                patch.object(routes._notifications, "start", **boom), \
                patch.object(routes, "_ensure_system_audio", **boom):
            routes.start_all(audio=True, vision=True, notifications=True,
                             desktop_capture=True, system_audio=True)

    def test_resume_source_points_at_the_capture_page(self):
        from fastapi import HTTPException
        from app.api import routes
        with patch.dict(os.environ, {"QUILL_HEADLESS": "1"}), \
                patch("app.services.capture_consent.allows",
                      return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                routes._resume_source("mic")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("/capture", ctx.exception.detail)

    def test_web_ingest_still_works_headless(self):
        # The WS feeder is exactly what headless mode exists for.
        from fastapi.testclient import TestClient
        from app.main import app
        from app.api import web_ingest
        web_ingest.reset_for_tests()
        client = TestClient(app)
        try:
            with patch.dict(os.environ, {"QUILL_HEADLESS": "1"}), \
                    patch.object(audio_mod.AudioPipeline, "_load", _stub_load), \
                    patch("app.services.capture_consent.allows",
                          return_value=True), \
                    patch("app.services.usage_ledger.usage", _NO_LEDGER):
                with client.websocket_connect("/ingest/audio") as ws:
                    ws.send_json({"type": "hello", "source": "mic",
                                  "sample_rate": 16000, "format": "s16le",
                                  "session_id": "hl"})
                    msg = ws.receive_json()
                    self.assertEqual(msg["type"], "ready")
                    ws.send_json({"type": "stop"})
                    while msg.get("type") != "bye":
                        msg = ws.receive_json()
        finally:
            web_ingest.reset_for_tests()


class ChannelClassificationTests(unittest.TestCase):
    def test_web_tab_diarizes_as_remote_web_mic_as_mic(self):
        from app.services.meeting_session import _channel_of
        self.assertEqual(_channel_of("audio.web_tab"), "remote")
        self.assertEqual(_channel_of("audio.web_mic"), "mic")
        self.assertEqual(_channel_of("audio.system"), "remote")
        self.assertEqual(_channel_of("audio.whisper"), "mic")


if __name__ == "__main__":
    unittest.main()
