# QUILL — Program Breakdown

*Status snapshot — July 07, 2026*

## What it is

QUILL is a **personal "memory + agent" system**: a laptop prototype of a wearable (a "pen") that continuously **listens** (mic), **sees** (webcam), **remembers** (searchable timeline), and then **acts** on what it perceived by driving a real web browser. The tagline in the code is the *"hear → act loop"*: QUILL overhears "I'll send Justin the pricing follow-up," files it, and later — when you ask, or proactively — hands that task to an autonomous browser agent that drafts the email, pausing for your approval before anything irreversible.

It's built as a series of **milestones** (M1–M6). M1–M3 are fully built; M4 (the conversational/agent brain) is what's been getting wired up; M5–M6 are aspirational.

## The architecture

Every input modality is independent and normalizes what it perceives into a single `Event` (`app/events.py`), pushed onto an in-process **EventBus** (async pub/sub). Subscribers react without knowing about each other:

```
  Webcam ─▶ Vision pipeline ─┐
Laptop mic ─▶ Audio pipeline ─┤
                             ├─▶ EventBus ─▶ Memory Engine ─▶ (SQLite + LanceDB)
                             │                     ▲
        to-do watcher ◀──────┘                     │ semantic search
                     │                             │
                     ▼                    Browser Agent ◀── /chat
              proactive offer ──────────────▶ (memory-grounded, acts on the web)
```

The `Event` schema is the lingua franca — `time, modality, raw, summary, source, confidence, people, tasks, entities, meta`. Everything downstream (memory, search, the agent, the console) speaks `Event`.

## The subsystems

### 1. Audio pipeline — M1 (`app/services/audio.py`)
Mic → **Silero VAD** (voice-activity detection, ONNX) segments continuous audio into utterances → **faster-whisper** (CTranslate2 Whisper, no PyTorch) transcribes each utterance → `Event`.
- Capture runs on the sounddevice audio thread doing only cheap work (VAD + buffering); transcription happens on a separate worker thread so audio frames are never dropped.
- **Ingest hygiene** (`app/services/ingest_filter.py`): drops Whisper hallucinations (e.g. repeated "Thank you." across silence) and confident duplicates before they pollute memory, using per-segment `avg_logprob`/`no_speech_prob`.
- **Speaker ID** (`app/services/speakers.py`): **SpeechBrain ECAPA-TDNN** voice embeddings. Anonymous out of the box (utterances cluster into "Speaker 1/2…" by cosine similarity); named once you enroll someone (`scripts/enroll_speaker.py`). Voiceprints persist as `.npy`.

### 2. Vision pipeline — M2 (`app/services/vision.py`, `app/services/vlm.py`)
Webcam → OpenCV frame selection → **Claude vision (Opus 4.8)** structured extraction → `Event`.
- **Frame selection** (cost control): capture continuously, analyze a frame only when the scene *changes* (mean absolute pixel diff over a threshold on a downscaled grayscale image), rate-limited to ≥`min_interval_s`, and forced at least every `max_interval_s`.
- The VLM returns a **structured JSON schema**: description, verbatim OCR text, people count, objects, scene type — plus **page understanding**: it classifies a shown page as `todo_list / questions / notes / table / code / diagram …` and transcribes the discrete `items`. That `todo_list` classification is what fires the proactive loop.

### 3. Memory Engine — M3 (`app/services/memory.py`)
The persistence + retrieval layer. Subscribes to the bus; every `Event` is:
- **Written to SQLite** (`app/storage.py`) — durable timeline that reloads on startup.
- **Embedded and indexed** in **LanceDB** (`app/vectorstore.py`) using local **sentence-transformers** (`all-MiniLM-L6-v2`, 384-d, CPU) for **semantic search** — "what did I see on the whiteboard?" matches a frame described as "Series A timeline." Falls back to substring search if semantic is disabled/unavailable. Un-indexed events are backfilled on startup.
- Raw artifacts (WAV clips, JPEG frames) are saved to disk and linked from `meta.audio_path`/`meta.frame_path` for **provenance**.
- (`app/services/consolidation.py` handles memory consolidation/summarization.)

### 4. The Brain / Agent layer — M4
- **`/chat`** (`app/api/routes.py`) dispatches a turn to the **browser agent** via `app/services/agent_bridge.py` — a lazily-started `AgentWorker` owning one persistent browser agent on its own thread (sync Playwright must live on one thread). Non-blocking: enqueue → poll `/chat/poll` for progress/results/approval prompts → answer via `/chat/answer`. `QUILL_AGENT=0` reverts to the memory-only retriever (`app/services/llm.py` stub).
- **Proactive to-do watcher** (`app/services/todo_watcher.py`): subscribes to the bus; when vision classifies a page as `todo_list`, it *offers in chat* to run the items through the agent (debounced by an items-hash, 5-min cooldown). You reply yes/no; on yes each item becomes a memory-grounded agent goal. This is the fully autonomous "see → offer → act" trigger.
- **Memory grounding**: the agent gets a `memory_provider` that semantic-searches QUILL's own timeline, so "follow up on what Marc said about pricing" pulls the "$49/mo" it overheard — no re-explaining.

### 5. The browser agent — "Exec.AI" (`browser_agent/`)
A self-contained, Anthropic-backed autonomous web agent (also runnable standalone via `exec_webapp.py` / `Exec.AI_v1/`). Its `Agent.run_goal()` (`browser_agent/orchestrator.py`):
- **Routes** the request (answer directly vs. drive the browser) using a QUILL "envelope" (intent, requires_browser, requires_approval, site).
- **Plans → executes → verifies** an observe→act loop over deterministic Playwright actions (`browser_agent/tools.py`: click, type, select, navigate, read, ask_human, request_approval, done).
- **Tiered models** (`browser_agent/config.py`): Sonnet for routing/execution, **Opus** for planning/escalation, **Haiku** for verification.
- **Learning layer** (`browser_agent/memory.py`): recalls what worked for `intent@site` on past runs to shorten plans and avoid repeat mistakes.
- **Safety**: irreversible steps (send/buy/delete) gate behind human approval.

### 6. Memory Console (`app/api/routes.py` `/console`)
A read-only web window onto the timeline: recent captures, search, speaker labels, confidence, low-confidence filtering, and a link from every memory back to its **source clip/frame** (served safely via `/artifact`, path-confined to the data dir). This is the trust/inspection layer.

## The tools & tech stack

| Layer | Tool | Role |
|---|---|---|
| **API / server** | FastAPI + Uvicorn, Pydantic | HTTP surface, async event loop |
| **Config** | python-dotenv | env/`.env` settings |
| **Audio capture** | sounddevice, NumPy | low-latency mic |
| **VAD** | silero-vad (ONNX) | utterance segmentation |
| **ASR** | faster-whisper (CTranslate2) | transcription, no GPU/torch |
| **Speaker ID** | SpeechBrain ECAPA-TDNN | diarization + named voiceprints |
| **Vision capture** | OpenCV | webcam + frame selection |
| **Vision/VLM** | Anthropic Claude (Opus 4.8) | structured frame understanding |
| **Embeddings** | sentence-transformers (MiniLM, local) | semantic search vectors |
| **Vector store** | LanceDB (embedded, file-based) | meaning-based retrieval |
| **Timeline store** | SQLite | durable events + provenance |
| **Browser agent** | Playwright (Chromium) + Claude (Sonnet/Opus/Haiku) | autonomous web actions |
| **Agent UI** | Flask | browser-agent web chat |
| **(optional) dashboard** | Streamlit | listed, optional |

Design ethos throughout: **local/CPU-first** (VAD, ASR, embeddings all run without a GPU or PyTorch); **Claude only where reasoning/vision is needed**; **cost control** at every model boundary (frame selection, tiered agent models); **graceful degradation** (missing API key or camera degrades a feature, never crashes the pipeline).

## Entry points

| Command | What runs |
|---|---|
| `python run_all.py` | **Everything**: memory + audio + vision + FastAPI (`:8000/docs`) + browser-agent UI (`:5000`). Capture runs in-process; the agent runs as a child process (Playwright needs its own process, isolated from the async server). |
| `python run_audio.py` | M1 only — live transcription in the terminal |
| `python run_vision.py` | M2 only — live webcam understanding |
| `uvicorn app.main:app` | API server alone |
| `python exec_webapp.py` | Browser agent standalone (with QUILL memory bridge) |

`app/main.py` startup wires it together: binds the event loop, attaches the Memory Engine and the to-do watcher, and optionally auto-starts capture (`QUILL_AUTOSTART=1`, set by `run_all.py`).

## Where things stand

- **Built & working:** M1 (audio), M2 (vision), M3 (semantic memory), the browser agent, and — new — the M4 `/chat`→agent bridge and the proactive to-do watcher, which together close the hear/see→act loop.
- **Stub / aspirational:** voice conversation/TTS (`app/services/voice.py`), the M5 knowledge graph (people/orgs/commitments), and the broader M6 agent surface (email/calendar/CRM). The requirements file lists the target stack (pyannote, elevenlabs, neo4j) as commented future deps.

> **Docs drift note:** the README's "Layout" section still describes memory as *"in-memory for now"* and `/chat` as a stub — both are now out of date (memory is SQLite+LanceDB; `/chat` dispatches to the agent).
