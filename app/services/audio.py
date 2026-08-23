"""Milestone 1 — live audio pipeline.

    Laptop Mic  ->  VAD (Silero)  ->  utterance segmentation  ->  ASR (Whisper)
                ->  TranscriptEvent  ->  EventBus

Design notes
------------
* Capture runs on sounddevice's own audio thread and only does cheap work
  (VAD + buffering). Heavy work (transcription) happens on a worker thread so
  the audio callback never blocks and never drops frames.
* Silero VAD's `VADIterator` gives us speech-start / speech-end events. We
  accumulate raw samples between start and end into one utterance, then hand
  the whole utterance to Whisper. This is far more accurate than transcribing
  fixed windows.
* No PyTorch required: `silero-vad` ships an ONNX model and `faster-whisper`
  runs on CTranslate2.
"""
from __future__ import annotations

import queue
import threading
import time
from contextlib import nullcontext
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

# One Whisper + one rolling session context for ALL pipelines (mic + loopback).
# Loading two models doubles RAM and fights for CPU cores — the main reason
# meeting backlog blew past a minute while ASR itself was only ~5–14s.
_shared_whisper = None
_shared_whisper_lock = threading.Lock()
_shared_transcribe_lock = threading.Lock()
_shared_session = None
_shared_session_lock = threading.Lock()


def _get_shared_whisper():
    """Lazy-load a process-wide WhisperModel shared by every AudioPipeline."""
    global _shared_whisper
    with _shared_whisper_lock:
        if _shared_whisper is None:
            from faster_whisper import WhisperModel

            kwargs = dict(
                device=AudioCfg.device,
                compute_type=AudioCfg.compute_type,
            )
            if AudioCfg.cpu_threads > 0:
                kwargs["cpu_threads"] = AudioCfg.cpu_threads
            if AudioCfg.num_workers > 1:
                kwargs["num_workers"] = AudioCfg.num_workers
            print(
                f"[audio] loading shared Whisper '{AudioCfg.whisper_model}' "
                f"({AudioCfg.compute_type}, {AudioCfg.device}, "
                f"beam={AudioCfg.beam_size}, workers={AudioCfg.num_workers}) ..."
            )
            _shared_whisper = WhisperModel(AudioCfg.whisper_model, **kwargs)
            print("[audio] shared Whisper ready.")
        return _shared_whisper


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
    """One capture -> VAD -> Whisper pipeline instance.

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
        self._model = None
        self._vad = None
        self._buffer: list[np.ndarray] = []
        self._in_speech = False
        self._speech_started_ts = 0.0   # wall-clock at VAD speech-start (telemetry)
        self._last_text = ""       # for consecutive-duplicate suppression
        self._last_text_ts = 0.0
        # Shared across mic + loopback so meeting names bias both sides.
        self._session = _get_shared_session()

    # -- lazy heavy imports so the module imports even without deps installed --
    def _load(self) -> None:
        from silero_vad import load_silero_vad, VADIterator

        self._model = _get_shared_whisper()
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
        speech_dict = self._vad(mono, return_seconds=False)

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
                    self._utterances.put(
                        (utterance, self._speech_started_ts, time.time()))
                    self._speech_started_ts = time.time()

        if speech_dict is not None:
            if "start" in speech_dict:
                self._in_speech = True
                self._speech_started_ts = time.time()
                self._buffer = [mono]
            elif "end" in speech_dict and self._in_speech:
                self._in_speech = False
                utterance = np.concatenate(self._buffer) if self._buffer else mono
                self._buffer = []
                # Carry the speech start/end wall-clock with the audio so the
                # transcribe worker can measure end-to-end (speech-end -> published)
                # latency for the Audio Health telemetry.
                self._utterances.put((utterance, self._speech_started_ts, time.time()))

    # ------------------------------ transcribe ---------------------------
    def _transcribe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._utterances.get(timeout=0.25)
            except queue.Empty:
                continue
            # Unpack (audio, speech_start, speech_end); tolerate a bare array.
            if isinstance(item, tuple):
                audio, _t_speech_start, t_speech_end = item
            else:
                audio, _t_speech_start, t_speech_end = item, None, None
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
                "model": self.cfg.whisper_model,
            }

            # --- pre-ASR audio quality: score the waveform before Whisper so we
            # can tell "bad audio" from "Whisper failed", and (later) route weak
            # audio through denoising. Best-effort; a failure never blocks ASR.
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
                # Optional gate (off by default): don't feed Whisper clearly-bad
                # audio. Keep an audio-only event so nothing is silently lost.
                if (aq is not None and settings.audio_quality.skip_bad
                        and aq["quality"] == "bad"):
                    self._emit_audio_only(
                        audio, reason="audio_quality:" + ",".join(aq["reasons"]),
                        aq=aq)
                    self._record_tele(tele, "dropped", "bad_audio")
                    continue

            # --- denoise only when needed (#2): 'noisy' audio is enhanced before
            # Whisper; 'good' is left untouched (denoising clean speech adds
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

            # --- session-aware ASR bias (#3): prime Whisper with known names /
            # projects (from the KG) + the last few transcripts, so it spells
            # "Abby Nengel" right instead of "Abby Nagle". Best-effort.
            initial_prompt = None
            if settings.asr_bias.enabled:
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
                # Serialize when the shared model has a single CTranslate2 worker;
                # with num_workers>1 concurrent transcribe() calls are supported.
                lock = (nullcontext() if self.cfg.num_workers > 1
                        else _shared_transcribe_lock)
                with lock:
                    segments, info = self._model.transcribe(
                        asr_audio,
                        language=self.cfg.language,
                        vad_filter=False,          # we already did VAD
                        beam_size=max(1, self.cfg.beam_size),
                        best_of=max(1, self.cfg.best_of),
                        temperature=self.cfg.temperature,
                        condition_on_previous_text=self.cfg.condition_on_previous_text,
                        initial_prompt=initial_prompt,
                    )
                    # Materialize under the lock: the generator pulls from the model.
                    segs = list(segments)
                text = " ".join(s.text.strip() for s in segs).strip()
            except Exception as exc:  # keep the pipeline alive
                print(f"[audio] transcription error: {exc}")
                self._record_tele(tele, "dropped", "asr_error")
                continue
            tele["asr_latency_ms"] = round((time.time() - t_asr_start) * 1000, 1)
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
                from app.services.ingest_filter import assess, normalize

                verdict = assess(text, segs)
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

            # avg_logprob (transcription confidence) if the filter computed it,
            # else fall back to language-detection probability.
            avg_logprob = (quality["avg_logprob"] if quality is not None
                           else getattr(info, "language_probability", None))
            meta = {"duration_s": round(len(audio) / self.cfg.sample_rate, 2)}
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
                # denoise ran) — otherwise the enhanced audio, which is what Whisper
                # heard, is lost and the transcript can't be traced to its true
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
        """Write one audio telemetry row (#9). Best-effort — telemetry must never
        break capture, so a disabled flag or any DB hiccup is swallowed."""
        if not settings.telemetry.enabled:
            return
        try:
            get_store().record_audio_telemetry(
                outcome=outcome, drop_reason=drop_reason, **tele)
        except Exception as exc:
            print(f"[audio] telemetry error: {exc}")

    # ------------------------------ lifecycle ----------------------------
    def start(self) -> None:
        if self._model is None:
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
                            piece = mono[i:i + frame]
                            self._on_audio(piece, len(piece), None, None)
            except Exception as exc:
                if self._stop.is_set():
                    break
                print(f"[audio] loopback error ({exc}); retrying in 2s ...")
                if self._vad is not None:
                    self._vad.reset_states()
                self._in_speech = False
                self._buffer = []
                time.sleep(2.0)

    def stop(self) -> None:
        try:
            from app.services.usage_ledger import usage
            usage.capture_stopped("audio")
        except Exception as exc:
            print(f"[usage] audio capture stop not counted ({exc}).")
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
