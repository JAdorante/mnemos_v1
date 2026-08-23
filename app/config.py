"""Central configuration. Reads from environment / .env, with sane defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
    _cred = os.environ.get("QUILL_CREDENTIALS_FILE", ".credentials.env")
    _cp = Path(_cred) if Path(_cred).is_absolute() else Path(__file__).resolve().parent.parent / _cred
    if _cp.is_file():
        load_dotenv(_cp, override=True)
except Exception:  # dotenv is optional
    pass


def _ollama_reachable() -> bool:
    url = os.environ.get("QUILL_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        from urllib.request import urlopen
        urlopen(f"{url}/api/tags", timeout=0.4).read(32)
        return True
    except Exception:
        return False


def apply_tester_profile() -> None:
    """QUILL_PROFILE=tester pins the reliable 20% for the September cohort.

    setdefault so a tester (or CI) can still override a single flag in .env.
    Ollama stays optional: local vision is skipped unless a daemon is up.
    """
    if os.environ.get("QUILL_PROFILE", "").strip().lower() != "tester":
        return
    pins = {
        "QUILL_FIRST_RUN_MODE": "meeting",
        "QUILL_PHONE_LINK": "0",
        "QUILL_ANTICIPATE": "0",
        "QUILL_DESKTOP_CAPTURE": "0",
        "QUILL_PHONE_WATCH": "0",
        "QUILL_AUTOSTART_NOTIFICATIONS": "0",
        "QUILL_TEXT_LOCAL": "0",
    }
    if not _ollama_reachable():
        pins["QUILL_VISION_LOCAL"] = "0"
    for key, val in pins.items():
        os.environ.setdefault(key, val)


apply_tester_profile()


def _get(key: str, default: str) -> str:
    return os.environ.get(key, default)


# Machine-specific audio thresholds may be auto-derived into calibration.json
# (see app/services/calibration.py + scripts/calibrate_audio.py). `_cal` is
# import-safe (returns the passed default when no calibration exists), so the
# precedence for a calibratable value is: explicit env var > calibration.json >
# shipped literal — written as _get("QUILL_...", str(_cal("dotted.path", literal))).
from app.services.calibration import cal as _cal  # noqa: E402
from app.services.camera import default_capture_backend  # noqa: E402


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16_000          # Whisper + Silero both expect 16 kHz mono
    frame_ms: int = 32                 # Silero works on 30-32 ms chunks
    channels: int = 1
    # faster-whisper model. "small" is a good CPU default; use
    # "large-v3-turbo" if you have a GPU / patience. For max-quality meeting
    # intake on CPU, "medium" + shared model (see audio.py) is the usual step up.
    whisper_model: str = _get("QUILL_WHISPER_MODEL", "small")
    compute_type: str = _get("QUILL_WHISPER_COMPUTE", "int8")  # cpu-friendly
    # "cpu" by default so it runs anywhere. Set QUILL_WHISPER_DEVICE=cuda if you
    # have a working CUDA + cuBLAS install for a big speedup.
    device: str = _get("QUILL_WHISPER_DEVICE", "cpu")
    language: str | None = _get("QUILL_ASR_LANGUAGE", "") or None
    vad_threshold: float = float(_get("QUILL_VAD_THRESHOLD", "0.5"))
    min_silence_ms: int = int(_get("QUILL_MIN_SILENCE_MS", "500"))
    speech_pad_ms: int = int(_get("QUILL_SPEECH_PAD_MS", "150"))
    # Decoding: higher beam improves spelling/accuracy (esp. names) at the cost
    # of ASR wall time. best_of only matters with temperature > 0.
    beam_size: int = int(_get("QUILL_WHISPER_BEAM_SIZE", "1"))
    best_of: int = int(_get("QUILL_WHISPER_BEST_OF", "1"))
    temperature: float = float(_get("QUILL_WHISPER_TEMPERATURE", "0"))
    condition_on_previous_text: bool = _get(
        "QUILL_ASR_CONDITION_PREVIOUS", "1"
    ) not in ("0", "false", "False")
    # CTranslate2 threading: one shared Whisper serves mic + loopback.
    cpu_threads: int = int(_get("QUILL_WHISPER_CPU_THREADS", "0"))  # 0 = library default
    num_workers: int = int(_get("QUILL_WHISPER_NUM_WORKERS", "1"))
    # PortAudio input: empty = default device; integer index; or a case-insensitive
    # name substring (e.g. "USB"). Used only for the mic pipeline, not loopback.
    input_device: str = _get("QUILL_AUDIO_DEVICE", "")
    # Force-cut long meeting turns so Whisper sees bounded clips (better
    # accuracy + avoids multi-minute backlog). 0 = never force-cut.
    max_utterance_s: float = float(_get("QUILL_ASR_MAX_UTTERANCE_S", "0"))

    @property
    def frame_samples(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000)


@dataclass(frozen=True)
class SystemAudioConfig:
    """Loopback ("what the computer is playing") capture — meeting audio, calls.

    Off by default: transcribing a meeting records the OTHER participants, which
    can require notice/consent — turning it on is a deliberate choice, like the
    other capture flags. When on, a second AudioPipeline instance runs on a
    WASAPI loopback stream of the default output device (or a named one) and
    publishes events under source=audio.system, so meeting speech is provenanced
    as heard-from-system, distinct from the user's own mic."""
    enabled: bool = _get("QUILL_SYSTEM_AUDIO", "0") not in ("0", "false", "False")
    # Substring of the output device to capture (e.g. "Headphones"). Empty =
    # follow the Windows default speaker, re-resolved on reconnect so device
    # switches mid-meeting recover.
    device: str = _get("QUILL_SYSTEM_AUDIO_DEVICE", "")
    # Cross-source echo dedupe: the mic hears what the speakers play, so the
    # same content lands twice (and speaker-ID mislabels the room echo). When
    # loopback is on, the second matching transcript within the window drops.
    echo_dedup: bool = _get("QUILL_ECHO_DEDUP", "1") not in ("0", "false", "False")
    echo_window_s: float = float(_get("QUILL_ECHO_WINDOW_S", "10"))
    echo_similarity: float = float(_get("QUILL_ECHO_SIMILARITY", "0.8"))


@dataclass(frozen=True)
class StorageConfig:
    data_dir: str = _get("QUILL_DATA_DIR", "data")
    # Off until capture consent opts in — a fresh install must not write WAVs
    # just because the mic happened to start. Consent hot-patches this at runtime.
    save_audio: bool = _get("QUILL_SAVE_AUDIO", "0") not in ("0", "false", "False")

    @property
    def db_path(self) -> str:
        return f"{self.data_dir}/quill.db"

    @property
    def audio_dir(self) -> str:
        return f"{self.data_dir}/audio"


@dataclass(frozen=True)
class SpeakerEnvConfig:
    """Acoustic-environment cutoffs for speakers.classify_environment (#4).

    These were hand-tuned to one developer's mic/rooms; they're env-overridable so
    the SAME code adapts to any machine's acoustics (and #B4 auto-calibration can
    derive them from that machine's own audio). All in the audio_quality units:
    snr in dB, rms as full-scale float, clipping as a percentage."""
    # Calibratable (env var > calibration.json > literal) — #B4 derives these
    # cutoffs from quantile splits of this machine's own SNR/RMS distribution.
    clip_pct: float = float(_get("QUILL_SPK_ENV_CLIP_PCT", "5.0"))       # > this -> "clipping"
    noisy_snr: float = float(_get("QUILL_SPK_ENV_NOISY_SNR", str(_cal("speaker_env.noisy_snr", 8.0))))     # < this -> "noisy_room"
    farfield_snr: float = float(_get("QUILL_SPK_ENV_FARFIELD_SNR", str(_cal("speaker_env.farfield_snr", 15.0))))  # < this -> far_field/laptop_fan
    farfield_rms: float = float(_get("QUILL_SPK_ENV_FARFIELD_RMS", str(_cal("speaker_env.farfield_rms", 0.02))))  # rms split at mid SNR
    close_rms: float = float(_get("QUILL_SPK_ENV_CLOSE_RMS", str(_cal("speaker_env.close_rms", 0.08))))    # >= this (high SNR) -> close_mic


@dataclass(frozen=True)
class SpeakerConfig:
    enabled: bool = _get("QUILL_SPEAKERS", "1") not in ("0", "false", "False")
    # Cosine-sim thresholds for ECAPA embeddings (L2-normalized). These are the
    # BASE numbers; #4 adapts them per acoustic environment profile.
    cluster_threshold: float = float(_get("QUILL_SPEAKER_CLUSTER_THRESHOLD", "0.40"))
    id_threshold: float = float(_get("QUILL_SPEAKER_ID_THRESHOLD", "0.45"))
    # Three-tier decision (#4): accept a NAME at/above id_threshold with a strong
    # margin; between hint_threshold and id_threshold, cluster anonymously but
    # attach the closest name as a candidate hint; below, a new/unknown speaker.
    hint_threshold: float = float(_get("QUILL_SPEAKER_HINT_THRESHOLD", "0.30"))
    min_margin: float = float(_get("QUILL_SPEAKER_MIN_MARGIN", "0.06"))
    # Learn a per-profile cluster-threshold offset online (label-free, bounded).
    adaptive: bool = _get("QUILL_SPEAKER_ADAPTIVE", "1") not in ("0", "false", "False")
    # The ECAPA embedder — swappable so a different/newer speaker model can drop in
    # without a code edit (a different embedding dim just works; voiceprints re-enroll).
    model: str = _get("QUILL_SPEAKER_MODEL", "speechbrain/spkrec-ecapa-voxceleb")
    # Clamp bounds on the environment-adapted effective thresholds (#4).
    id_clamp_lo: float = float(_get("QUILL_SPK_ID_CLAMP_LO", "0.25"))
    id_clamp_hi: float = float(_get("QUILL_SPK_ID_CLAMP_HI", "0.80"))
    cluster_clamp_lo: float = float(_get("QUILL_SPK_CLUSTER_CLAMP_LO", "0.20"))
    cluster_clamp_hi: float = float(_get("QUILL_SPK_CLUSTER_CLAMP_HI", "0.70"))
    # Online label-free adaptation of the per-profile cluster offset: begin after
    # this many samples; nudge by ±step, bounded to ±bound; tighten when re-match
    # headroom sits above `adapt_hi`, loosen when it dips below `adapt_lo`.
    adapt_min_n: int = int(_get("QUILL_SPK_ADAPT_MIN_N", "8"))
    adapt_bound: float = float(_get("QUILL_SPK_ADAPT_BOUND", "0.08"))
    adapt_step: float = float(_get("QUILL_SPK_ADAPT_STEP", "0.005"))
    adapt_hi: float = float(_get("QUILL_SPK_ADAPT_HI", "0.15"))
    adapt_lo: float = float(_get("QUILL_SPK_ADAPT_LO", "0.04"))

    @property
    def voiceprint_dir(self) -> str:
        return f"{_get('QUILL_DATA_DIR', 'data')}/speakers"


@dataclass(frozen=True)
class VisionConfig:
    # Off by default — webcam capture requires explicit in-UI consent
    # (see app/services/capture_consent.py). QUILL_VISION=1 alone no longer
    # means "start on boot"; consent + start_all / /vision/start do.
    enabled: bool = _get("QUILL_VISION", "0") not in ("0", "false", "False")
    camera_index: int = int(_get("QUILL_CAMERA_INDEX", "0"))
    # OpenCV capture backend. Windows: DirectShow (MSMF often fails to grab).
    # Linux: V4L2. Override with QUILL_CAMERA_BACKEND=v4l2|gstreamer|dshow|msmf|any.
    capture_backend: str = _get("QUILL_CAMERA_BACKEND", default_capture_backend())
    # Discard this many frames after opening so the sensor auto-exposes (early
    # frames come back black). Skip analyzing frames darker than min_brightness
    # (0-255 mean) — a covered/cold lens shouldn't burn a VLM call.
    warmup_frames: int = int(_get("QUILL_CAMERA_WARMUP", "20"))
    min_brightness: float = float(_get("QUILL_VISION_MIN_BRIGHTNESS", "8"))
    # Colored-noise / green frames on Windows are a pixel-format mismatch: OpenCV
    # grabs the camera's raw YUY2/NV12 buffer and mis-strides it. Forcing MJPG
    # (which the camera encodes and OpenCV decodes cleanly) + RGB conversion fixes
    # it. Empty FOURCC = leave the format alone. Width/height 0 = don't request.
    capture_fourcc: str = _get("QUILL_CAMERA_FOURCC", "MJPG" if os.name == "nt" else "")
    capture_width: int = int(_get("QUILL_CAMERA_WIDTH", "1280"))
    capture_height: int = int(_get("QUILL_CAMERA_HEIGHT", "720"))
    # Analyze a frame at most this often (seconds) — the VLM call costs money.
    min_interval_s: float = float(_get("QUILL_VISION_MIN_INTERVAL_S", "5"))
    # Also force a frame at least this often even without motion (seconds).
    max_interval_s: float = float(_get("QUILL_VISION_MAX_INTERVAL_S", "30"))
    # Mean abs frame-difference (0-255) above which we treat the scene as changed.
    motion_threshold: float = float(_get("QUILL_VISION_MOTION_THRESHOLD", "12"))
    jpeg_quality: int = int(_get("QUILL_VISION_JPEG_QUALITY", "80"))
    # Two paid tiers, picked by escalation reason (see vlm.VLMRouter.describe):
    # `model` is the accurate reader for high-stakes pages (todo_list/form/code,
    # weak captures, notebook OCR); `fallback_model` covers the bulk — frames
    # that just need *a* decent read because the local VLM is down, cooling, or
    # unsure. Telemetry showed ~91% of escalations are the latter, so routing
    # them to Haiku (~5x cheaper than Opus) is where the vision spend shrinks.
    model: str = _get("QUILL_VISION_MODEL", "claude-opus-4-8")
    fallback_model: str = _get("QUILL_VISION_FALLBACK_MODEL", "claude-haiku-4-5")

    # Local-first VLM (Ollama). Every selected frame goes to the local model on
    # the GPU; Claude is the *paid fallback*, called only for high-stakes pages
    # (todo_list/form/code) or when the local model reports low confidence.
    # Falls back to Claude automatically if the local model isn't reachable, so
    # this is safe to leave on even before the model is pulled.
    local_vlm: bool = _get("QUILL_VISION_LOCAL", "1") not in ("0", "false", "False")
    # minicpm-v: strong local OCR, loads on the current Ollama build. (Llama 3.2
    # Vision / 'mllama' isn't supported by this Ollama's runner — see notes.)
    local_model: str = _get("QUILL_VISION_LOCAL_MODEL", "minicpm-v")
    ollama_url: str = _get("QUILL_OLLAMA_URL", "http://127.0.0.1:11434")
    # Long enough that CPU contention (two Whisper instances + embeddings warm)
    # doesn't spuriously time the local model out and push whole cooldown
    # windows of frames to paid Claude (was 10s — telemetry showed
    # local_cooldown as the #1 escalation reason), but still bounded so a truly
    # hung model can't stall the pipeline for a minute per frame.
    local_timeout_s: float = float(_get("QUILL_VISION_LOCAL_TIMEOUT_S", "25"))
    # After a local timeout/error, skip Ollama for this many seconds and go
    # straight to Claude so every frame isn't taxed by another dead wait.
    local_cooldown_s: float = float(_get("QUILL_VISION_LOCAL_COOLDOWN_S", "120"))
    # When the local VLM is down/cooling, "1" routes frames to the cheap Claude
    # tier (never miss a frame); "0" skips ambient frames entirely until local
    # recovers (never pay for a frame local would have handled). Deliberate
    # escalations (hard pages, weak captures) still reach Claude either way.
    cloud_when_local_down: bool = _get(
        "QUILL_VISION_CLOUD_WHEN_LOCAL_DOWN", "1") not in ("0", "false", "False")
    # Escalate a content page to Claude when local confidence falls below this.
    escalate_min_conf: float = float(_get("QUILL_VISION_ESCALATE_MIN_CONF", "0.6"))
    # ...or when the frame's own capture quality (#6 frame_quality) is this weak on
    # a content page — a soft/dim capture makes local OCR untrustworthy regardless
    # of how confident the model sounds, so the accurate reader is worth the call.
    escalate_min_capture: float = float(_get("QUILL_VISION_ESCALATE_MIN_CAPTURE", "0.6"))

    @property
    def frame_dir(self) -> str:
        return f"{_get('QUILL_DATA_DIR', 'data')}/frames"


@dataclass(frozen=True)
class TextLocalConfig:
    """Local-first TEXT routing (see services/ollama_text.py + model_router.py).

    Mirror of the vision tiering for text: with QUILL_TEXT_LOCAL=1, router-served
    text calls (chat / extract / reflect / activity summarize) run on a local
    Ollama model first; Claude is the *paid parent*, invoked when the local model
    errors, its output doesn't parse, it self-reports low confidence, or the task
    is high-stakes. OFF by default — off/unset keeps today's Claude-only routing
    unchanged. Falls back to Claude automatically when Ollama is unreachable.
    """
    enabled: bool = _get("QUILL_TEXT_LOCAL", "0") not in ("0", "false", "False")
    # llama3.2 (3B instruct): small, already pulled on this class of machine; the
    # vision default (minicpm-v) stays vision-only.
    local_model: str = _get("QUILL_TEXT_LOCAL_MODEL", "llama3.2")
    ollama_url: str = _get("QUILL_OLLAMA_URL", "http://127.0.0.1:11434")  # shared with vision
    local_timeout_s: float = float(_get("QUILL_TEXT_LOCAL_TIMEOUT_S", "45"))
    # Escalate to Claude when the local model's self-reported confidence is below this.
    escalate_min_conf: float = float(_get("QUILL_TEXT_ESCALATE_MIN_CONF", "0.6"))
    # Retrieval few-shot (services/few_shot.py): before a local attempt, inject
    # up to K past escalations with similar prompts whose parent answer a human
    # accepted/edited — worked examples of this model's own failure modes.
    # 0 disables. Only the LOCAL prompt is augmented; the Claude parent prompt
    # (and the distill row's stored prompt) stay clean training targets.
    fewshot_k: int = int(_get("QUILL_TEXT_FEWSHOT_K", "3"))
    # Cosine floor — below this a "similar" example is noise, not guidance.
    fewshot_min_sim: float = float(_get("QUILL_TEXT_FEWSHOT_MIN_SIM", "0.4"))
    # Calibration (#6): small models under-rate good answers (bench: sim-0.7
    # replies self-scored 0.0). A strong retrieval match against a HUMAN-VERIFIED
    # example is measured evidence the answer is in-distribution, so effective
    # confidence is floored at top_example_sim * this weight (self-report can
    # only raise it, never fall below the evidence). 0 disables the blend.
    fewshot_conf_weight: float = float(_get("QUILL_TEXT_FEWSHOT_CONF_WEIGHT", "0.85"))
    # Phase 1.1 — model residency. Ollama unloads a model after ~5 min idle by
    # default, so the first interaction after a quiet spell pays a multi-second
    # cold load. `keep_alive` is passed per request (Ollama's own knob, no
    # daemon config needed). "0" unloads immediately, "-1" pins forever.
    keep_alive: str = _get("QUILL_OLLAMA_KEEP_ALIVE", "30m")
    # Warm the model once at startup so the FIRST user interaction never pays
    # the cold load either. Off by default: it loads weights on a machine that
    # may never make a local call this session.
    warmup: bool = _get("QUILL_OLLAMA_WARMUP", "0") not in ("0", "false", "False")

    @property
    def high_stakes_tasks(self) -> tuple[str, ...]:
        """Tasks that always escalate to Claude (comma-separated; user-extendable —
        add e.g. `extract` to force the parent on sensitive extraction)."""
        raw = _get("QUILL_TEXT_HIGH_STAKES_TASKS", "plan")
        return tuple(t.strip() for t in raw.split(",") if t.strip())


@dataclass(frozen=True)
class EscalateLogConfig:
    """Append-only local→parent distillation trail (see services/escalate_log.py).

    Written when VLMRouter actually calls Claude after a local attempt (or when
    local is unavailable and Claude runs alone). Off disables the file only —
    escalate routing itself is unchanged.
    """
    enabled: bool = _get("QUILL_ESCALATE_LOG", "1") not in ("0", "false", "False")
    # Full-fidelity TEXT rows: store the untruncated prompt/system/output so
    # rows are trainable and evaluable (you cannot fine-tune or score against a
    # 500-char prompt head). ON by default now that the learning loop is live;
    # set QUILL_DISTILL_FULL=0 to restore the old truncated, archive-lite rows.
    full_fidelity: bool = _get("QUILL_DISTILL_FULL", "1") not in ("0", "false", "False")

    @property
    def path(self) -> str:
        return _get(
            "QUILL_ESCALATE_LOG_PATH",
            f"{_get('QUILL_DATA_DIR', 'data')}/escalate_distill.jsonl",
        )


@dataclass(frozen=True)
class LearningConfig:
    """Unified verdict harvesting (services/learning_store.py, Workstream A).

    Every human verdict anywhere in the product (fact review, chat 👍/👎/✏️,
    reflection audit, person merges, claim adjudications) becomes one canonical
    `learning_pairs` row in SQLite — the substrate the exemplar store, shadow
    eval, escalation router, and LoRA curation all read. Local-only writes;
    disabling stops harvesting but never breaks the verdict surfaces.
    """
    enabled: bool = _get("QUILL_LEARNING", "1") not in ("0", "false", "False")
    # Dual-write transition (A.3): the legacy escalate_distill.jsonl writer
    # keeps running alongside learning_pairs for one release so few_shot/bench
    # stay on their current source. Flip to 0 once downstream readers migrate.
    legacy_distill: bool = _get("QUILL_LEGACY_DISTILL", "1") not in ("0", "false", "False")


@dataclass(frozen=True)
class ExemplarConfig:
    """Retrieval-first learning (services/exemplar_store.py, Workstream C).

    Curated (input → verified answer) exemplars in LanceDB, injected as
    few-shot demonstrations at local-inference time. Below ~1K examples this
    beats a LoRA on the same data, adapts the moment a verdict lands, and
    rolls back by deleting a row. OFF by default until its A/B report earns
    the flip; the legacy few_shot distill retrieval remains the fallback.
    """
    enabled: bool = _get("QUILL_EXEMPLARS", "0") not in ("0", "false", "False")
    k: int = int(_get("QUILL_EXEMPLAR_K", "3"))
    # Cosine floor per retrieved exemplar (tune per type; 0.75 start).
    min_sim: float = float(_get("QUILL_EXEMPLAR_MIN_SIM", "0.75"))
    # Total added-token budget per call (~4 chars/token heuristic).
    token_budget: int = int(_get("QUILL_EXEMPLAR_TOKEN_BUDGET", "1200"))

    @property
    def gates_path(self) -> str:
        """Per-task-type auto-disable file (written by scripts/eval_exemplars.py
        when a type's A/B delta goes negative; also the Console kill switch)."""
        return _get("QUILL_EXEMPLAR_GATES_PATH",
                    f"{_get('QUILL_DATA_DIR', 'data')}/exemplar_type_gates.json")

    @property
    def uses_path(self) -> str:
        """Append-only log of which exemplars each call injected (C.5 feed)."""
        return _get("QUILL_EXEMPLAR_USES_PATH",
                    f"{_get('QUILL_DATA_DIR', 'data')}/exemplar_uses.jsonl")


@dataclass(frozen=True)
class ShadowEvalConfig:
    """Idle shadow evaluation (services/shadow_eval.py, Workstream B).

    While the machine is idle, Claude re-grades a small nightly batch of the
    day's NON-escalated local outputs; disagreements become unconfirmed
    learning pairs — labels for exactly the failure class the escalation
    trigger cannot see (confident-but-wrong). OFF by default until the
    Learning tab ships the review surface. Personal/sensitive-classed rows
    are never sampled (shadow_eligible=0 at log time, fail-closed).
    """
    enabled: bool = _get("QUILL_SHADOW_EVAL", "0") not in ("0", "false", "False")
    # Outputs re-graded per night (stratified across task types).
    batch: int = int(_get("QUILL_SHADOW_BATCH", "20"))
    # Hard DAILY token ceiling (input+output) for grading calls. 250K tokens
    # at claude-haiku-4-5 list rates ($1/MTok in, $5/MTok out) is ≈$0.21 in +
    # ≈$0.19 out at the observed ~85/15 split — ≈$0.40/day, ≈$0.50 worst-case.
    # The job stops mid-batch when the ceiling is hit and logs the cutoff.
    budget_tokens: int = int(_get("QUILL_SHADOW_BUDGET_TOKENS", "250000"))
    # Grader tier: haiku is the bulk tier (same convention as vision
    # fallback_model) — grading 20 short outputs needs volume, not depth.
    model: str = _get("QUILL_SHADOW_MODEL", "claude-haiku-4-5")
    # Cap per grading call (verdict JSON + a corrected output).
    max_grade_tokens: int = int(_get("QUILL_SHADOW_GRADE_MAX_TOKENS", "600"))
    # Idle gate (minutes) — same default as the idle trainer.
    min_idle_s: float = float(_get("QUILL_SHADOW_IDLE_MIN", "20")) * 60
    # Explicitly documented as LOWERING label quality: unconfirmed shadow
    # pairs become exemplar/router training data without human review.
    autotrust: bool = _get("QUILL_SHADOW_AUTOTRUST", "0") not in ("0", "false", "False")

    @property
    def local_outputs_path(self) -> str:
        """Append log of kept (non-escalated) local outputs — the sample pool."""
        return _get("QUILL_SHADOW_LOCAL_OUTPUTS_PATH",
                    f"{_get('QUILL_DATA_DIR', 'data')}/local_outputs.jsonl")

    @property
    def state_path(self) -> str:
        return _get("QUILL_SHADOW_STATE_PATH",
                    f"{_get('QUILL_DATA_DIR', 'data')}/shadow_eval_state.json")

    @property
    def grades_path(self) -> str:
        """Per-row grade log (agrees included) — the router's free labels (D.1)."""
        return _get("QUILL_SHADOW_GRADES_PATH",
                    f"{_get('QUILL_DATA_DIR', 'data')}/shadow_grades.jsonl")

    @property
    def report_path(self) -> str:
        return _get("QUILL_SHADOW_REPORT_PATH",
                    f"{_get('QUILL_DATA_DIR', 'data')}/shadow_eval_report.json")


@dataclass(frozen=True)
class RouterConfig:
    """Trained escalation router (services/escalation_router.py, Workstream D).

    A small calibrated classifier predicting "will the local model fail on
    this input?" — trained from the verdict labels the other workstreams
    collect for free. Modes: off (default) | shadow (logs its decision next
    to the heuristic's; the heuristic still routes) | active (three-band
    routing; hard safety gates — high-stakes, parse failures, suspect
    answers — always escalate regardless). Activation is an explicit user
    choice with a rollback line, same UX contract as LoRA promotion.
    HARD RULE (invariant 3): the router influences the local-vs-parent
    choice ONLY — it has no code path into approval/risk classification.
    """
    mode: str = _get("QUILL_ROUTER", "off")
    # Three bands: p < t_low → local; t_low <= p < t_high → local, but
    # flagged for shadow-eval priority sampling; p >= t_high → escalate.
    t_low: float = float(_get("QUILL_ROUTER_T_LOW", "0.25"))
    t_high: float = float(_get("QUILL_ROUTER_T_HIGH", "0.6"))
    # Minimum labels before the first fit is offered at all.
    min_labels: int = int(_get("QUILL_ROUTER_MIN_LABELS", "50"))
    # Retrain when this many NEW labels accrued since the last fit.
    retrain_new_labels: int = int(_get("QUILL_ROUTER_RETRAIN_LABELS", "25"))

    @property
    def dir(self) -> str:
        """Versioned model + calibration files live here (D.2)."""
        return _get("QUILL_ROUTER_DIR",
                    f"{_get('QUILL_DATA_DIR', 'data')}/router")


@dataclass(frozen=True)
class IngestConfig:
    """Thresholds for the ASR hygiene filter (see services/ingest_filter.py).

    Signals come from faster-whisper: `no_speech_prob` (higher = more likely
    silence) and `avg_logprob` (higher = more confident; typically -0.1 to -1.5).
    Disable the whole filter with QUILL_INGEST_FILTER=0 to store everything raw.
    """
    enabled: bool = _get("QUILL_INGEST_FILTER", "1") not in ("0", "false", "False")
    min_chars: int = int(_get("QUILL_INGEST_MIN_CHARS", "2"))
    # Hard-drop thresholds.
    max_no_speech_prob: float = float(_get("QUILL_INGEST_MAX_NO_SPEECH", "0.6"))
    min_avg_logprob: float = float(_get("QUILL_INGEST_MIN_LOGPROB", "-1.0"))
    # A denylisted phrase is only dropped when confidence also looks weak.
    phrase_no_speech_prob: float = float(_get("QUILL_INGEST_PHRASE_NO_SPEECH", "0.4"))
    phrase_avg_logprob: float = float(_get("QUILL_INGEST_PHRASE_LOGPROB", "-0.5"))
    # Kept-but-flagged (surfaced in the console, not dropped).
    low_conf_logprob: float = float(_get("QUILL_INGEST_LOW_LOGPROB", "-0.7"))
    low_conf_no_speech: float = float(_get("QUILL_INGEST_LOW_NO_SPEECH", "0.5"))
    # A low-confidence transcript with at least this many words is routed to
    # needs_user_review (kept + surfaced) instead of being demoted to audio-only —
    # a real sentence Whisper wasn't sure about must not silently vanish (#7).
    review_min_words: int = int(_get("QUILL_INGEST_REVIEW_MIN_WORDS", "3"))
    # Suppress an utterance identical to the previous one within this window (s).
    dedup_window_s: float = float(_get("QUILL_INGEST_DEDUP_WINDOW_S", "20"))


@dataclass(frozen=True)
class AudioQualityConfig:
    """Pre-ASR audio quality scoring (see services/audio_quality.py).

    Scores the raw utterance waveform *before* Whisper so the rest of Mnemos can
    tell "the audio was bad" apart from "Whisper failed", and so denoising (#2)
    and the Audio Health console (#9) have a signal to route on. Pure numpy.
    """
    enabled: bool = _get("QUILL_AUDIO_QUALITY", "1") not in ("0", "false", "False")
    frame_ms: int = int(_get("QUILL_AQ_FRAME_MS", "30"))
    # Speech gate: a frame is speech when it's this many dB above the utterance's
    # own noise floor AND within `speech_range_db` of its loudest frame.
    speech_margin_db: float = float(_get("QUILL_AQ_SPEECH_MARGIN_DB", "6.0"))
    speech_range_db: float = float(_get("QUILL_AQ_SPEECH_RANGE_DB", "25.0"))
    # |sample| at/above this fraction of full scale counts as clipped.
    clip_ceiling: float = float(_get("QUILL_AQ_CLIP_CEILING", "0.98"))
    # --- "bad" = unusable (skip-ASR candidate). Calibrated against 1400 real
    # past utterances: quiet-but-clean speech transcribes fine, so loudness (rms)
    # alone is NOT a bad signal — only near-silence (very low rms AND sparse
    # speech), too-short, buried-in-noise (low SNR), or heavy clipping are. ---
    min_duration_ms: float = float(_get("QUILL_AQ_MIN_DURATION_MS", "250"))  # physical, not calibrated
    # Calibratable (env var > calibration.json > literal): these floors are
    # machine/mic-specific, so #B4 can derive them from this machine's own audio.
    bad_snr_db: float = float(_get("QUILL_AQ_BAD_SNR_DB", str(_cal("audio_quality.bad_snr_db", 3.0))))
    bad_clipping_pct: float = float(_get("QUILL_AQ_BAD_CLIPPING_PCT", "20.0"))
    # Near-silence: only "bad" when BOTH near-digital-silent AND barely any speech.
    silence_rms: float = float(_get("QUILL_AQ_SILENCE_RMS", str(_cal("audio_quality.silence_rms", 0.0006))))
    silence_speech_ratio: float = float(_get("QUILL_AQ_SILENCE_SPEECH_RATIO", "0.20"))
    # --- "noisy" = degraded but usable (a denoise/enhance candidate for #2).
    # Driven by SNR / clipping (real noise), plus a LOW speech-ratio floor that
    # only fires on genuinely mostly-silent clips — not normal short utterances
    # (a 0.40 floor over-flagged 20% of clean real audio). vad_flips stays
    # telemetry-only (flip count tracks duration/syllable rate, not noise). ---
    noisy_snr_db: float = float(_get("QUILL_AQ_NOISY_SNR_DB", str(_cal("audio_quality.noisy_snr_db", 10.0))))
    noisy_clipping_pct: float = float(_get("QUILL_AQ_NOISY_CLIPPING_PCT", "1.0"))
    noisy_speech_ratio: float = float(_get("QUILL_AQ_NOISY_SPEECH_RATIO", "0.15"))
    # When true, skip Whisper for quality=="bad" and emit an audio-only event
    # instead (the WAV + score are still kept, so nothing is silently lost).
    # Off by default: scoring ships observationally first, routing comes in #2.
    skip_bad: bool = _get("QUILL_AQ_SKIP_BAD", "0") not in ("0", "false", "False")


@dataclass(frozen=True)
class AsrBiasConfig:
    """Session-aware ASR biasing (#3) via a Whisper initial_prompt built from the
    personal knowledge graph (#11 VocabularyProvider) + the last few transcripts.
    Fixes the 'Abby Nagle'->'Abby Nengel' class of error at the source.

    CAUTION: an initial_prompt can INDUCE hallucination of the biased terms, so the
    prompt is kept short/relevant and every change is gated by the #8 eval harness.
    Disable with QUILL_ASR_BIAS=0."""
    enabled: bool = _get("QUILL_ASR_BIAS", "1") not in ("0", "false", "False")
    max_names: int = int(_get("QUILL_ASR_BIAS_MAX_NAMES", "24"))
    max_projects: int = int(_get("QUILL_ASR_BIAS_MAX_PROJECTS", "12"))
    # Include the last few accepted transcripts for conversational continuity.
    include_recent: bool = _get("QUILL_ASR_BIAS_RECENT", "1") not in ("0", "false", "False")
    recent_turns: int = int(_get("QUILL_ASR_BIAS_RECENT_TURNS", "4"))
    recent_chars: int = int(_get("QUILL_ASR_BIAS_RECENT_CHARS", "240"))
    # Hard cap on the whole prompt (Whisper's prompt window is ~224 tokens).
    max_chars: int = int(_get("QUILL_ASR_BIAS_MAX_CHARS", "600"))


@dataclass(frozen=True)
class DenoiseConfig:
    """Speech enhancement, routed by audio_quality (#2). Only 'noisy' utterances
    are denoised — 'good' goes to Whisper untouched (denoising adds latency and
    can distort clean speech) and 'bad' is skipped upstream. See services/denoise.py.

    Backend 'auto' prefers DeepFilterNet (`df`, optional) and falls back to a
    built-in numpy spectral gate that's always available."""
    backend: str = _get("QUILL_DENOISE_BACKEND", "auto").strip().lower()  # auto|deepfilternet|spectral|off
    # Whether the built-in numpy spectral gate may feed Whisper. Default OFF:
    # measured on real audio, DSP denoising RAISES SNR but HURTS transcription
    # (Whisper is more robust to natural noise than to denoiser artifacts). Only a
    # learned backend (DeepFilterNet) enhances the ASR path by default; the
    # spectral gate is still used for non-ASR/provenance. Set 1 to force it (e.g.
    # very noisy far-field). Explicit QUILL_DENOISE_BACKEND=spectral also enables it.
    spectral_asr: bool = _get("QUILL_DENOISE_SPECTRAL_ASR", "0") not in ("0", "false", "False")
    # Re-score the enhanced audio so telemetry can show the SNR/quality lift.
    rescore: bool = _get("QUILL_DENOISE_RESCORE", "1") not in ("0", "false", "False")
    # Built-in Wiener-gate params. Tuned so denoising HELPS (not just raises SNR):
    # a high gain floor + strong decision-directed smoothing keep Whisper-hurting
    # "musical noise" out. See services/denoise.py.
    noise_percentile: float = float(_get("QUILL_DENOISE_NOISE_PCTL", "25"))
    over_subtraction: float = float(_get("QUILL_DENOISE_OVERSUB", "1.2"))  # noise overestimate
    spectral_floor: float = float(_get("QUILL_DENOISE_FLOOR", "0.15"))     # Wiener gain floor
    dd_alpha: float = float(_get("QUILL_DENOISE_DD_ALPHA", "0.98"))        # a-priori SNR smoothing

    @property
    def enabled(self) -> bool:
        return self.backend not in ("0", "off", "false", "none")

    @property
    def routes(self) -> tuple[str, ...]:
        """Which audio_quality labels get enhanced (default: noisy only)."""
        raw = _get("QUILL_DENOISE_ROUTES", "noisy")
        return tuple(r.strip() for r in raw.split(",") if r.strip())


@dataclass(frozen=True)
class TelemetryConfig:
    """Per-utterance audio pipeline telemetry (#9) — feeds the Audio Health
    console. One row per handled utterance (kept or dropped). Pure bookkeeping;
    disable with QUILL_AUDIO_TELEMETRY=0."""
    enabled: bool = _get("QUILL_AUDIO_TELEMETRY", "1") not in ("0", "false", "False")
    # Default window the Audio Health view aggregates over (seconds).
    window_s: float = float(_get("QUILL_AUDIO_HEALTH_WINDOW_S", "3600"))


@dataclass(frozen=True)
class ConsolidationConfig:
    """Merge adjacent utterances into turns (see services/consolidation.py)."""
    enabled: bool = _get("QUILL_CONSOLIDATE", "1") not in ("0", "false", "False")
    # Start a new turn when the silence gap since the last utterance exceeds this.
    max_gap_s: float = float(_get("QUILL_CONSOLIDATE_MAX_GAP_S", "8"))
    # Group turns into sessions (see services/sessions.py): a new session starts
    # when the gap between consecutive turns exceeds this — much larger than the
    # turn gap, so a session is a coherent conversation/work block, not a phrase.
    session_gap_s: float = float(_get("QUILL_SESSION_GAP_S", "300"))
    # Fold desktop capture events into activities (see services/activity.py):
    # a new activity starts when the foreground app changes or no desktop event
    # arrived for this long (screen frames come every 8-45s, so a longer gap
    # means capture stopped or the machine sat idle).
    activity_gap_s: float = float(_get("QUILL_ACTIVITY_GAP_S", "300"))
    # Multimodal activity enrichment: co-timed audio transcripts / webcam vision
    # events are folded into each activity's summary as short "heard: ..." /
    # "saw: ..." segments. These cap how many snippets one activity may carry.
    activity_max_heard: int = int(_get("QUILL_ACTIVITY_MAX_HEARD", "3"))
    activity_max_saw: int = int(_get("QUILL_ACTIVITY_MAX_SAW", "2"))
    # Optional LLM polish of activity summaries at rebuild time — one call per
    # CLOSED activity (end older than activity_gap_s). OFF by default: the
    # heuristic summary is the MVP and the default path makes zero LLM calls.
    activity_summarize: bool = (
        _get("QUILL_ACTIVITY_SUMMARIZE", "0") not in ("0", "false", "False"))


@dataclass(frozen=True)
class FactHygieneConfig:
    """Write-time quality gates + lifecycle for extracted facts
    (see services/fact_gate.py) and recency weighting in semantic retrieval
    (see services/memory.py). All best-effort: when the vector index or the
    adjudicator model is unavailable the gates degrade to plain insert."""
    # Facts below this extractor-reported confidence never enter the store.
    # 0 disables the floor.
    min_conf: float = float(_get("QUILL_FACT_MIN_CONF", "0.35"))
    # Drop facts whose source_span is not a (normalized) verbatim quote of the
    # speech they cite — the hallucination guard, promoted from telemetry to gate.
    span_gate: bool = _get("QUILL_FACT_SPAN_GATE", "1") not in ("0", "false", "False")
    # Near-duplicate collapse + update/contradiction detection at write time.
    dedup: bool = _get("QUILL_FACT_DEDUP", "1") not in ("0", "false", "False")
    # Cosine similarity >= this vs an ACTIVE fact of the same kind: refresh that
    # fact instead of inserting a twin row (no model call).
    auto_dup_sim: float = float(_get("QUILL_FACT_AUTO_DUP_SIM", "0.97"))
    # Cosine in [adjudicate_sim, auto_dup_sim): a small local-model call decides
    # duplicate / update / unrelated ("meeting moved to 3pm" supersedes "at 2pm").
    adjudicate_sim: float = float(_get("QUILL_FACT_ADJUDICATE_SIM", "0.72"))
    # People v3 WS-F: structural span-overlap dedup. Two same-kind facts whose
    # source event ranges overlap >= overlap_frac AND whose texts share tokens
    # are one fact re-extracted across overlapping windows — collapsed before
    # the embedding check ever runs. Off until the noise-eval gate passes (P1).
    dedup_overlap: bool = _get("QUILL_FACT_DEDUP_V2", "0") not in ("0", "false", "False")
    overlap_frac: float = float(_get("QUILL_FACT_OVERLAP_FRAC", "0.5"))
    # Token-Jaccard floor for the structural check: distinct facts born from
    # the SAME turn share an identical event range — range overlap alone must
    # never collapse "send the deck" with "book the room".
    overlap_token_sim: float = float(_get("QUILL_FACT_OVERLAP_TOKEN_SIM", "0.5"))
    # People v3 WS-E: review-queue TTL. Unreviewed ambient (screen/document)
    # facts below ttl_max_conf that nothing referenced for ttl_days auto-archive
    # (never deleted). Speech-derived facts are exempt — they are the product.
    ttl_enabled: bool = _get("QUILL_QUEUE_TTL", "0") not in ("0", "false", "False")
    ttl_days: float = float(_get("QUILL_QUEUE_TTL_DAYS", "14"))
    ttl_max_conf: float = float(_get("QUILL_QUEUE_TTL_MAX_CONF", "0.7"))
    # Semantic-search recency blend: score += weight * 0.5^(age_days/half_life).
    # 0 weight disables — pure cosine, the pre-hygiene behavior.
    recency_weight: float = float(_get("QUILL_MEMORY_RECENCY_WEIGHT", "0.08"))
    recency_half_life_days: float = float(
        _get("QUILL_MEMORY_RECENCY_HALF_LIFE_D", "14"))


@dataclass(frozen=True)
class AnticipationConfig:
    """Likely-next suggestions from activity patterns (see services/anticipation.py).

    Off by default — proactive chat offers need an explicit opt-in. Heuristic
    only (app transition frequencies + open tasks); no LLM required.

    When enabled, defaults are intentionally quiet: high confidence, long idle,
    and bare "Open <app>" suggestions off unless QUILL_ANTICIPATE_OPEN_APP=1.
    """
    enabled: bool = _get("QUILL_ANTICIPATE", "0") not in ("0", "false", "False")
    min_conf: float = float(_get("QUILL_ANTICIPATE_MIN_CONF", "0.75"))
    cooldown_s: float = float(_get("QUILL_ANTICIPATE_COOLDOWN_S", "900"))
    # Don't re-run scoring more often than this (activity rebuilds are bursty).
    consider_cooldown_s: float = float(_get("QUILL_ANTICIPATE_CONSIDER_S", "60"))
    # Newest activity must be idle this long before we suggest a next step.
    idle_s: float = float(_get("QUILL_ANTICIPATE_IDLE_S", "120"))
    history: int = int(_get("QUILL_ANTICIPATE_HISTORY", "40"))
    min_activities: int = int(_get("QUILL_ANTICIPATE_MIN_ACTIVITIES", "4"))
    min_transition_count: int = int(_get("QUILL_ANTICIPATE_MIN_TRANSITIONS", "3"))
    max_offers: int = int(_get("QUILL_ANTICIPATE_MAX", "1"))
    # Bare "Open Cursor" style offers — noisy; off unless explicitly enabled.
    # Task-matched suggestions ("continue this open task in Cursor") still work.
    offer_open_app: bool = _get("QUILL_ANTICIPATE_OPEN_APP", "0") not in (
        "0", "false", "False")


@dataclass(frozen=True)
class WorkerConfig:
    """Durable background job runner (see services/worker.py)."""
    enabled: bool = _get("QUILL_WORKER", "1") not in ("0", "false", "False")
    poll_interval_s: float = float(_get("QUILL_WORKER_POLL_S", "2.0"))
    # Plan 0.10: 5 attempts then dead-letter (was 3).
    max_attempts: int = int(_get("QUILL_WORKER_MAX_ATTEMPTS", "5"))
    # Exponential backoff base after a failed attempt: wait base^attempts
    # seconds (capped) before the job is claimable again.
    backoff_base_s: float = float(_get("QUILL_WORKER_BACKOFF_BASE_S", "2.0"))
    backoff_cap_s: float = float(_get("QUILL_WORKER_BACKOFF_CAP_S", "60.0"))


@dataclass(frozen=True)
class MemoryConfig:
    # Semantic search via local embeddings + LanceDB. Off falls back to substring.
    semantic: bool = _get("QUILL_SEMANTIC", "1") not in ("0", "false", "False")
    embedding_model: str = _get("QUILL_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    # Lance writes one immutable version per commit and never prunes on its own;
    # unmaintained, the manifest backlog once grew to 106 GB for 145 MB of
    # vectors. Every N commits (and on open, if a backlog is found) the store
    # compacts fragments and drops old versions. 0 disables self-maintenance.
    lance_optimize_every: int = int(_get("QUILL_LANCE_OPTIMIZE_EVERY", "500"))

    # WS-E hybrid search: the substring/facts-LIKE query runs *alongside* the
    # ANN query instead of only when the index errors, so an exact identifier
    # ("capital-connect", an unusual surname) cannot lose to a semantic
    # neighbour and vanish. An exact-substring hit enters ranking at this fixed
    # score floor — high enough to survive the cut, low enough that a strong
    # semantic match still outranks it. QUILL_SEARCH_HYBRID=0 restores the old
    # vector-first-with-fallback behavior.
    hybrid: bool = _get("QUILL_SEARCH_HYBRID", "1") not in ("0", "false", "False")
    exact_floor: float = float(_get("QUILL_SEARCH_EXACT_FLOOR", "0.55"))

    @property
    def lance_dir(self) -> str:
        return f"{_get('QUILL_DATA_DIR', 'data')}/lance"


@dataclass(frozen=True)
class NotificationConfig:
    """Windows toast notifications (Phone Link iPhone mirror). Windows-only."""
    enabled: bool = (
        _get("QUILL_NOTIFICATIONS", "1" if os.name == "nt" else "0")
        not in ("0", "false", "False")
    )
    poll_interval_s: float = float(_get("QUILL_NOTIFICATION_POLL_S", "2.5"))
    # When true (default), only ingest Phone Link / Link to Windows toasts.
    phone_link_only: bool = _get("QUILL_NOTIFICATIONS_PHONE_LINK_ONLY", "1") not in (
        "0", "false", "False")


@dataclass(frozen=True)
class DesktopCaptureConfig:
    """Passive desktop observation: screen frames + mouse clicks (no keystrokes).

    Opt-in and OFF by default — this watches the user's screen/activity, so it
    must never start unless explicitly enabled. Independent of QUILL_DESKTOP_UI
    (agent pixel control). See app/services/desktop_capture.py.
    """
    enabled: bool = _get("QUILL_DESKTOP_CAPTURE", "0") not in ("0", "false", "False")
    screen: bool = _get("QUILL_DESKTOP_CAPTURE_SCREEN", "1") not in ("0", "false", "False")
    # OFF by default — click floods dominate ambient noise; opt in via Privacy
    # ("Mouse clicks") or QUILL_DESKTOP_CAPTURE_CLICKS=1.
    clicks: bool = _get("QUILL_DESKTOP_CAPTURE_CLICKS", "0") not in ("0", "false", "False")
    # Click VLM is OFF by default — coords + window + crop are cheap; describing
    # every click timed out locally and fell back to Claude. Opt in only when you
    # need "what was under the cursor" (still local-only, never escalates).
    click_vlm: bool = _get("QUILL_DESKTOP_CAPTURE_CLICK_VLM", "0") not in (
        "0", "false", "False")
    min_interval_s: float = float(_get("QUILL_DESKTOP_CAPTURE_MIN_INTERVAL_S", "8"))
    max_interval_s: float = float(_get("QUILL_DESKTOP_CAPTURE_MAX_INTERVAL_S", "45"))
    motion_threshold: float = float(_get("QUILL_DESKTOP_CAPTURE_MOTION_THRESHOLD", "10"))
    jpeg_quality: int = int(_get("QUILL_DESKTOP_CAPTURE_JPEG_QUALITY", "75"))
    # Downscale long edge before VLM (saves tokens; UI text usually survives).
    max_width: int = int(_get("QUILL_DESKTOP_CAPTURE_MAX_WIDTH", "1280"))
    click_crop: int = int(_get("QUILL_DESKTOP_CAPTURE_CLICK_CROP", "420"))
    click_vlm_min_interval_s: float = float(
        _get("QUILL_DESKTOP_CAPTURE_CLICK_VLM_MIN_S", "8"))
    # Drop double-fires / micro-jitter clicks (same button near same pixel).
    click_dedup_px: int = int(_get("QUILL_DESKTOP_CAPTURE_CLICK_DEDUP_PX", "12"))
    click_dedup_s: float = float(_get("QUILL_DESKTOP_CAPTURE_CLICK_DEDUP_S", "0.35"))

    @property
    def frame_dir(self) -> str:
        return f"{_get('QUILL_DATA_DIR', 'data')}/desktop_frames"


@dataclass(frozen=True)
class VoiceConfig:
    """Text-to-speech so Mnemos can talk back (see services/voice.py).

    `auto` uses the most human voice available: a neural (online) Edge voice when
    reachable, else the offline OS voice (SAPI5 on Windows, pyttsx3 elsewhere).
    Force one with `edge` / `sapi` / `pyttsx3`; `off` mutes everything.
    `speak_replies` controls whether the agent's chat replies are auto-spoken.
    The UI Voice chip can mute at runtime (data/voice_prefs.json) without restart."""
    backend: str = _get("QUILL_TTS", "auto").strip().lower()   # auto | edge | sapi | pyttsx3 | off
    # For edge: a name like Aria/Andrew/Ava/Guy. For SAPI: Zira/David. Substring match.
    voice: str = _get("QUILL_TTS_VOICE", "")
    rate: int = int(_get("QUILL_TTS_RATE", "0"))               # SAPI speed, -10 (slow)..10 (fast)
    volume: float = float(_get("QUILL_TTS_VOLUME", "1.0"))     # 0.0 .. 1.0
    max_chars: int = int(_get("QUILL_TTS_MAX_CHARS", "400"))   # cap a single spoken utterance
    speak_replies: bool = _get("QUILL_TTS_SPEAK_REPLIES", "1") not in ("0", "false", "False")

    @property
    def enabled(self) -> bool:
        return self.backend not in ("0", "off", "false", "none")

    @property
    def speak_kinds(self) -> tuple[str, ...]:
        raw = _get("QUILL_TTS_SPEAK_KINDS", "result,ask")
        return tuple(k.strip() for k in raw.split(",") if k.strip())


@dataclass(frozen=True)
class FirstRunConfig:
    """Meeting-first tester onboarding (see services/first_run.py).

    meeting = calendar window capture only (QUILL_PROFILE=tester pins this).
    ambient = consented always-on sources from Privacy.
    full    = existing consent-resume behaviour (code default — invariant 4).
    """
    mode: str = _get("QUILL_FIRST_RUN_MODE", "full").strip().lower() or "full"
    meeting_pad_min: float = float(_get("QUILL_MEETING_PAD_MIN", "5"))
    unlock_after_briefs: int = int(_get("QUILL_UNLOCK_AFTER_BRIEFS", "3"))


@dataclass(frozen=True)
class ExhaustConfig:
    """Gmail/Calendar metadata cold-start (see services/exhaust_ingest.py)."""
    enabled: bool = _get("QUILL_EXHAUST_INGEST", "1") not in ("0", "false", "False")
    days: int = int(_get("QUILL_EXHAUST_DAYS", "90"))
    client_id: str = _get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret: str = _get("GOOGLE_OAUTH_CLIENT_SECRET", "")

    @property
    def token_path(self) -> str:
        return _get(
            "QUILL_EXHAUST_TOKEN",
            f"{_get('QUILL_DATA_DIR', 'data')}/google_oauth_token.json")

    @property
    def ledger_path(self) -> str:
        return _get(
            "QUILL_EXHAUST_LEDGER",
            f"{_get('QUILL_DATA_DIR', 'data')}/exhaust_ledger.json")


@dataclass(frozen=True)
class McpConfig:
    """Read-only MCP memory server (mcp_server/). Off until QUILL_MCP=1."""
    enabled: bool = _get("QUILL_MCP", "0") not in ("0", "false", "False")
    bind: str = _get("QUILL_MCP_BIND", "127.0.0.1")

    @property
    def token_path(self) -> str:
        return _get("QUILL_MCP_TOKEN",
                    f"{_get('QUILL_DATA_DIR', 'data')}/mcp_token")


@dataclass(frozen=True)
class ExternalCaptureConfig:
    """Omi / phone-as-mic ingest (see services/external_capture.py)."""
    enabled: bool = _get("QUILL_EXTERNAL_CAPTURE", "0") not in (
        "0", "false", "False")


@dataclass(frozen=True)
class OnboardingConfig:
    """One-time new-user profile sheet (see services/onboarding.py).

    Seeds people/entities/facts/graph from a JSON sheet the user fills in ONCE:
    first boot writes a template + one pointer, a later boot (or POST
    /onboarding/ingest) ingests it, and the state file keeps it from ever being
    asked again. Data-only — no user-specific code (the generality rule)."""
    enabled: bool = _get("QUILL_ONBOARDING", "1") not in ("0", "false", "False")

    @property
    def profile_path(self) -> str:
        return _get("QUILL_ONBOARDING_PROFILE",
                    f"{_get('QUILL_DATA_DIR', 'data')}/onboarding_profile.json")

    @property
    def scan_enabled(self) -> bool:
        """Phase-1 system scan: pre-fill the wizard from LOW-sensitivity local
        signals (installed apps, git identity, dev-folder projects). Produces a
        REVIEWABLE draft only — never ingests; the user still confirms in the
        wizard, so seeded facts stay honestly ACCEPTED. Off => manual only."""
        return _get("QUILL_ONBOARDING_SCAN", "1") not in ("0", "false", "False")

    @property
    def scan_sources(self) -> frozenset:
        """Signals the scan reads AUTOMATICALLY when the user clicks auto-fill —
        low-sensitivity only. Browser history / email / calendar attendees are
        deliberately NOT here (they need their own consent step)."""
        raw = _get("QUILL_ONBOARDING_SCAN_SOURCES", "apps,git,projects")
        return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())

    @property
    def scan_optional(self) -> frozenset:
        """Extra sources the user may EXPLICITLY opt into per-scan (a checkbox in
        the wizard), never run automatically. Bookmarks surface only recognized
        productivity tools — personal bookmarks never enter the profile — but
        it's still opt-in. Set to empty to forbid even the opt-in."""
        raw = _get("QUILL_ONBOARDING_SCAN_OPTIONAL", "bookmarks")
        return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())

    @property
    def state_path(self) -> str:
        return _get("QUILL_ONBOARDING_STATE",
                    f"{_get('QUILL_DATA_DIR', 'data')}/onboarding_state.json")

    @property
    def scan_state_path(self) -> str:
        """Dedup ledger for system-scan ENRICHMENT — separate from the survey's
        state so the two ingest paths never confuse each other's item keys."""
        return _get("QUILL_ONBOARDING_SCAN_STATE",
                    f"{_get('QUILL_DATA_DIR', 'data')}/onboarding_scan_state.json")


@dataclass(frozen=True)
class DocumentsConfig:
    """Read-my-documents ingestion (see services/documents.py).

    The heaviest, most sensitive enrichment source: it reads the TEXT of the
    user's files (PDF / Word / notes) and mines it for facts. Unlike the app/git
    scan (metadata only), this crosses into content — so it is EXPLICIT opt-in
    (a wizard checkbox + its own endpoint), never runs automatically, and every
    fact lands unreviewed (reviewable in the Console) and reversible
    (source='documents.scan'). All limits are data (env), not code, and the
    scanner reads whatever machine it runs on — no user-specific logic."""
    enabled: bool = _get("QUILL_DOCUMENTS", "1") not in ("0", "false", "False")
    # File types we can pull clean text from. Kept conservative: plain text +
    # the two office formats with reliable pure-python parsers.
    exts_raw: str = _get("QUILL_DOCUMENTS_EXTS", ".txt,.md,.markdown,.pdf,.docx")
    max_docs: int = int(_get("QUILL_DOCUMENTS_MAX", "40"))
    max_bytes: int = int(_get("QUILL_DOCUMENTS_MAX_BYTES", "3000000"))     # per file (3 MB)
    max_chars: int = int(_get("QUILL_DOCUMENTS_MAX_CHARS", "40000"))       # per file text cap
    chunk_chars: int = int(_get("QUILL_DOCUMENTS_CHUNK", "2500"))          # per extractor call
    max_depth: int = int(_get("QUILL_DOCUMENTS_MAX_DEPTH", "4"))           # folder recursion cap

    @property
    def exts(self) -> frozenset:
        return frozenset(e.strip().lower() for e in self.exts_raw.split(",")
                         if e.strip().startswith("."))

    @property
    def roots_raw(self) -> str:
        """Explicit folders to read, ';'-separated. Empty => the OS default
        document folders (Documents/Desktop/Downloads), computed at runtime."""
        return _get("QUILL_DOCUMENTS_ROOTS", "")

    @property
    def state_path(self) -> str:
        return _get("QUILL_DOCUMENTS_STATE",
                    f"{_get('QUILL_DATA_DIR', 'data')}/documents_scan_state.json")


@dataclass(frozen=True)
class PhoneChannelConfig:
    """Direct phone -> Mnemos channel (see services/phone_channel.py).

    Lets ANY phone (iPhone via the Shortcuts app, Android via an HTTP-shortcut
    app) push notes, shares, dictations, and locations straight into the event
    pipeline over HTTP — no Phone Link dependency. Pairing is a short-lived
    code shown on the desktop (QR / typed); a claimed pairing mints a per-device
    bearer token whose hash is stored in a registry file. Device-specific
    behavior lives in the phone's own shortcuts (data), never in code."""
    enabled: bool = _get("QUILL_PHONE_CHANNEL", "1") not in ("0", "false", "False")
    pair_ttl_s: int = int(_get("QUILL_PHONE_PAIR_TTL_S", "600"))
    max_claim_attempts: int = int(_get("QUILL_PHONE_PAIR_ATTEMPTS", "5"))
    max_devices: int = int(_get("QUILL_PHONE_MAX_DEVICES", "8"))
    max_text_chars: int = int(_get("QUILL_PHONE_MAX_TEXT", "8000"))
    # Photo ingest (phone -> vision pipeline). Largest accepted upload (bytes);
    # iPhone photos are a few MB, so 12 MB covers full-res while capping abuse.
    max_photo_bytes: int = int(_get("QUILL_PHONE_MAX_PHOTO_BYTES", "12000000"))
    # Outbox (Mnemos -> phone): pending ceiling per target, and how much
    # delivered history the file keeps for the audit trail.
    max_outbox_pending: int = int(_get("QUILL_PHONE_MAX_OUTBOX", "50"))
    outbox_history: int = int(_get("QUILL_PHONE_OUTBOX_HISTORY", "200"))

    @property
    def devices_path(self) -> str:
        return _get("QUILL_PHONE_DEVICES",
                    f"{_get('QUILL_DATA_DIR', 'data')}/phone_devices.json")

    @property
    def photos_dir(self) -> str:
        return _get("QUILL_PHONE_PHOTOS",
                    f"{_get('QUILL_DATA_DIR', 'data')}/phone_photos")

    @property
    def outbox_path(self) -> str:
        return _get("QUILL_PHONE_OUTBOX",
                    f"{_get('QUILL_DATA_DIR', 'data')}/phone_outbox.json")


@dataclass(frozen=True)
class PeerChannelConfig:
    """Mnemos <-> Mnemos peer channel for teams (see services/peer_channel.py).

    Pairs two instances (mutual per-peer bearer tokens, phone-channel trust
    model) so one user's assistant can ask another's a question, answered from
    the OTHER user's memory behind THEIR consent. Default posture is "offer":
    every inbound ask waits for the human's disclosure verdict.
    QUILL_PEER_AUTO_ANSWER=1 (dev/sim) answers synchronously instead."""
    enabled: bool = _get("QUILL_PEER_CHANNEL", "1") not in ("0", "false", "False")
    auto_answer: bool = _get("QUILL_PEER_AUTO_ANSWER", "0") in ("1", "true", "True")
    pair_ttl_s: int = int(_get("QUILL_PEER_PAIR_TTL_S", "600"))
    max_claim_attempts: int = int(_get("QUILL_PEER_PAIR_ATTEMPTS", "5"))
    max_peers: int = int(_get("QUILL_PEER_MAX_PEERS", "64"))
    max_text_chars: int = int(_get("QUILL_PEER_MAX_TEXT", "4000"))
    max_pending_asks: int = int(_get("QUILL_PEER_MAX_PENDING", "50"))
    history: int = int(_get("QUILL_PEER_HISTORY", "200"))
    # Synchronous auto-mode answers hold the connection while the remote's
    # local model composes — generous by design.
    http_timeout_s: float = float(_get("QUILL_PEER_TIMEOUT_S", "120"))
    # Presence: last_seen younger than this is "online". Heartbeat pings
    # all paired peers and flushes the offline mailbox.
    presence_stale_s: int = int(_get("QUILL_PEER_PRESENCE_STALE_S", "90"))
    ping_interval_s: float = float(_get("QUILL_PEER_PING_S", "30"))
    ping_timeout_s: float = float(_get("QUILL_PEER_PING_TIMEOUT_S", "5"))
    # Off by default so existing LAN http:// pairing still works; when on,
    # join/claim refuse non-local HTTP. Localhost is always allowed.
    require_tls: bool = _get("QUILL_PEER_REQUIRE_TLS", "0") in (
        "1", "true", "True")

    @property
    def peers_path(self) -> str:
        return _get("QUILL_PEER_REGISTRY",
                    f"{_get('QUILL_DATA_DIR', 'data')}/peers.json")

    @property
    def asks_path(self) -> str:
        return _get("QUILL_PEER_ASKS",
                    f"{_get('QUILL_DATA_DIR', 'data')}/peer_asks.json")

    @property
    def sent_path(self) -> str:
        return _get("QUILL_PEER_SENT",
                    f"{_get('QUILL_DATA_DIR', 'data')}/peer_sent.json")

    @property
    def mailbox_path(self) -> str:
        return _get("QUILL_PEER_MAILBOX",
                    f"{_get('QUILL_DATA_DIR', 'data')}/peer_mailbox.json")

    @property
    def teams_path(self) -> str:
        return _get("QUILL_PEER_TEAMS",
                    f"{_get('QUILL_DATA_DIR', 'data')}/peer_teams.json")

    @property
    def loops_path(self) -> str:
        return _get("QUILL_PEER_LOOPS",
                    f"{_get('QUILL_DATA_DIR', 'data')}/peer_loops.json")


@dataclass(frozen=True)
class OrgNodeConfig:
    """Hybrid Org AI Network node (see org_coordinator/ + services/org_*.py).

    OFF by default — existing personal capture/memory/peer behaviour is
    unchanged until QUILL_ORG_NETWORK=1. When on, this Mnemos registers with
    a lightweight Org Coordinator, ships redacted upward digests, receives
    downward priority packets, and can escalate strategic blockers. Raw
    memory never leaves the machine; Claude remains the parent model via
    ModelRouter tasks org_digest / org_escalate / org_cascade."""
    enabled: bool = _get("QUILL_ORG_NETWORK", "0") not in ("0", "false", "False")
    coordinator_url: str = _get("QUILL_ORG_COORDINATOR_URL",
                                "http://127.0.0.1:8100").rstrip("/")
    node_id: str = _get("QUILL_ORG_NODE_ID", "")
    node_token: str = _get("QUILL_ORG_NODE_TOKEN", "")
    # ic | manager | exec | ceo
    role: str = _get("QUILL_ORG_ROLE", "ic").strip().lower() or "ic"
    display_name: str = _get("QUILL_ORG_DISPLAY_NAME", "")
    reports_to: str = _get("QUILL_ORG_REPORTS_TO", "")  # manager node_id
    manager_peer_id: str = _get("QUILL_ORG_MANAGER_PEER_ID", "")
    digest_interval_h: float = float(_get("QUILL_ORG_DIGEST_INTERVAL_H", "4"))
    http_timeout_s: float = float(_get("QUILL_ORG_TIMEOUT_S", "30"))

    @property
    def state_path(self) -> str:
        return _get("QUILL_ORG_STATE",
                    f"{_get('QUILL_DATA_DIR', 'data')}/org_node_state.json")

    @property
    def priorities_path(self) -> str:
        return _get("QUILL_ORG_PRIORITIES",
                    f"{_get('QUILL_DATA_DIR', 'data')}/org_priorities.json")

    @property
    def escalations_path(self) -> str:
        return _get("QUILL_ORG_ESCALATIONS",
                    f"{_get('QUILL_DATA_DIR', 'data')}/org_escalations.jsonl")


@dataclass(frozen=True)
class IcloudConfig:
    """Read-only iCloud calendar sync (see services/icloud_calendar.py).

    Runs only when the guided connect flow has stored credentials
    (QUILL_ICLOUD_USER / QUILL_ICLOUD_APP_PASSWORD in the credentials file).
    Events land as observed-tier memory (source=phone.calendar)."""
    sync_enabled: bool = _get("QUILL_ICLOUD_SYNC", "1") not in ("0", "false", "False")
    sync_interval_s: float = float(_get("QUILL_ICLOUD_SYNC_S", "1800"))
    past_days: int = int(_get("QUILL_ICLOUD_PAST_DAYS", "1"))
    ahead_days: int = int(_get("QUILL_ICLOUD_AHEAD_DAYS", "14"))

    @property
    def state_path(self) -> str:
        return _get("QUILL_ICLOUD_STATE",
                    f"{_get('QUILL_DATA_DIR', 'data')}/icloud_calendar_state.json")


@dataclass(frozen=True)
class AttentionConfig:
    """Attention-impressions ledger (Cognitive OS roadmap, Phase 0).

    Instrument-only: every node the field / grounding surfaces is logged with
    its score decomposition, and user reactions (pin / hide / evidence dwell)
    close the loop — the training data for learned ranking. Zero behavior
    change: the ledger observes the current gravity scorer, never alters it."""
    enabled: bool = _get("QUILL_ATTENTION_LEDGER", "1") not in ("0", "false", "False")
    # Re-log the same node on the same surface only after this long, unless its
    # score moved or it changed layer (focus <-> periphery) — keeps the 4s
    # version-poll refetch loop from writing a row per node per fetch.
    throttle_s: float = float(_get("QUILL_ATTENTION_THROTTLE_S", "1800"))
    # Score delta that counts as "moved" and defeats the throttle.
    rescore_delta: float = float(_get("QUILL_ATTENTION_RESCORE_DELTA", "0.08"))
    # A person asked about in chat counts as a MISS when the field hasn't
    # surfaced them within this window — the ground truth engagement can't see.
    miss_window_s: float = float(_get("QUILL_ATTENTION_MISS_WINDOW_S", "2700"))
    # Context snapshots (what app / when) are written at most this often.
    snapshot_every_s: float = float(_get("QUILL_ATTENTION_SNAPSHOT_S", "600"))
    # A1 priors-continuity replay: mean Kendall tau gate before Field v2.
    replay_gate: float = float(_get("QUILL_ATTENTION_REPLAY_GATE", "0.6"))
    replay_days: float = float(_get("QUILL_ATTENTION_REPLAY_DAYS", "7"))
    # Field v2 (Track A2 / constellation WS1): selects only the Scorer
    # (GravityScorer vs FieldV2Scorer) inside ranking.pipeline — never forks
    # selection. OFF by default; shadow stays logged either way; replay gate
    # (scripts/replay_attention) is the promotion condition.
    field_v2: bool = _get("QUILL_FIELD_V2", "0") not in ("0", "false", "False")
    # Working Memory (Track A3): MMR + hysteresis focus selection; grounding
    # WORKING SET + planner read the same slots. ON by default. QUILL_WM=0
    # makes the Selector pure top-k (no MMR/hysteresis); the Admitter still
    # enforces people/entity quotas — quotas are never an alternate path.
    wm: bool = _get("QUILL_WM", "1") not in ("0", "false", "False")
    # Track A4: online β updates from closed impressions. OFF by default —
    # priors stay frozen until the console shows healthy drift. Kill switch.
    learn: bool = _get("QUILL_ATTENTION_LEARN", "0") not in ("0", "false", "False")
    learn_lr: float = float(_get("QUILL_ATTENTION_LEARN_LR", "0.02"))
    learn_max_daily_drift: float = float(
        _get("QUILL_ATTENTION_LEARN_MAX_DRIFT", "0.05"))
    # Horizon strip (A4): calendar-heuristic predicted-next items.
    horizon: bool = _get("QUILL_HORIZON", "1") not in ("0", "false", "False")
    horizon_min_p: float = float(_get("QUILL_HORIZON_MIN_P", "0.5"))
    horizon_horizon_s: float = float(_get("QUILL_HORIZON_S", str(90 * 60)))
    # Meta-memory: auto-escalate U on at-risk commitments (D8 attention-only).
    meta_auto_urgency: bool = _get("QUILL_META_AUTO_URGENCY", "1") not in (
        "0", "false", "False")


@dataclass(frozen=True)
class EconomyConfig:
    """Memory economy (Track C) — lifecycle, retention scoring, compaction.

    Two-stage safety: `enabled` covers the OBSERVE side only (nightly retention
    scores + lifecycle metadata + storage-growth curve; never mutates content).
    `compaction` gates the MUTATING side (replace absorbed raw events with
    span-preserving stubs, original archived first) and stays OFF until the
    trust period ends — flipping it on is the C2 promotion decision."""
    enabled: bool = _get("QUILL_MEMORY_ECONOMY", "1") not in ("0", "false", "False")
    compaction: bool = _get("QUILL_COMPACTION", "0") not in ("0", "false", "False")
    # fresh -> absorbed once the extractor has seen the event AND it has aged
    # past this (days). Absorbed means "represented in a derived layer".
    absorb_after_days: float = float(_get("QUILL_ECONOMY_ABSORB_DAYS", "7"))
    # absorbed events older than this are compaction CANDIDATES (still needs
    # low retention + no open citing facts + compaction flag to actually run).
    compact_after_days: float = float(_get("QUILL_ECONOMY_COMPACT_DAYS", "45"))
    # Retention score below which an old absorbed event becomes a candidate.
    retention_threshold: float = float(_get("QUILL_ECONOMY_RETENTION_MIN", "0.35"))
    # Churn cap per sweep when compaction is on.
    compact_max_per_run: int = int(_get("QUILL_ECONOMY_COMPACT_MAX", "200"))
    # Storage-growth snapshots at most this often (seconds).
    growth_every_s: float = float(_get("QUILL_ECONOMY_GROWTH_S", str(20 * 3600)))
    # Sweep is due when the last run is older than this (boot-enqueue pattern).
    due_after_s: float = float(_get("QUILL_ECONOMY_DUE_S", str(20 * 3600)))
    # Plan 6.6: drop Lance rows for dismissed/superseded/evidence_removed facts
    # older than this many days; then run Lance optimize(). Kill with
    # QUILL_VECTOR_GC=0.
    vector_gc: bool = _get("QUILL_VECTOR_GC", "1") not in ("0", "false", "False")
    vector_gc_after_days: float = float(_get("QUILL_VECTOR_GC_DAYS", "30"))


@dataclass(frozen=True)
class PredictorsConfig:
    """Learned predictors (Track F) — next-contact / next-document / next-app.

    Console-only in v1: predictions render on /console/predictors and feed
    nothing else — no offers, no interruptions (anticipation.py and the shell
    own surfacing, each behind its own gate). Heuristic baselines ship first;
    a learned model may only take over via the bench promote gate (beat the
    active model on the held-out window) and can always be rolled back."""
    enabled: bool = _get("QUILL_PREDICTORS", "1") not in ("0", "false", "False")
    # Walk-forward evaluation: points inside this trailing window are held out;
    # everything earlier is the history the predictor may look at.
    holdout_days: float = float(_get("QUILL_PREDICTORS_HOLDOUT_DAYS", "7"))
    # Bench needs at least this many held-out decision points to judge a model.
    min_points: int = int(_get("QUILL_PREDICTORS_MIN_POINTS", "20"))
    # Boot-enqueue cadences (reflect_daily pattern).
    bench_due_s: float = float(_get("QUILL_PREDICTORS_BENCH_S", str(20 * 3600)))
    drill_due_s: float = float(_get("QUILL_HARDENING_DRILL_S", str(7 * 86400)))


@dataclass(frozen=True)
class PerceptionConfig:
    """Desktop perception subsystem (app/perception/).

    L0 is the always-on foreground-window METADATA stream (titles + input
    COUNTS only, never contents). L1 is change-triggered OCR (Phase B) —
    opt-in via QUILL_PERCEPTION_L1 (default off so today's VLM screen loop
    stays the producer until an explicit flip). Exactly one of L1 / the old
    `_analyze_screen` path may emit `desktop.screen` events. The cloud budget
    is the hard USD/day ceiling on ambient enrichment — see spend_cap.py;
    0 = uncapped, an explicit escape hatch that is never the default."""
    enabled: bool = _get("QUILL_PERCEPTION", "1") not in ("0", "false", "False")
    # Phase B: L1 OCR text layer replaces the VLM screen producer when on.
    l1_enabled: bool = _get("QUILL_PERCEPTION_L1", "0") not in (
        "0", "false", "False")
    poll_s: float = float(_get("QUILL_PERCEPTION_POLL_S", "1.0"))
    debounce_ms: int = int(_get("QUILL_PERCEPTION_DEBOUNCE_MS", "500"))
    heartbeat_s: float = float(_get("QUILL_PERCEPTION_HEARTBEAT_S", "60"))
    batch_commit_s: float = float(_get("QUILL_PERCEPTION_BATCH_S", "2.0"))
    idle_s: float = float(_get("QUILL_PERCEPTION_IDLE_S", "60"))
    gap_threshold_s: float = float(_get("QUILL_PERCEPTION_GAP_S", "5.0"))
    budget_usd_day: float = float(_get("QUILL_CLOUD_BUDGET_USD_DAY", "2.0"))
    # L1 trigger / OCR tunables (prompt defaults).
    l1_settle_ms: int = int(_get("QUILL_PERCEPTION_L1_SETTLE_MS", "700"))
    l1_dhash_every_s: float = float(_get("QUILL_PERCEPTION_L1_DHASH_S", "5"))
    l1_dhash_hamming: int = int(_get("QUILL_PERCEPTION_L1_HAMMING", "10"))
    l1_max_interval_s: float = float(_get("QUILL_PERCEPTION_L1_MAX_S", "120"))
    l1_min_conf: float = float(_get("QUILL_PERCEPTION_L1_MIN_CONF", "0.55"))
    l1_line_cache: int = int(_get("QUILL_PERCEPTION_L1_LINE_CACHE", "2000"))
    l1_scroll_overlap: float = float(
        _get("QUILL_PERCEPTION_L1_SCROLL_OVERLAP", "0.70"))
    # Phase C: L2 content-addressed frames (side-effect of L1; kill-switch off).
    l2_enabled: bool = _get("QUILL_PERCEPTION_L2", "1") not in (
        "0", "false", "False")
    thumb_max_px: int = int(_get("QUILL_PERCEPTION_THUMB_PX", "960"))
    thumb_quality: int = int(_get("QUILL_PERCEPTION_THUMB_Q", "55"))
    full_quality: int = int(_get("QUILL_PERCEPTION_FULL_Q", "80"))
    full_ttl_h: float = float(_get("QUILL_PERCEPTION_FULL_TTL_H", "72"))
    thumb_ttl_d: float = float(_get("QUILL_PERCEPTION_THUMB_TTL_D", "30"))
    disk_budget_gb: float = float(
        _get("QUILL_PERCEPTION_DISK_GB_YEAR", "15"))
    # Phase D: L3 async semantics (default off — soak before cutting over
    # screen_extract). When on, screen_extract is not scheduled.
    l3_enabled: bool = _get("QUILL_PERCEPTION_L3", "0") not in (
        "0", "false", "False")
    l3_idle_gap_s: float = float(_get("QUILL_PERCEPTION_L3_IDLE_S", "300"))
    l3_switch_gap_s: float = float(_get("QUILL_PERCEPTION_L3_SWITCH_S", "180"))
    l3_vlm_ocr_chars: int = int(_get("QUILL_PERCEPTION_L3_VLM_CHARS", "40"))

    @property
    def db_path(self) -> str:
        return f"{_get('QUILL_DATA_DIR', 'data')}/perception.db"

    @property
    def frames_dir(self) -> str:
        return f"{_get('QUILL_DATA_DIR', 'data')}/frames"

    @property
    def disk_budget_bytes(self) -> int:
        return int(max(0.0, self.disk_budget_gb) * (1024 ** 3))

    @property
    def export_dir(self) -> str:
        return _get("QUILL_PERCEPTION_EXPORT_DIR", "export")


@dataclass(frozen=True)
class ScoreConfig:
    """People v3 WS-B — connection score v2 (see services/score_v2.py).

    Weights live in data/score_config.json (fail-closed loader, surfaced in
    /health); these flags only control rollout and both default OFF.
    `shadow` runs the nightly v1-vs-v2 comparison job (report-only);
    `live_v2` switches /people/list ranking to v2, and even then only after
    cutover_ready() (7 consecutive clean nightlies)."""
    shadow: bool = _get("QUILL_SCORE_SHADOW", "0") not in ("0", "false", "False")
    live_v2: bool = _get("QUILL_SCORE_V2", "0") not in ("0", "false", "False")


@dataclass(frozen=True)
class PeopleEscrowConfig:
    """People v3 P3 (WS-A): voice-track escrow + retroactive rebind.

    When ON, facts extracted from an unbound diarization track ("Speaker N")
    are kept but escrowed against the track instead of being dropped (or
    minting a junk "Speaker N" person). Escrowed rows stay out of grounding,
    retrieval, people scoring and the constellation until the track is bound
    to a named person, at which point a durable rebind job rewrites them.
    Default OFF: behavior is byte-identical to the pre-escrow pipeline.
    """
    enabled: bool = _get("QUILL_PEOPLE_ESCROW", "0") not in ("0", "false", "False")


@dataclass(frozen=True)
class MintRecurrenceConfig:
    """People v3 P4 (WS-C): recurrence-gated person minting.

    When ON, an unmatched NAMED mention that would mint a new Person on first
    sight parks in the pending pool instead (person_mentions rows with
    resolution_status='pending_mint'); the Person is minted retroactively only
    once the same identity has been seen in >= `min_sessions` distinct
    sessions, adopting the pooled mentions so no signal is lost. Pending
    mentions that never recur are archived (status='pending_expired', never
    deleted) after `ttl_days`. Default OFF: behavior is byte-identical to the
    first-sight-minting pipeline. Composes with QUILL_PEOPLE_ESCROW, which
    handles UNNAMED speakers — this gate handles named-but-new mentions.
    """
    enabled: bool = _get("QUILL_MINT_RECURRENCE", "0") not in ("0", "false", "False")
    min_sessions: int = int(_get("QUILL_MINT_RECURRENCE_SESSIONS", "2"))
    ttl_days: float = float(_get("QUILL_MINT_RECURRENCE_TTL_DAYS", "30"))


@dataclass(frozen=True)
class ProvisionalBindConfig:
    """People v3 WS-D part 2: provisional-bind band + merge-as-training.

    When ON, a would-be create_new / leave_open / pending_mint whose best
    EXISTING candidate scores in [score_lo, score_hi] binds provisionally
    to that person instead of minting a twin or stalling. Mentions land as
    resolution_status='provisional'; a later human soft_merge confirms them
    into conclusive positive alias_rules (the training signal). Default OFF:
    byte-identical to the pre-band pipeline.
    """
    enabled: bool = _get("QUILL_PROVISIONAL_BIND", "0") not in (
        "0", "false", "False")
    score_lo: float = float(_get("QUILL_PROVISIONAL_BIND_LO", "0.55"))
    score_hi: float = float(_get("QUILL_PROVISIONAL_BIND_HI", "0.80"))


@dataclass(frozen=True)
class UsageConfig:
    """Pilot instrumentation — local usage ledger (WS-A).

    Counts *numbers only* (see services/usage_ledger.py): how many searches,
    chat turns, meetings, review verdicts — never a query, a fact, a name or a
    window title. Rows live in `usage_daily` in the main store, keyed by UTC
    day, so WAU / week-2 retention can be computed on the tester's own machine.

    Nothing leaves the box on its own. `ping_url` alone does nothing: the
    weekly POST additionally requires a consent flag the user stored through
    the Privacy controls (persisted in data/usage_consent.json, never an env
    var — consent is a user act, not a deployment setting).
    """
    enabled: bool = _get("QUILL_USAGE_LEDGER", "1") not in ("0", "false", "False")
    # Accumulator -> SQLite cadence. A crash loses at most this much counting.
    flush_s: float = float(_get("QUILL_USAGE_FLUSH_S", "60"))
    # Operator endpoint for the opt-in weekly ping. Empty = manual sharing only.
    ping_url: str = _get("QUILL_USAGE_PING_URL", "")
    # Ping cadence + the floor on retry after a failure (never more than one
    # attempt per day, and a failure is logged, never raised).
    ping_every_days: float = float(_get("QUILL_USAGE_PING_DAYS", "7"))
    ping_retry_days: float = float(_get("QUILL_USAGE_PING_RETRY_DAYS", "1"))
    ping_timeout_s: float = float(_get("QUILL_USAGE_PING_TIMEOUT_S", "5"))


@dataclass(frozen=True)
class UpdateCheckConfig:
    """Version manifest check (WS-C) — a notification, never an updater.

    An unconditional GET of a static JSON the operator hosts: no query params,
    no install id, no version header, so the only thing the operator learns is
    that some IP asked for a file. No auto-download, no auto-update. Off with
    QUILL_UPDATE_CHECK=0 (documented in TESTER_SETUP and toggleable in the
    Privacy controls).
    """
    enabled: bool = _get("QUILL_UPDATE_CHECK", "1") not in ("0", "false", "False")
    manifest_url: str = _get("QUILL_UPDATE_MANIFEST_URL", "")
    # Re-check cadence; the cached answer is served in between.
    every_hours: float = float(_get("QUILL_UPDATE_CHECK_HOURS", "24"))
    timeout_s: float = float(_get("QUILL_UPDATE_TIMEOUT_S", "3"))


@dataclass(frozen=True)
class ExportConfig:
    """Data export / backup (WS-B) — the "prove I can leave" path.

    Backups are streamed, never buffered (the 107 GB incident): a data dir can
    be far larger than RAM. `free_disk_fraction` is the size guard — refuse
    when the projected zip would eat more than this share of free space.
    """
    # Refuse a backup projected to exceed free_disk * this fraction.
    free_disk_fraction: float = float(_get("QUILL_EXPORT_DISK_FRACTION", "0.5"))
    # Zip streaming chunk size (bytes).
    chunk_bytes: int = int(_get("QUILL_EXPORT_CHUNK_BYTES", "1048576"))
    # Directory names under data/ that are never exported (secrets).
    #   .env lives at the repo root, .api_token/.mcp_token under data/.
    excluded: tuple[str, ...] = (
        ".env", ".credentials.env", ".api_token", ".mcp_token", "usage_consent.json")


@dataclass(frozen=True)
class LatencyConfig:
    """Stage-level latency spans (see services/latency.py).

    Phase 0 of the latency program: measure before optimizing. `model_log`
    records per-call wall time; this records where that time went — queue
    wait, cold model load, prefill, generation, retrieval, post-processing.

    Off by default. A program that begins by instrumenting must not begin by
    changing behavior, and the writer touches the request path on every trace.
    """
    enabled: bool = _get("QUILL_LATENCY_SPANS", "0") not in ("0", "false", "False")
    # Rows the console and the aggregator read from the tail of the trail.
    read_limit: int = int(_get("QUILL_LATENCY_READ_LIMIT", "20000"))


@dataclass(frozen=True)
class Settings:
    audio: AudioConfig = AudioConfig()
    system_audio: SystemAudioConfig = SystemAudioConfig()
    storage: StorageConfig = StorageConfig()
    speakers: SpeakerConfig = SpeakerConfig()
    speaker_env: SpeakerEnvConfig = SpeakerEnvConfig()
    vision: VisionConfig = VisionConfig()
    text_local: TextLocalConfig = TextLocalConfig()
    escalate_log: EscalateLogConfig = EscalateLogConfig()
    learning: LearningConfig = LearningConfig()
    exemplars: ExemplarConfig = ExemplarConfig()
    shadow: ShadowEvalConfig = ShadowEvalConfig()
    router: RouterConfig = RouterConfig()
    notifications: NotificationConfig = NotificationConfig()
    desktop_capture: DesktopCaptureConfig = DesktopCaptureConfig()
    ingest: IngestConfig = IngestConfig()
    audio_quality: AudioQualityConfig = AudioQualityConfig()
    denoise: DenoiseConfig = DenoiseConfig()
    asr_bias: AsrBiasConfig = AsrBiasConfig()
    telemetry: TelemetryConfig = TelemetryConfig()
    consolidation: ConsolidationConfig = ConsolidationConfig()
    facts: FactHygieneConfig = FactHygieneConfig()
    anticipation: AnticipationConfig = AnticipationConfig()
    worker: WorkerConfig = WorkerConfig()
    memory: MemoryConfig = MemoryConfig()
    first_run: FirstRunConfig = FirstRunConfig()
    exhaust: ExhaustConfig = ExhaustConfig()
    mcp: McpConfig = McpConfig()
    external_capture: ExternalCaptureConfig = ExternalCaptureConfig()
    onboarding: OnboardingConfig = OnboardingConfig()
    documents: DocumentsConfig = DocumentsConfig()
    phone: PhoneChannelConfig = PhoneChannelConfig()
    peer: PeerChannelConfig = PeerChannelConfig()
    org: OrgNodeConfig = OrgNodeConfig()
    icloud: IcloudConfig = IcloudConfig()
    voice: VoiceConfig = VoiceConfig()
    attention: AttentionConfig = AttentionConfig()
    economy: EconomyConfig = EconomyConfig()
    predictors: PredictorsConfig = PredictorsConfig()
    perception: PerceptionConfig = PerceptionConfig()
    score: ScoreConfig = ScoreConfig()
    people_escrow: PeopleEscrowConfig = PeopleEscrowConfig()
    mint_recurrence: MintRecurrenceConfig = MintRecurrenceConfig()
    provisional_bind: ProvisionalBindConfig = ProvisionalBindConfig()
    usage: UsageConfig = UsageConfig()
    update_check: UpdateCheckConfig = UpdateCheckConfig()
    export: ExportConfig = ExportConfig()
    latency: LatencyConfig = LatencyConfig()
    # Bind address. 127.0.0.1 keeps the unauthenticated local trust model.
    # 0.0.0.0 (phone / Tailscale) enables LanApiAuthMiddleware — set
    # QUILL_API_TOKEN or let startup write data/.api_token, then unlock at /auth.
    host: str = _get("QUILL_HOST", "127.0.0.1")
    port: int = int(_get("QUILL_PORT", "8000"))


settings = Settings()
