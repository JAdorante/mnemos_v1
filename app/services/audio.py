"""Milestone 1 — live audio pipeline.

    Laptop Mic  ->  VAD (Silero)  ->  utterance segmentation  ->  ASR (engine)
                ->  TranscriptEvent  ->  EventBus

Design notes
------------
* Capture runs on sounddevice's own audio thread and only does cheap work
  (VAD + buffering). Heavy work (transcription) happens on a worker thread so
  the audio callback never blocks and never drops frames.
* Silero VAD's `VADIterator` gives us speech-start / speech-end events. We
  accumulate raw samples between start and end into one utterance, then hand
  the whole utterance to the ASR engine. This is far more accurate than
  transcribing fixed windows.
* Which engine that is comes from `services/asr.py` (``QUILL_ASR_ENGINE``).
  This module never names one: it hands over samples and gets back an
  `ASRResult`, so swapping engines does not touch the capture path.
* No PyTorch required: `silero-vad` ships an ONNX model and the default engine
  (`faster-whisper`) runs on CTranslate2.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable

import numpy as np

from app.config import settings
from app.events import Event, Modality, bus
from app.storage import get_store

AudioCfg = settings.audio


def _dev_get(dev, key: str, default=None):
    if isinstance(dev, dict):
        return dev.get(key, default)
    return getattr(dev, key, default)


def resolve_input_device(spec: str, devices=None) -> int | None:
    """Map QUILL_AUDIO_DEVICE to a PortAudio index, or None for the default.

    Empty → None. Digit string → that index. Otherwise a case-insensitive
    substring match against input device names.
    """
    spec = (spec or "").strip()
    if not spec:
        return None
    if spec.isdigit() or (spec.startswith("-") and spec[1:].isdigit()):
        return int(spec)
    if devices is None:
        import sounddevice as sd
        devices = sd.query_devices()
    needle = spec.lower()
    for i, d in enumerate(devices):
        n_in = int(_dev_get(d, "max_input_channels", 0) or 0)
        if n_in <= 0:
            continue
        name = str(_dev_get(d, "name", "") or "")
        if needle in name.lower():
            return i
    raise ValueError(f"no input device matching {spec!r}")


def first_input_index(devices) -> int | None:
    """First PortAudio device that accepts capture, or None."""
    for i, d in enumerate(devices):
        if int(_dev_get(d, "max_input_channels", 0) or 0) > 0:
            return i
    return None


def format_input_devices(devices) -> str:
    lines = []
    for i, d in enumerate(devices):
        n_in = int(_dev_get(d, "max_input_channels", 0) or 0)
        if n_in <= 0:
            continue
        name = str(_dev_get(d, "name", "") or "")
        lines.append(f"  [{i}] {name} ({n_in} in)")
    return "\n".join(lines) if lines else "  (no input devices)"


def _log_mic_open_failure(exc: BaseException, devices=None) -> None:
    print(f"[audio] mic open failed ({exc}).")
    try:
        if devices is None:
            import sounddevice as sd
            devices = sd.query_devices()
        print("[audio] input devices:\n" + format_input_devices(devices))
    except Exception:
        print("[audio] could not list PortAudio devices.")
    print("[audio] hint: install PortAudio (Debian/Ubuntu: libportaudio2), "
          "set QUILL_AUDIO_DEVICE to an index or name, and check Privacy "
          "mic consent.")

# A callback that receives finalized transcript text. Defaults to bus publish.
TranscriptSink = Callable[[Event], None]

# One engine + one rolling session context for ALL pipelines (mic + loopback).
# Loading two models doubles RAM and fights for CPU cores — the main reason
# meeting backlog blew past a minute while ASR itself was only ~5–14s. The
# sharing (and the model, and its locking) now lives in services/asr.py; this
# module asks for an engine and does not know which one it got.
_shared_session = None
_shared_session_lock = threading.Lock()


def _get_shared_session():
    """Session context shared across mic + system so ASR bias sees both sides."""
    global _shared_session
    with _shared_session_lock:
        if _shared_session is None:
            from app.services.vocabulary import SessionContext

            _shared_session = SessionContext(maxlen=settings.asr_bias.recent_turns)
        return _shared_session


def _ms_asr_terms() -> list[str]:
    try:
        from app.services import meeting_session as _ms
        return _ms.asr_extra_terms()
    except Exception:
        return []


def _ms_speaker_space(source: str) -> str:
    try:
        from app.services import meeting_session as _ms
        return _ms.speaker_space(source)
    except Exception:
        return "default"


class AudioPipeline:
    """One capture -> VAD -> ASR pipeline instance.

    `capture` picks the audio source: "mic" (default, sounddevice input stream)
    or "loopback" (WASAPI what-the-computer-is-playing via `soundcard` — meeting
    audio, calls). Everything downstream (VAD, quality, denoise, ingest filter,
    speakers, provenance) is shared; only the capture backend and the event
    `source` tags differ, so a system-audio transcript is provenanced as
    heard-from-system rather than heard-on-mic.
    """

    def __init__(self, sink: TranscriptSink | None = None, *,
                 capture: str = "mic",
                 source: str = "audio.whisper",
                 skip_source: str = "audio.skipped",
                 device: str = "") -> None:
        self.cfg = AudioCfg
        self.capture = capture
        self.source = source
        self.skip_source = skip_source
        self.device = device               # loopback: output-device name substring
        self._sink = sink or (lambda ev: bus.publish_nowait(ev))
        self._utterances: "queue.Queue[np.ndarray]" = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._reader: threading.Thread | None = None   # loopback capture thread
        self._stream = None
        self._engine = None
        self._vad = None
        self._buffer: list[np.ndarray] = []
        self._in_speech = False
        self._speech_started_ts = 0.0   # wall-clock at VAD speech-start (telemetry)
        # Silero compute accumulated over the current utterance. Reset per
        # utterance so the stage timer describes one utterance, not the run.
        self._vad_ms = 0.0
        self._last_text = ""       # for consecutive-duplicate suppression
        self._last_text_ts = 0.0
        # Lifetime utterance count: lets a remote feeder (web ingest) report
        # "N utterances this connection" without reaching into the queue.
        self.utterances_total = 0
        # Shared across mic + loopback so meeting names bias both sides.
        self._session = _get_shared_session()

    # -- lazy heavy imports so the module imports even without deps installed --
    def _load(self) -> None:
        from silero_vad import load_silero_vad, VADIterator

        from app.services import asr as _asr

        self._engine = _asr.get_engine()
        vad_model = load_silero_vad(onnx=True)
        self._vad = VADIterator(
            vad_model,
            threshold=self.cfg.vad_threshold,
            sampling_rate=self.cfg.sample_rate,
            min_silence_duration_ms=self.cfg.min_silence_ms,
            speech_pad_ms=self.cfg.speech_pad_ms,
        )
        print(f"[audio] VAD ready ({self.capture} -> {self.source}).")

    # ------------------------------ capture ------------------------------
    def _on_audio(self, indata, frames, time_info, status) -> None:
        if status:
            print(f"[audio] stream status: {status}")
        # indata: float32 (frames, channels) -> mono float32 vector
        mono = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        self.feed(mono)

    def feed(self, mono: np.ndarray) -> None:
        """Feed one 16 kHz mono float32 chunk into VAD framing.

        Chunks must be `cfg.frame_samples` long (Silero's window). Called by
        the sounddevice callback, the loopback reader thread, and the web
        ingest WebSocket (`capture="remote"`) — three feeders, one framing
        loop, so everything downstream is shared.
        """
        # Two perf_counter reads (~100 ns) on the audio thread, which is the
        # only place Silero's cost is visible: it runs per 32 ms chunk, so it
        # cannot be timed from the worker side after the fact.
        _t_vad = time.perf_counter()
        speech_dict = self._vad(mono, return_seconds=False)
        self._vad_ms += (time.perf_counter() - _t_vad) * 1000.0

        if self._in_speech:
            self._buffer.append(mono)
            # Force-cut long continuous speech (meetings) into bounded clips so
            # Whisper stays accurate and the queue cannot grow without bound.
            max_s = self.cfg.max_utterance_s
            if max_s > 0:
                n = sum(len(x) for x in self._buffer)
                if n >= int(max_s * self.cfg.sample_rate):
                    utterance = np.concatenate(self._buffer)
                    self._buffer = []
                    # Stay in-speech: next frames continue the same talk turn.
                    self.utterances_total += 1
                    self._utterances.put(
                        (utterance, self._speech_started_ts, time.time(),
                         self._vad_ms))
                    self._speech_started_ts = time.time()
                    self._vad_ms = 0.0

        if speech_dict is not None:
            if "start" in speech_dict:
                self._in_speech = True
                self._speech_started_ts = time.time()
                self._buffer = [mono]
                self._vad_ms = 0.0
            elif "end" in speech_dict and self._in_speech:
                self._in_speech = False
                utterance = np.concatenate(self._buffer) if self._buffer else mono
                self._buffer = []
                # Carry the speech start/end wall-clock (and the VAD compute
                # spent on this utterance) with the audio so the transcribe
                # worker can measure end-to-end (speech-end -> published)
                # latency for the Audio Health telemetry.
                self.utterances_total += 1
                self._utterances.put((utterance, self._speech_started_ts,
                                      time.time(), self._vad_ms))
                self._vad_ms = 0.0

    # ------------------------------ transcribe ---------------------------
    def _transcribe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._utterances.get(timeout=0.25)
            except queue.Empty:
                continue
            # Unpack (audio, speech_start, speech_end, vad_ms); tolerate the
            # older 3-tuple and a bare array, so a queue drained across a
            # restart never crashes the worker on shape alone.
            if isinstance(item, tuple):
                audio, _t_speech_start, t_speech_end = item[:3]
                vad_ms = item[3] if len(item) > 3 else None
            else:
                audio, _t_speech_start, t_speech_end, vad_ms = item, None, None, None
            if audio is None or len(audio) < self.cfg.sample_rate * 0.2:
                continue  # ignore < 200 ms blips

            try:
                from app.services import meeting_session as _ms
                if not _ms.should_ingest(self.source):
                    continue
            except Exception:
                pass

            # One telemetry row per utterance (kept or dropped) -> Audio Health.
            tele = {
                "audio_duration_ms": round(
                    1000.0 * len(audio) / self.cfg.sample_rate, 1),
                "queue_depth": self._utterances.qsize(),
                "model": getattr(self._engine, "model_id", self.cfg.whisper_model),
                # Which pipeline produced this row. mic and loopback have
                # different audio, different timing and different failure modes;
                # aggregating them together hid both.
                "channel": self.capture,
                # Engine provenance per transcript, so a report from a tester
                # on a flag-flipped build says which engine produced it.
                "engine": getattr(self._engine, "engine_id", "?"),
            }
            if vad_ms is not None:
                tele["vad_ms"] = round(float(vad_ms), 1)
            # Handed to _record_tele, which turns them into post_ms and the
            # latency span. Stripped before the row is written.
            tele["_t_speech_end"] = t_speech_end
            # Latency program, Phase 0: how long this utterance sat between
            # speech-end and the transcribe worker picking it up. Everything
            # needed was already on hand (`t_speech_end` is stamped by the
            # capture callback), so this costs one subtraction on a thread that
            # must not be given real work. total - queue_wait - asr is then the
            # post-ASR tail, which is what Phase 3.1 has to pipeline away.
            if t_speech_end:
                tele["queue_wait_ms"] = round(
                    max(0.0, (time.time() - t_speech_end) * 1000.0), 1)

            # --- pre-ASR audio quality: score the waveform before ASR so we
            # can tell "bad audio" from "the engine failed", and (later) route
            # weak audio through denoising. Best-effort; a failure never blocks ASR.
            aq = None
            if settings.audio_quality.enabled:
                try:
                    from app.services.audio_quality import score as _aq_score

                    aq = _aq_score(audio, self.cfg.sample_rate)
                    print(f"[audio] quality={aq['quality']} snr={aq['snr_est']}dB "
                          f"rms={aq['rms']} speech={aq['vad_speech_ratio']} "
                          f"clip={aq['clipping_pct']}%"
                          + (f" ({', '.join(aq['reasons'])})" if aq['reasons'] else ""))
                except Exception as exc:
                    print(f"[audio] quality scoring error: {exc}")
                if aq is not None:
                    tele.update(quality=aq["quality"], snr_est=aq["snr_est"],
                                rms=aq["rms"], clipping_pct=aq["clipping_pct"],
                                speech_ratio=aq["vad_speech_ratio"])
                # Optional gate (off by default): don't feed the engine
                # clearly-bad audio. Keep an audio-only event so nothing is silently lost.
                if (aq is not None and settings.audio_quality.skip_bad
                        and aq["quality"] == "bad"):
                    self._emit_audio_only(
                        audio, reason="audio_quality:" + ",".join(aq["reasons"]),
                        aq=aq)
                    self._record_tele(tele, "dropped", "bad_audio")
                    continue

            # --- denoise only when needed (#2): 'noisy' audio is enhanced before
            # ASR; 'good' is left untouched (denoising clean speech adds
            # latency and can distort). The raw clip is kept for provenance —
            # only this ASR copy is enhanced.
            asr_audio = audio
            denoise_info = None
            if (aq is not None and settings.denoise.enabled
                    and aq["quality"] in settings.denoise.routes):
                try:
                    from app.services.denoise import enhance as _enhance

                    asr_audio, denoise_info = _enhance(audio, self.cfg.sample_rate)
                    if denoise_info.get("applied"):
                        note = f" via {denoise_info['backend']}"
                        if settings.denoise.rescore and settings.audio_quality.enabled:
                            from app.services.audio_quality import score as _aq_score
                            after = _aq_score(asr_audio, self.cfg.sample_rate)
                            denoise_info["quality_after"] = after["quality"]
                            denoise_info["snr_after"] = after["snr_est"]
                            note += (f" ({aq['snr_est']}→{after['snr_est']}dB, "
                                     f"{aq['quality']}→{after['quality']})")
                        print(f"[audio] denoised{note}")
                except Exception as exc:
                    print(f"[audio] denoise error: {exc}")
                    asr_audio = audio

            # --- session-aware ASR bias (#3): prime the engine with known
            # names / projects (from the KG) + the last few transcripts, so it
            # spells "Abby Nengel" right instead of "Abby Nagle". Skipped for an
            # engine with no context hook — see ASREngine.supports_context.
            # Best-effort.
            initial_prompt = None
            if settings.asr_bias.enabled and getattr(
                    self._engine, "supports_context", False):
                try:
                    from app.services.vocabulary import vocabulary

                    initial_prompt = vocabulary.whisper_prompt(
                        recent_texts=self._session.recent(
                            settings.asr_bias.recent_turns),
                        extra_terms=_ms_asr_terms()) or None
                except Exception as exc:
                    print(f"[audio] asr-bias error: {exc}")

            t_asr_start = time.time()
            try:
                # Whatever engine is configured. Decoding parameters, model
                # sharing and concurrency are the engine's business now; this
                # loop's business is what to do with the words.
                res = self._engine.transcribe(
                    asr_audio, self.cfg.sample_rate, context=initial_prompt)
                segs = res.segments
                text = res.text
            except Exception as exc:  # keep the pipeline alive
                print(f"[audio] transcription error: {exc}")
                self._record_tele(tele, "dropped", "asr_error")
                continue
            tele["asr_latency_ms"] = round((time.time() - t_asr_start) * 1000, 1)
            # Everything after this point is the post-ASR tail: ingest filter,
            # dedupe, routing, provenance, speaker ID, WAV write, publish. It is
            # the share of the utterance-end -> event budget that a faster
            # engine does NOT fix, so it has to be measured separately.
            tele["_t_asr_done"] = time.time()
            if not text:
                self._record_tele(tele, "dropped", "empty")
                continue
            ts = time.time()

            # --- ingest hygiene: route each utterance by structured verdict (#7).
            # Nothing real is silently lost — low-trust text is demoted (audio-only
            # or flagged-for-review); only confident ghost phrases are dropped.
            quality = None
            needs_review = False
            if settings.ingest.enabled:
                from app.services.asr_calibration import cfg_for as _ingest_cfg
                from app.services.ingest_filter import assess, normalize

                # Judged on THIS engine's thresholds. Whisper has none and gets
                # the shipped defaults — they were written for its scale. An
                # engine on a different confidence scale is judged on numbers
                # fitted for it, so a swap cannot quietly move the line between
                # "kept as memory" and "discarded".
                verdict = assess(text, segs, _ingest_cfg(res.engine_id))
                tele.update(avg_logprob=verdict.avg_logprob,
                            no_speech_prob=verdict.no_speech_prob,
                            filter_verdict=verdict.action,
                            low_confidence=1 if verdict.low_confidence else 0)
                if verdict.action == "drop_hallucination":
                    print(f"[audio] dropped hallucination ({verdict.reason}): {text!r}")
                    self._record_tele(tele, "dropped", "hallucination")
                    continue
                if verdict.action == "store_audio_only":
                    # No reliable text: keep the CLIP + the shaky transcript as
                    # provenance, but don't index it as a real memory.
                    print(f"[audio] audio-only ({verdict.reason}): {text!r}")
                    self._emit_audio_only(audio, reason=verdict.reason,
                                          aq=aq, transcript=text)
                    self._record_tele(tele, "dropped", "store_audio_only")
                    continue
                # Suppress a confident duplicate of the immediately-previous line
                # (Whisper repeats "Thank you." across adjacent silence windows).
                if (normalize(text) == normalize(self._last_text)
                        and ts - self._last_text_ts <= settings.ingest.dedup_window_s):
                    print(f"[audio] dropped (duplicate): {text!r}")
                    self._record_tele(tele, "dropped", "duplicate")
                    continue
                self._last_text, self._last_text_ts = text, ts
                needs_review = verdict.needs_review
                quality = verdict.as_meta()

            # --- cross-source echo dedupe: with loopback on, the mic hears
            # what the speakers play — drop this copy if the other source
            # already published the same content moments ago.
            echoed = None
            try:
                from app.services import echo_dedup as _echo
                if _echo.enabled():
                    echoed = _echo.check_and_register(text, self.source)
            except Exception as exc:
                print(f"[audio] echo dedupe skipped ({exc}).")
            if echoed:
                print(f"[audio] dropped echo of {echoed} audio: {text!r}")
                self._record_tele(tele, "dropped", "echo")
                continue

            # --- self-echo guard: the speakers just played OUR OWN TTS reply,
            # and both the mic and the loopback transcribe it right back.
            # Never ingest the app's own voice as heard speech (observed live:
            # replies fed the extractor and polluted grounding/graph).
            try:
                from app.services import voice as _voice
                if _voice.recently_spoken(text):
                    print(f"[audio] dropped self-echo (own TTS): {text!r}")
                    self._record_tele(tele, "dropped", "self_echo")
                    continue
            except Exception as exc:
                print(f"[audio] self-echo guard skipped ({exc}).")

            # Transcription confidence: the filter's per-segment mean when it
            # ran, else the engine's own. Both are on the engine's confidence
            # scale (`res.confidence_kind`) — which is why the scale is recorded
            # on the event rather than assumed downstream. `conf_from_asr`
            # handles a negative log-probability and an already-0..1 value alike.
            avg_logprob = (quality["avg_logprob"] if quality is not None
                           else res.avg_confidence)
            meta = {"duration_s": round(len(audio) / self.cfg.sample_rate, 2),
                    # Which engine produced these words. A tester's bug report
                    # on a mixed-engine build has to say that, and a memory
                    # stored under one engine's confidence scale has to record
                    # which scale that was.
                    "asr_engine": res.engine_id,
                    "confidence_kind": res.confidence_kind}
            # Word-level timings when the engine emits them (Parakeet does,
            # natively). Additive: nothing downstream requires the key, and
            # provenance links can point at exact seconds once it is there.
            if res.word_timestamps:
                meta["word_timestamps"] = res.word_timestamps
            if quality is not None:
                meta["quality"] = quality
            if needs_review:
                meta["needs_review"] = True
            if aq is not None:
                meta["audio_quality"] = aq
            if denoise_info is not None and denoise_info.get("applied"):
                meta["denoised"] = denoise_info
            if initial_prompt:
                meta["asr_biased"] = True
            # #6: classify what KIND of speech this is (command / dictation /
            # conversation / noise) and stamp it as trusted evidence on every
            # transcript. Observational by default — routing behavior is opt-in
            # (QUILL_UTTERANCE_ROUTE); the stamp alone changes nothing.
            try:
                from app.services import utterance_router as _ur
                if _ur.enabled():
                    rr = _ur.classify(text)
                    meta["utterance_type"] = rr.as_meta()
                    tele["utterance_type"] = rr.type
            except Exception as exc:
                print(f"[audio] utterance route skipped ({exc}).")
            # Persist the raw utterance as a WAV and link it from the event.
            if settings.storage.save_audio:
                try:
                    meta["audio_path"] = get_store().save_wav(
                        audio, ts, self.cfg.sample_rate
                    )
                except Exception as exc:
                    print(f"[audio] wav save error: {exc}")
                # #12: also keep the ENHANCED copy actually transcribed (when
                # denoise ran) — otherwise the enhanced audio, which is what the
                # engine heard, is lost and the transcript can't be traced to its true
                # input. Saved under a distinct name so the raw stays the ground truth.
                if (denoise_info is not None and denoise_info.get("applied")
                        and asr_audio is not audio):
                    try:
                        meta["enhanced_audio_path"] = get_store().save_wav(
                            asr_audio, ts, self.cfg.sample_rate, suffix=".enhanced")
                    except Exception as exc:
                        print(f"[audio] enhanced wav save error: {exc}")
            # #12: stamp the full provenance chain (raw -> enhanced -> transcript ->
            # corrections). Capture-time fields + the source-side corrections already
            # applied (ASR bias, denoise); later stages append (phone grounding, human
            # edits). One inspectable answer to "where did this come from?".
            try:
                from app.services import provenance as _prov
                if _prov.enabled():
                    meta["provenance"] = _prov.build(
                        raw_audio=meta.get("audio_path"),
                        enhanced_audio=meta.get("enhanced_audio_path"),
                        transcript=text, asr_prompt=initial_prompt or "",
                        audio_quality=aq, denoise=denoise_info, captured_at=ts)
            except Exception as exc:
                print(f"[audio] provenance stamp skipped ({exc}).")
            # Attribute the utterance to a speaker (anonymous cluster or name).
            people: list[str] = []
            speaker_label = ""
            if settings.speakers.enabled:
                try:
                    from app.services.speakers import speakers as _spk

                    res = _spk.identify(audio, self.cfg.sample_rate, aq=aq,
                                        space=_ms_speaker_space(self.source))
                    speaker_label = res["label"]
                    meta["speaker"] = res
                    tele.update(speaker=res.get("label"),
                                speaker_known=1 if res.get("is_known") else 0,
                                speaker_confidence=res.get("confidence"))
                    if res["is_known"]:
                        people = [res["name"]]
                except Exception as exc:
                    print(f"[audio] speaker id error: {exc}")
            ev = Event(
                time=ts,
                modality=Modality.AUDIO,
                raw=text,
                summary=text,
                source=self.source,
                confidence=avg_logprob,
                people=people,
                meta=meta,
            )
            try:
                from app.services import meeting_session as _ms
                _ms.stamp_event(ev)
                if ev.meta.get("audio_channel") == "mic" and speaker_label:
                    ev.people = list(dict.fromkeys(
                        [speaker_label] + list(ev.people or [])))
            except Exception:
                pass
            # Stamp the confidence contract (#3): a transcript is model-EXTRACTED
            # from observed audio — capture_quality from the waveform score,
            # model_confidence from the ASR certainty. Separating them lets a
            # quiet-but-clean line and a loud-but-garbled one be told apart.
            try:
                from app.services import confidence as _conf
                _conf.attach(ev, _conf.EXTRACTED,
                             capture=_conf.capture_from_audio_quality(aq),
                             model=_conf.conf_from_asr(avg_logprob))
            except Exception as exc:
                print(f"[audio] confidence stamp skipped ({exc}).")
            print(f"[transcript] {speaker_label + ': ' if speaker_label else ''}{text}")
            self._sink(ev)
            # Kept: end-to-end latency (speech-end -> published) + record the row.
            tele["char_count"] = len(text)
            if t_speech_end:
                tele["total_latency_ms"] = round((time.time() - t_speech_end) * 1000, 1)
            self._record_tele(tele, "kept")
            self._session.add(text)   # feed accepted text into session context (#3)

    def _emit_audio_only(self, audio: "np.ndarray", *, reason: str,
                         aq: dict | None = None, transcript: str = "") -> None:
        """Publish a transcript-less AUDIO event for an utterance we can't trust
        as text — bad audio (skip_bad) or a no-reliable-text ingest verdict. Keeps
        the WAV, the quality score, and any shaky transcript as provenance so a
        real commitment buried in bad audio is never silently discarded — the
        Memory Console can surface it as audio-only for review or re-processing."""
        ts = time.time()
        meta = {"duration_s": round(len(audio) / self.cfg.sample_rate, 2),
                "skipped": reason}
        if aq is not None:
            meta["audio_quality"] = aq
        if transcript:
            meta["asr_text"] = transcript      # unreliable text, kept for review
        if settings.storage.save_audio:
            try:
                meta["audio_path"] = get_store().save_wav(
                    audio, ts, self.cfg.sample_rate)
            except Exception as exc:
                print(f"[audio] wav save error: {exc}")
        ev = Event(
            time=ts, modality=Modality.AUDIO, raw="", summary="",
            source=self.skip_source, confidence=None, meta=meta,
        )
        try:
            from app.services import meeting_session as _ms
            _ms.stamp_event(ev)
        except Exception:
            pass
        self._sink(ev)

    def _record_tele(self, tele: dict, outcome: str,
                     drop_reason: str | None = None) -> None:
        """Close out one utterance: derive the post-ASR tail, write the telemetry
        row (#9), and emit the capture latency span.

        Both consumers are fed from the same numbers rather than from a second
        set of probes — the stage timings this needs were already taken for the
        Audio Health console, so the span trail costs one dict lookup on a
        thread that must not be given real work. Best-effort throughout:
        telemetry must never break capture.
        """
        # Private marks handed over by the worker; never columns.
        t_speech_end = tele.pop("_t_speech_end", None)
        t_asr_done = tele.pop("_t_asr_done", None)
        now = time.time()
        if t_asr_done:
            tele["post_ms"] = round(max(0.0, (now - t_asr_done) * 1000.0), 1)
        # End-to-end for the span even when the utterance was dropped — a drop
        # still consumed the budget, and a pipeline that is slow *because* it
        # drops late is invisible if only kept rows are timed. The telemetry
        # column keeps its kept-only meaning; this is span-side only.
        total_ms = (max(0.0, (now - t_speech_end) * 1000.0)
                    if t_speech_end else None)

        if settings.telemetry.enabled:
            try:
                get_store().record_audio_telemetry(
                    outcome=outcome, drop_reason=drop_reason, **tele)
            except Exception as exc:
                print(f"[audio] telemetry error: {exc}")

        if total_ms is not None:
            try:
                from app.services import latency as _lat

                _lat.record(
                    _lat.KIND_CAPTURE, task=self.capture, total_ms=total_ms,
                    stages={k: v for k, v in (
                        ("vad", tele.get("vad_ms")),
                        ("queue_wait", tele.get("queue_wait_ms")),
                        ("asr", tele.get("asr_latency_ms")),
                        ("post", tele.get("post_ms")),
                    ) if v is not None},
                    marks={"outcome": outcome, "drop_reason": drop_reason,
                           "engine": tele.get("engine"),
                           "channel": tele.get("channel"),
                           "quality": tele.get("quality"),
                           "audio_duration_ms": tele.get("audio_duration_ms"),
                           "queue_depth": tele.get("queue_depth")},
                )
            except Exception as exc:
                print(f"[audio] latency span skipped ({exc}).")

    # ------------------------------ lifecycle ----------------------------
    def start(self) -> None:
        if self._engine is None:
            self._load()
        # Pilot ledger (WS-A): accrue capture minutes while the pipeline runs.
        # Start/stop only — the ledger's flush timer turns wall time into whole
        # minutes, so there is no per-frame hook on the capture path (rule 3).
        try:
            from app.services.usage_ledger import usage
            usage.capture_started("audio")
        except Exception as exc:
            print(f"[usage] audio capture start not counted ({exc}).")
        self._stop.clear()
        self._worker = threading.Thread(target=self._transcribe_loop, daemon=True)
        self._worker.start()
        if self.capture == "loopback":
            self._reader = threading.Thread(target=self._loopback_loop, daemon=True)
            self._reader.start()
            print(f"[audio] loopback capture starting @ {self.cfg.sample_rate} Hz "
                  f"({self.cfg.frame_ms} ms frames) -> source={self.source}")
        elif self.capture == "remote":
            # No local device: samples arrive via feed() from the web ingest
            # WebSocket. VAD + ASR + the worker are already up at this point.
            print(f"[audio] remote capture ready @ {self.cfg.sample_rate} Hz "
                  f"({self.cfg.frame_ms} ms frames) -> source={self.source}")
        else:
            self._open_mic()

    def _open_mic(self) -> None:
        """Open the PortAudio input stream with Linux-friendly fallback."""
        import sounddevice as sd

        def _stream(device):
            return sd.InputStream(
                samplerate=self.cfg.sample_rate,
                channels=self.cfg.channels,
                dtype="float32",
                blocksize=self.cfg.frame_samples,
                device=device,
                callback=self._on_audio,
            )

        spec = getattr(self.cfg, "input_device", "") or ""
        try:
            device = resolve_input_device(spec)
        except ValueError as exc:
            _log_mic_open_failure(exc)
            raise

        try:
            self._stream = _stream(device)
            self._stream.start()
        except Exception as first:
            devices = None
            try:
                devices = sd.query_devices()
            except Exception:
                pass
            _log_mic_open_failure(first, devices)
            if device is None and devices is not None:
                fallback = first_input_index(devices)
                if fallback is not None:
                    try:
                        self._stream = _stream(fallback)
                        self._stream.start()
                        print(f"[audio] recovered on input device {fallback}.")
                    except Exception as second:
                        raise RuntimeError(
                            f"could not open microphone ({second})"
                        ) from second
                    print(f"[audio] listening @ {self.cfg.sample_rate} Hz "
                          f"({self.cfg.frame_ms} ms frames). Speak — Ctrl+C to stop.")
                    return
            raise RuntimeError(f"could not open microphone ({first})") from first
        print(f"[audio] listening @ {self.cfg.sample_rate} Hz "
              f"({self.cfg.frame_ms} ms frames). Speak — Ctrl+C to stop.")

    # ------------------------------ loopback capture ----------------------
    def _loopback_loop(self) -> None:
        """Read system-output (loopback) audio and feed the shared VAD path.

        Runs on its own daemon thread. The default speaker (or the configured
        device) is re-resolved on every (re)open, so switching output devices
        mid-meeting — e.g. plugging in headphones — recovers on its own. When
        nothing is playing, WASAPI delivers no packets and `record()` simply
        blocks; the thread is a daemon, so that never wedges shutdown."""
        import sys

        # soundcard's import-time CoInitializeEx treats S_FALSE ("COM already
        # initialized on this thread") as fatal, and each S_FALSE still bumps
        # the thread's COM refcount. This thread is ours alone, so on failure
        # drain the refcount (theirs + one pre-existing per round) and retry
        # until the init lands on a clean slate.
        sc = None
        for _ in range(4):
            try:
                import soundcard as sc
                break
            except RuntimeError as exc:
                if sys.platform != "win32":
                    raise
                last_exc = exc
                import ctypes
                for _u in range(2):
                    try:
                        ctypes.windll.ole32.CoUninitialize()
                    except Exception:
                        pass
        if sc is None:
            print(f"[audio] system-audio loopback unavailable ({last_exc}); "
                  "not capturing computer audio. Set QUILL_SYSTEM_AUDIO=0 "
                  "to silence this.")
            return

        if sys.platform == "win32":
            # A cached import skips soundcard's COM setup, so make sure THIS
            # thread has COM initialized; S_FALSE (already done) is fine here.
            import ctypes
            try:
                ctypes.windll.ole32.CoInitializeEx(None, 0)  # MULTITHREADED
            except Exception:
                pass

        frame = self.cfg.frame_samples
        # Pull ~256 ms per record() call: fewer Python round-trips per second
        # means fewer missed WASAPI packets, so fewer stream discontinuities.
        chunk = frame * 8
        # Brief capture gaps are routine on a busy CPU and harmless to VAD +
        # transcription — don't let soundcard spam the console about them.
        import warnings
        warnings.filterwarnings(
            "ignore", message="data discontinuity in recording")
        while not self._stop.is_set():
            try:
                name = self.device or sc.default_speaker().name
                mic = sc.get_microphone(name, include_loopback=True)
                with mic.recorder(samplerate=self.cfg.sample_rate,
                                  blocksize=frame) as rec:
                    print(f"[audio] loopback capturing {mic.name!r}")
                    while not self._stop.is_set():
                        data = rec.record(numframes=chunk)
                        # (frames, channels) -> mono; VAD expects 1-D float32
                        mono = (data.mean(axis=1) if data.ndim > 1
                                else data).astype(np.float32, copy=False)
                        # Silero VAD wants fixed frame-sized pieces.
                        for i in range(0, len(mono) - frame + 1, frame):
                            self.feed(mono[i:i + frame])
            except Exception as exc:
                if self._stop.is_set():
                    break
                print(f"[audio] loopback error ({exc}); retrying in 2s ...")
                if self._vad is not None:
                    self._vad.reset_states()
                self._in_speech = False
                self._buffer = []
                time.sleep(2.0)

    def flush(self) -> None:
        """Finalize any in-progress speech as an utterance.

        A remote feeder that stops (or pauses) mid-sentence has real speech in
        the buffer that VAD never got to close — never silently drop it."""
        if self._in_speech and self._buffer:
            utterance = np.concatenate(self._buffer)
            self.utterances_total += 1
            self._utterances.put((utterance, self._speech_started_ts,
                                  time.time(), self._vad_ms))
        self._buffer = []
        self._in_speech = False
        self._vad_ms = 0.0
        if self._vad is not None:
            self._vad.reset_states()

    def feed_utterance(self, utterance: np.ndarray,
                       start_ts: float | None = None,
                       end_ts: float | None = None) -> None:
        """Enqueue one complete, already-segmented utterance, bypassing VAD.

        The client-side-VAD path (web Phase 4): the browser ran Silero itself
        and only ships detected speech, so framing it again would be wrong —
        the padding around the utterance looks like silence to a second VAD."""
        ts = time.time()
        self.utterances_total += 1
        self._utterances.put((utterance, start_ts or ts, end_ts or ts, None))

    def queue_depth(self) -> int:
        """Pending (untranscribed) utterances — the web ingest backpressure signal."""
        return self._utterances.qsize()

    def drain(self, timeout: float = 10.0) -> bool:
        """Wait until the utterance queue is empty (True) or timeout (False).

        stop() ends the worker loop without processing what's still queued, so
        a remote stop that wants its final words transcribed drains first."""
        deadline = time.time() + max(0.0, timeout)
        while self._utterances.qsize() > 0:
            if time.time() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def stop(self) -> None:
        try:
            from app.services.usage_ledger import usage
            usage.capture_stopped("audio")
        except Exception as exc:
            print(f"[usage] audio capture stop not counted ({exc}).")
        if self.capture == "remote":
            self.flush()
            self.drain()
        self._stop.set()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._vad is not None:
            self._vad.reset_states()

    def run_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[audio] stopping ...")
        finally:
            self.stop()
