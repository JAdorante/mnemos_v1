# vinceo.ai

**A personal "memory + agent" system that listens, sees, remembers, reflects, and acts.**

![status](https://img.shields.io/badge/status-experimental%20prototype-orange)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows%20(primary)%20·%20macOS%2FLinux-lightgrey)
![local-first](https://img.shields.io/badge/inference-local%2FCPU--first-brightgreen)
![license](https://img.shields.io/badge/license-unspecified-lightgrey)

vinceo.ai is a laptop prototype of a wearable (a "pen") that continuously **hears**
(microphone) and **sees** (webcam) — and, on Windows, mirrors your **phone
notifications** — distills what it perceives into a searchable, provenance-linked
memory, extracts the **tasks, commitments, and claims** buried in ordinary
conversation, reflects on them into durable **insights**, and then — with your
approval — **acts** on them by driving a real web browser, your desktop, or Phone Link.

The tagline in the code is the **hear → act loop**: vinceo.ai overhears
*"I'll send Justin the pricing follow-up,"* files it as a commitment with a
verbatim source quote, and later — when you ask, or when it proactively offers —
hands that task to an autonomous browser agent that drafts the email and pauses
for your approval before anything irreversible happens.

> The laptop is the hardware prototype: prove the software experience first, shrink
> it into a pen later.

> **Status:** Experimental research prototype, actively developed. Local capture
> (audio/vision/notifications), memory, facts, reflection, the knowledge graph, and
> the browser/desktop agents all work today. Some pieces are stubs or feature-flagged
> off — see [Known gaps & roadmap](#known-gaps--roadmap). Not production software.

---

## Table of contents

- [Why this exists](#why-this-exists)
- [Quick start](#quick-start)
- [Usage examples](#usage-examples)
- [The big picture](#the-big-picture)
- [Architecture](#architecture)
- [Milestones](#milestones)
- [The subsystems in detail](#the-subsystems-in-detail)
  - [1. Capture spine — audio (M1)](#1-capture-spine--audio-m1)
  - [2. Capture spine — vision (M2)](#2-capture-spine--vision-m2)
  - [3. Capture spine — phone notifications (Windows)](#3-capture-spine--phone-notifications-windows)
  - [4. Memory Engine (M3)](#4-memory-engine-m3)
  - [5. Consolidation & the durable job queue](#5-consolidation--the-durable-job-queue)
  - [6. Facts layer — Track A](#6-facts-layer--track-a)
  - [7. Reflection — durable intelligence](#7-reflection--durable-intelligence)
  - [8. Knowledge graph (M5 v1)](#8-knowledge-graph-m5-v1)
  - [9. Personal Agent Layer — the Planner](#9-personal-agent-layer--the-planner)
  - [10. Browser agent — "Exec.AI"](#10-browser-agent--execai)
  - [11. Desktop agent](#11-desktop-agent)
  - [12. Phone Link agent](#12-phone-link-agent)
  - [13. Proactive watchers](#13-proactive-watchers)
  - [14. Model router & model log](#14-model-router--model-log)
- [Interfaces & API](#interfaces--api)
- [Tech stack](#tech-stack)
- [Configuration reference](#configuration-reference)
- [Where data lives](#where-data-lives)
- [Project layout](#project-layout)
- [Development & testing](#development--testing)
- [Security model](#security-model)
- [Design ethos](#design-ethos)
- [Known gaps & roadmap](#known-gaps--roadmap)
- [License & status](#license--status)

---

## Why this exists

Most "AI assistants" start from a blank chat box: you have to tell them everything,
every time. vinceo.ai inverts that. It is **ambient** — it captures the context of your
day as it happens (what was said, what you showed the camera, what your phone buzzed
about) and turns that raw stream into structured, auditable memory. Because it already
knows *"Marc quoted $49/mo"* and *"you promised Justin a follow-up,"* acting on your
behalf doesn't require re-explaining anything.

You should know in ten seconds whether this is for you: **vinceo.ai is for building an
always-on personal memory that can eventually act.** If you want a from-scratch chatbot,
this isn't it. If you want a local-first system that remembers your real life and can
draft-and-send with your approval, read on.

Three principles run through every layer:

- **Local/CPU-first.** Voice-activity detection, speech-to-text, speaker ID, and
  embeddings all run on the CPU with no GPU and no PyTorch. Vision defaults to a
  **local** VLM (Ollama). A paid Claude call happens only where reasoning or
  high-stakes vision genuinely needs it.
- **Provenance everywhere.** Every memory links back to the raw audio clip or
  video frame it came from. Every extracted fact carries a verbatim `source_span`
  quote and a pointer to its source event. Nothing is a black box you have to trust.
- **Memory is context, never command authority.** vinceo.ai's memory is grounded in
  microphone and camera content, which an attacker could influence (say something
  in the room, hold up a sign to the camera). So retrieved memory can *inform* an
  action but can **never approve** one — only a live human reply authorizes anything
  irreversible.

---

## Quick start

### 1. Prerequisites

- **Python 3.10+** (uses `X | None` type hints).
- **Windows 11** is the primary target (audio, vision, and Phone Link notification
  mirroring are all developed there). macOS/Linux run the capture + memory + agent
  stack too; Phone Link and Windows-toast capture are Windows-only and degrade
  gracefully elsewhere.
- A working **microphone** and **webcam** for live capture (optional — you can run
  headless and drive it over the API).
- An **Anthropic API key** for Claude vision, fact extraction, reflection, and the
  browser agent. Everything local (VAD, ASR, speaker ID, embeddings, local VLM)
  runs without it; the paid features simply skip if the key is missing.

### 2. Install

```powershell
# from the project root
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # Windows PowerShell
pip install -r requirements.txt
```

The M1 audio stack needs **no GPU**: `faster-whisper` (CTranslate2) + `silero-vad`.

**Pre-download the speech models** so the first live run starts instantly instead of
pulling ~460 MB mid-session (cached under `~/.cache/huggingface`, one-time):

```powershell
python scripts/download_models.py            # the configured Whisper model + VAD
python scripts/download_models.py base small # or specific sizes
```

**One-time browser setup** (for the agent):

```powershell
playwright install chromium
```

### 3. Add your API key

Put it in a `.env` (or a `.credentials.env`) at the project root — **never commit
this file** (`.gitignore` already excludes both):

```dotenv
# .env
ANTHROPIC_API_KEY=sk-ant-...
# optional: enables the Gemini vision backend
GOOGLE_API_KEY=...
```

…or export it for the session:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Without a key, the system degrades gracefully — frames are still captured and saved,
just not analyzed by Claude; extraction and reflection are skipped — it never crashes.

### 4. (Optional) local-first vision

To keep vision off the paid path, install [Ollama](https://ollama.com) and pull the
default local model:

```powershell
ollama pull minicpm-v
```

If Ollama isn't running, vision automatically falls back to Claude — so this step is
optional and safe to skip.

### 5. Run everything

```powershell
python run_all.py
```

That single command starts the Memory Engine, live audio + vision + notification
capture, the FastAPI server, and the Exec.AI browser-agent UI. Then open:

- **Memory Console** — <http://127.0.0.1:8000/console>
- **Live chat UI** — <http://127.0.0.1:8000/ui>
- **API docs (Swagger)** — <http://127.0.0.1:8000/docs>
- **Browser-agent UI** — <http://127.0.0.1:5000>

`Ctrl+C` stops everything, including the agent's Chromium (killed as a process tree).

**Flags:** `--no-audio` · `--no-vision` · `--no-notifications` · `--no-browser` ·
`--browser-headless` · `--port` · `--browser-port` · `--host`.

---

## Usage examples

### Run one piece at a time

```powershell
python run_audio.py                 # M1 only — live transcription in the terminal
python run_vision.py                # M2 only — live webcam understanding
uvicorn app.main:app --reload       # API server alone
python exec_webapp.py               # browser agent standalone (with vinceo.ai memory bridge)
```

`run_audio.py` prints finalized utterances as `[transcript]` lines (`Marc: ...`
once enrolled); `Ctrl+C` prints a session summary.

### Teach it who's talking

Speaker ID is anonymous out of the box (`Speaker 1`, `Speaker 2`, …). Enroll a name
once and every future utterance from that voice is labeled:

```powershell
python scripts/enroll_speaker.py Marc          # record 10s of Marc from the mic
python scripts/enroll_speaker.py Justin 15     # 15 seconds
python scripts/enroll_speaker.py Marc clip.wav # or from a 16 kHz mono WAV
```

### The hear → act loop, end to end

1. **Speak** near the laptop: *"I still owe Justin the pricing follow-up — Marc said
   forty-nine a month."*
2. vinceo.ai transcribes it, attributes the speakers, and stores it with the WAV clip.
3. The extractor distills a **commitment** (*owe Justin a follow-up*) and a **claim**
   (*$49/mo*), each with the verbatim quote and a link to the audio.
4. In the **Console** you approve the commitment (or fix a misheard name).
5. In **chat**, you ask vinceo.ai to act:

```powershell
# Kick off a goal (non-blocking) — the agent routes it and drives the browser
$r = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/chat `
     -ContentType application/json `
     -Body '{"message": "email Justin the pricing follow-up"}'
$since = $r.since

# Poll for progress, results, and approval prompts
Invoke-RestMethod -Uri "http://127.0.0.1:8000/chat/poll?since=$since"

# When it surfaces an approval packet, answer it
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/chat/answer `
     -ContentType application/json -Body '{"text": "approve"}'
```

The agent pulls *"$49/mo"* straight from memory (no re-explaining), drafts the email,
and **pauses at a source-grounded approval packet** — Action / To / Subject / Body /
**Why / Source** — with **Approve / Edit / Cancel**. Nothing is sent until you say so.

The equivalent with `curl`:

```bash
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "email Justin the pricing follow-up"}'

curl -s "http://127.0.0.1:8000/chat/poll?since=0"
```

### Cap how far a run may go (safe demos)

Prefix a message with a dry-run level, or set `AGENT_DRY_RUN` globally:

```jsonc
{ "message": "/draft reply to Marc about the Series A timeline" }
// levels: /plan · /navigate · /draft · /approval (default) · /full
```

`/draft` prepares everything but **stops at the first commit gate without prompting** —
perfect for a demo that must never actually send.

### Search your memory

```bash
curl -s "http://127.0.0.1:8000/memory/search?q=whiteboard%20series%20A"
```

Semantic search: *"what did I see on the whiteboard?"* matches a frame vinceo.ai
described as *"Series A timeline"* even though the words don't overlap.

---

## The big picture

vinceo.ai is built as a stack of layers, each of which only depends on the one below it:

```
  ┌──────────────────────────────────────────────────────────────┐
  │  ACT      Browser agent (Exec.AI) · Desktop agent · Phone Link│  ← acts on the world, gated by approval
  ├──────────────────────────────────────────────────────────────┤
  │  DECIDE   Personal Agent Layer (Planner) — goal → ActionPacket│  ← plans, grounds, classifies risk
  ├──────────────────────────────────────────────────────────────┤
  │  REASON   Knowledge graph · Reflection · Facts (tasks/…)      │  ← what it *means*
  ├──────────────────────────────────────────────────────────────┤
  │  REMEMBER Memory Engine (SQLite timeline + LanceDB semantic)  │  ← durable, searchable
  ├──────────────────────────────────────────────────────────────┤
  │  PERCEIVE Audio (M1) · Vision (M2) · Phone notifications      │  ← raw perception → Event
  └──────────────────────────────────────────────────────────────┘
```

---

## Architecture

Every input modality is independent. Each normalizes what it perceives into a
single **`Event`** (see [app/events.py](app/events.py)) and pushes it onto an
in-process **`EventBus`** (a minimal async pub/sub). Subscribers react without
knowing about each other:

```
   Webcam ─▶ Vision pipeline ─┐
Laptop mic ─▶ Audio pipeline ─┤
    Phone ─▶ Notif. pipeline ─┤
                              ├─▶ EventBus ─▶ Memory Engine ─▶ SQLite + LanceDB
                              │                    │
   to-do / phone watcher ◀───┘                    │ semantic search
        │                                          │
        ▼            consolidate ─▶ extract facts ─▶ rebuild graph ─▶ reflect (daily)
  proactive offer                    (durable background worker)
        │                                          │
        ▼                                          ▼
   /chat ──▶ Planner ──▶ Browser / Desktop / Phone agent (memory-grounded, approval-gated)
```

The `Event` schema is the lingua franca — `time, modality, raw, summary, source,
confidence, people, tasks, entities, meta`. Everything downstream (memory, search,
facts, reflection, the graph, the agents, the console) speaks `Event`. `Modality` is
one of `audio`, `vision`, `notification`, `input`, or `system`.

The bus supports both async producers (`publish`) and synchronous ones on other
threads (`publish_nowait`, used by the audio capture and notification-poll threads),
so capture never blocks on downstream processing.

---

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| **M1** | Live audio pipeline — mic → VAD → Whisper → transcript, with ingest hygiene + speaker ID | ✅ built |
| **M2** | Live vision pipeline — webcam → frame selection → VLM (local-first, Claude fallback) | ✅ built |
| **M3** | Persistent + semantic memory — SQLite timeline + WAV/JPEG provenance + LanceDB embeddings | ✅ built |
| **Track A** | Facts layer — episodic events distilled into tasks / commitments / claims with a review loop | ✅ built |
| **Reflection** | Daily facts → grounded, reviewable insights (change / risk / open-loop / recommendation) | ✅ v1 built |
| **M4** | The brain — `/chat` dispatch to agents, memory-grounded, proactive offers | ✅ built (TTS still stub) |
| **M5** | Knowledge graph — people / orgs / commitments as a traversable graph | 🟡 v1 (relations + traversal) |
| **Phase 5** | Personal Agent Layer — compile goals into grounded, risk-classified ActionPackets | 🟡 v1 slice (behind `QUILL_PLANNER=1`) |
| **Phone** | Windows Phone Link — mirror iPhone notifications, send SMS with approval | ✅ built (Windows-only) |
| **M6** | Broader agent surface — email / calendar / CRM connectors | 🟡 browser agent drives these UIs directly |

Legend: ✅ built & working · 🟡 partially realized · ⬜ aspirational.

---

## The subsystems in detail

### 1. Capture spine — audio (M1)

**File:** [app/services/audio.py](app/services/audio.py) · **Speaker ID:**
[app/services/speakers.py](app/services/speakers.py) · **Hygiene:**
[app/services/ingest_filter.py](app/services/ingest_filter.py)

Microphone → **Silero VAD** (ONNX voice-activity detection) segments the continuous
stream into utterances → **faster-whisper** (CTranslate2 Whisper, no PyTorch)
transcribes each utterance → `Event`.

- **Two-thread design.** Capture runs on the `sounddevice` audio thread doing only
  cheap work (VAD + buffering); transcription runs on a separate worker thread, so
  audio frames are never dropped waiting on the ASR.
- **Ingest hygiene.** Whisper hallucinates on silence (endless *"Thank you."*,
  *"Thanks for watching."*). The ingest filter uses per-segment `avg_logprob` and
  `no_speech_prob` to hard-drop hallucinations and confident duplicates before they
  pollute memory, and flags (but keeps) low-confidence utterances so the Console can
  surface them. Fully tunable; disable with `QUILL_INGEST_FILTER=0`.
- **Speaker ID.** **SpeechBrain ECAPA-TDNN** voice embeddings. Anonymous out of the
  box — utterances cluster into `Speaker 1`, `Speaker 2`, … by cosine similarity.
  Named once you enroll someone (`python scripts/enroll_speaker.py Marc`); the name
  is then written onto the event (`people`, `meta.speaker`). Voiceprints persist as
  `.npy` files. Tune `QUILL_SPEAKER_CLUSTER_THRESHOLD` / `QUILL_SPEAKER_ID_THRESHOLD`
  if it over- or under-merges.

### 2. Capture spine — vision (M2)

**Files:** [app/services/vision.py](app/services/vision.py),
[app/services/vlm.py](app/services/vlm.py) (Claude),
[app/services/vlm_gemini.py](app/services/vlm_gemini.py) (Gemini alternative)

Webcam → OpenCV frame selection → VLM structured extraction → `Event`.

- **Frame selection (cost control).** Capture continuously, but analyze a frame only
  when the scene *changes* — mean absolute pixel difference over a threshold on a
  downscaled grayscale image — rate-limited to at most once every `min_interval_s`,
  and forced at least every `max_interval_s`. Dark/covered-lens frames below a
  brightness floor are skipped so a black frame never burns a VLM call.
- **Local-first VLM, Claude as paid fallback.** Every selected frame goes to a
  **local** model via Ollama (`minicpm-v` by default — strong local OCR).
  Claude (Opus 4.8) is called only for **high-stakes pages** (`todo_list` / `form` /
  `code`) or when the local model reports low confidence (`escalate_min_conf`). If
  Ollama isn't reachable, it falls back to Claude automatically. Set
  `QUILL_VISION_LOCAL=0` to always use Claude.
- **Structured output.** The VLM returns a JSON schema: description, verbatim OCR
  text, people count, objects, scene type — plus **page understanding**: it
  classifies a shown page as `todo_list / questions / notes / table / code /
  diagram …` and transcribes the discrete `items`. That `todo_list` classification
  is what fires the proactive loop (§13).
- **Windows camera quirks handled.** On Windows the default MSMF backend often fails
  (`E_UNEXPECTED`); vinceo.ai uses **DirectShow** (`dshow`) and forces **MJPG** capture
  to avoid mis-strided green/noise frames. All overridable.

### 3. Capture spine — phone notifications (Windows)

**Files:** [app/services/notifications.py](app/services/notifications.py) (capture),
[app/services/phone_link.py](app/services/phone_link.py) (control),
[app/services/phone_watcher.py](app/services/phone_watcher.py) (proactive)

Microsoft ships no public Phone Link API, so vinceo.ai reads iPhone notifications the way
they actually surface: as ordinary **Windows toast notifications** from the "Phone
Link" app, via `UserNotificationListener` (`winsdk`). Each becomes an `Event` with
`Modality.NOTIFICATION`, flowing into the same memory pipeline as speech and vision.

- **App-filtered.** By default only Phone Link / "Link to Windows" / "Your Phone"
  toasts are ingested (`QUILL_NOTIFICATIONS_PHONE_LINK_ONLY=1`); widen with
  `QUILL_NOTIFICATION_APPS=phone link,slack`.
- **One-time OS grant.** Windows Settings → Privacy & security → Notifications →
  enable notification access for Python.
- **Windows-only, off elsewhere.** `QUILL_NOTIFICATIONS` defaults on for Windows, off
  everywhere else; the pipeline no-ops cleanly on other platforms.

Sending back out (SMS) is handled by the [Phone Link agent](#12-phone-link-agent).

### 4. Memory Engine (M3)

**Files:** [app/services/memory.py](app/services/memory.py),
[app/storage.py](app/storage.py) (SQLite),
[app/vectorstore.py](app/vectorstore.py) (LanceDB),
[app/services/embeddings.py](app/services/embeddings.py)

The persistence + retrieval layer. It subscribes to the bus; every `Event` is:

- **Written to SQLite** (`data/quill.db`) — a durable timeline that reloads on
  startup, so transcripts and frames survive restarts.
- **Embedded and indexed in LanceDB** using local **sentence-transformers**
  (`all-MiniLM-L6-v2`, 384-d, CPU) for **semantic search**. Falls back to substring
  search if semantic is disabled or unavailable. Un-indexed events are backfilled
  automatically on startup.
- **Linked to its raw artifact** — WAV clips and JPEG frames are saved to disk and
  referenced from `meta.audio_path` / `meta.frame_path` for provenance.

### 5. Consolidation & the durable job queue

**Files:** [app/services/consolidation.py](app/services/consolidation.py),
[app/services/worker.py](app/services/worker.py)

- **Consolidation** merges adjacent utterances into conversational **turns** (a new
  turn starts after a silence gap exceeds `QUILL_CONSOLIDATE_MAX_GAP_S`), so facts
  are extracted from whole thoughts rather than fragments.
- **One queue, one worker.** A `jobs` table plus a single background worker thread
  drains processing (`consolidate` → `extract` → `graph`, plus time-driven
  `reflect_daily`) off the capture/request path. It survives crashes, coalesces
  bursts (a flurry of new audio queues exactly one pending re-consolidation, not
  dozens), and drains a backlog in small batches. **No Celery, no Redis.** The chain
  is wired in [app/main.py](app/main.py) at startup.

### 6. Facts layer — Track A

**Files:** [app/services/extractor.py](app/services/extractor.py),
[app/services/resolution.py](app/services/resolution.py), facts routes in
[app/api/routes.py](app/api/routes.py)

The heart of "captures **and** understands." Episodic events are distilled into
structured facts:

- **Extractor.** A windowed pass over *settled* turns produces **tasks**,
  **commitments**, and **claims** via Claude structured output (one swappable
  `EXTRACTOR_MODEL`). Every fact carries a `source_span` (verbatim quote) and a
  `source_event_id`.
- **Person resolution.** A cascade — exact match → prefix (Chris/Christopher) →
  embedding cosine similarity — resolves who a fact is about. Fuzzy merges are
  recorded as aliases rather than silently collapsed.
- **Status lifecycle.** Tasks move through `open → done → cancelled`. Vision to-do
  items become task facts too.
- **The review loop *is* the training layer.** The Console exposes every fact with
  **approve / edit / dismiss / done** controls and inline playback of the source
  audio clip. Correcting a misattributed speaker, killing a hallucinated commitment,
  or confirming a real one is the human signal that makes the agent trustworthy
  enough to act. Facts are also indexed into the shared LanceDB, so they're
  searchable alongside raw episodes.

### 7. Reflection — durable intelligence

**File:** [app/services/reflector.py](app/services/reflector.py) · **storage:**
`reflections` + `reflection_items` tables

The extractor answers *"what was said?"*; reflection answers *"what changed, what
matters, what is unresolved, and what should happen next?"* over a period. It reads
the facts/tasks/commitments the pipeline already produced and emits structured,
individually-reviewable **insights**.

- **Grounded.** The model may only cite fact ids it was handed; any invented id is
  dropped before persistence (`_ground`). No ungrounded oracle.
- **Reviewable.** Each insight is one `reflection_items` row — approve / edit /
  dismiss / **convert-to-task**, exactly like a fact. Nothing auto-mutates your tasks;
  a recommendation becomes a task only when a human converts it.
- **Insight taxonomy.** `change · pattern · risk · open_loop · project_update ·
  relationship_update · policy · recommendation`. A `policy` is a *tentative* learned
  preference about how you work (e.g. "prefers concise investor emails"), always
  low-confidence until you accept it.
- **Time-driven, not capture-driven.** A daily reflection auto-enqueues on startup if
  the last one is stale (>20h); there's no cron yet. Run one on demand via
  `POST /reflect/run` or `python scripts/test_reflection.py`. Gated by `QUILL_REFLECT`.

### 8. Knowledge graph (M5 v1)

**File:** [app/services/graph.py](app/services/graph.py) · **storage:**
`relations` table in [app/storage.py](app/storage.py)

Turns the nodes the facts pipeline already produces (people, facts, events) into a
traversable graph — **deterministically, with no LLM calls** — so vinceo.ai can answer
*relational* questions that flat text search can't.

- **`rebuild()`** recomputes edges from existing signal: typed person↔fact edges
  (`responsible_for` / `committed` / `owed`), name-mention edges (`mentioned_in`),
  provenance edges (`evidenced_by`), and weighted co-occurrence edges (`co_occurs`).
  Derived edges are wiped and recomputed on each rebuild; *asserted* extractor edges
  (e.g. `works_at`) are preserved.
- **`context_for_person(name)`** walks those edges to answer *"who is this, what's
  open with them, and who do they come up with"* in one traversal.
- **API:** `GET /graph/context?name=…` (lazy-builds on first call),
  `POST /graph/rebuild`, `GET /graph/stats`.

### 9. Personal Agent Layer — the Planner

**Files:** [app/services/agent_planner.py](app/services/agent_planner.py),
[app/services/agent_log.py](app/services/agent_log.py) (the recorder / `ActionPacket`)

The brain that sits *above* the hands. It compiles a user goal into a grounded,
risk-classified **Plan** of `ActionPacket`s before anything touches a browser:

```
Facts / Graph / Reflections / Commitments
        │  select_context()  (the 3 memories, not 30)
        ▼
PersonalAgentLayer.compile(goal) → Plan
   1. select_context   2. decompose   3. per step: choose compiler → ActionPacket
   4. classify_risk    (read/draft = low … send/buy = high, delete = blocked)
        │
        ▼
Execution surfaces (browser / desktop / phone) → human approval → Recorder logs the verdict
```

- **Risk table, not an LLM guess.** A precise, inspectable table decides
  approval: `blocked` never reaches a surface; `high` always forces the gate; any
  brush with a sensitive domain (medical / financial / password / …) escalates.
- **Cognitive agents as plug-ins.** An `IntentCompiler` turns *(goal, context)* into
  an `ActionPacket`. Two ship today: a **Writing Agent** (drafts email/message bodies
  from memory instead of asking "what should I say?") and a **Meeting Agent** (a
  read-only pre-meeting briefing grounded in the relationship graph). Adding another
  is a single `register()` call.
- **Best-effort & reversible.** Behind `QUILL_PLANNER=1` (default **off**). If the
  Planner is off or errors, callers fall back to handing the raw goal to the browser
  agent — today's path. Task decomposition and the other cognitive agents are still
  stubbed (marked `# LLM:` in the source).
- **The substrate.** [agent_log.py](app/services/agent_log.py) is a surface-agnostic
  `Recorder` that persists every run, compiled packet, and human verdict (including
  the *edit* revision — the richest training signal) into `data/quill.db`, surfaced at
  `GET /console/agent-runs`.

### 10. Browser agent — "Exec.AI"

**Directory:** [browser_agent/](browser_agent/) · **standalone entry:**
[exec_webapp.py](exec_webapp.py) · **memory bridge:**
[app/services/agent_bridge.py](app/services/agent_bridge.py)

A self-contained, Anthropic-backed autonomous web agent with a mature
**route → plan → execute → verify** loop over deterministic Playwright actions.

- **Routing.** A vinceo.ai "envelope" (intent, `requires_browser`, `requires_approval`,
  `site`, `surface`) decides answer-directly vs. drive-the-browser vs. hand off to
  the desktop or phone agent.
- **Tiered models** ([browser_agent/config.py](browser_agent/config.py)):
  **Sonnet** for routing/execution (the hot path), **Opus** for planning and
  escalation, **Haiku** for high-volume yes/no verification. Reasoning `effort` is the
  biggest cost knob and is set per tier.
- **Executor vision.** DOM + screenshot; Claude reads the pixels itself (its own OCR,
  no extra engine). Adaptive — the screenshot is attached only when the accessibility
  tree is thin or the agent is stuck, keeping the per-step path cheap. Proven
  load-bearing: an eval renders an access code entirely inside a `<canvas>` (no DOM
  text) and the agent fails text-only but succeeds with vision on.
- **Source-grounded approval packets.** `request_approval` shows a structured
  Action / To / Subject / Body / **Why / Source** packet with **Approve / Edit /
  Cancel** (editing feeds a revision back so it re-drafts).
- **Agent modes** ([browser_agent/modes.py](browser_agent/modes.py)) — 7 task-specific
  policies (email / calendar / research / shopping / crm / form / general). Each adds
  guidance and *extra* approval patterns (additive to the global commit net, never
  subtractive); research mode is `read_only`.
- **Dry-run levels** (`AGENT_DRY_RUN`: `plan` / `navigate` / `draft` / `approval` /
  `full`) cap how far a run may go — a safety lever for demos.
- **Failure taxonomy** ([browser_agent/failures.py](browser_agent/failures.py))
  classifies blocks (login / captcha / timeout / wrong page / no-progress) into
  recovery actions (LOGIN / REPLAN / INSTRUCT / STOP). It explicitly refuses to enter
  credentials or solve CAPTCHAs.
- **Learning layer** ([browser_agent/memory.py](browser_agent/memory.py)) recalls what
  worked for `intent@site` on past runs to shorten plans and avoid repeat mistakes.
- **Session reuse.** A named profile (`QUILL_AGENT_PROFILE`) is a persistent
  user-data-dir — log into Gmail/a CRM by hand once and the agent reuses that
  authenticated session. `QUILL_AGENT_CHANNEL=chrome` uses real installed Chrome
  (far less likely to be blocked than bundled Chromium).
- **Memory-grounded.** The agent gets a `memory_provider` that semantic-searches
  vinceo.ai's own timeline, so *"follow up on what Marc said about pricing"* pulls the
  "$49/mo" it overheard — no re-explaining.

### 11. Desktop agent

**Directory:** [desktop_agent/](desktop_agent/)
([guards.py](desktop_agent/guards.py) · [driver.py](desktop_agent/driver.py) ·
[config.py](desktop_agent/config.py))

OS-level control — the counterpart to the browser agent, for tasks that live in
apps rather than the web. Because there is no browser sandbox here, **the allowlist
*is* the sandbox.**

Guardrails (all layered):

- **Path jail** — file operations are confined under `QUILL_DESKTOP_JAIL`.
- **App allowlist** — launch apps by key (cursor / code / notepad / explorer /
  chrome / terminal), never by raw path.
- **Shell-verb allowlist** — read verbs run automatically, mutating verbs are
  approval-gated, everything else is blocked.
- **Hard-block list** no prompt can unlock — `rm`, `del`, `format`, `reg`, `sudo`,
  shell metacharacters, secret-path markers, `..`.
- **Args-as-list, `shell=False`** — no shell injection surface.
- **Tiered human approval** on mutating actions, an **audit log**, a per-task action
  budget, and a command timeout.

Integration: the router emits a `surface` field; `surface == 'desktop'` dispatches to
a guarded observe→act loop over `DesktopDriver` (also reachable directly via
`POST /desktop`). Flagship demo: *"open Cursor and start a new project"* works via
`make_dir` + `launch_app` — no pixel automation. Approval is always routed to the
**live human**, never satisfied from memory.

### 12. Phone Link agent

**File:** [app/services/phone_link.py](app/services/phone_link.py) · **scripts:**
[scripts/phone_link/](scripts/phone_link/)

The outbound counterpart to notification capture. It drives the installed Windows
**Phone Link** UI via PowerShell + UI Automation (scripts adapted from the MIT-licensed
`phonelink-mcp-server`) to **launch Phone Link, read conversations, and send SMS** to
your iPhone from the laptop.

Typical flow: you say *"text Justin I'll be late"* → the router picks
`surface = phone_link` → this module launches Phone Link and sends the SMS **after the
approval gate**. Reachable directly via `POST /phone`. Windows-only; disable with
`QUILL_PHONE_LINK=0`.

### 13. Proactive watchers

**Files:** [app/services/todo_watcher.py](app/services/todo_watcher.py),
[app/services/task_offer.py](app/services/task_offer.py),
[app/services/phone_watcher.py](app/services/phone_watcher.py)

- **To-do watcher.** When vision classifies a page as `todo_list`, it *offers in chat*
  to run the items through the browser agent (debounced by an items-hash with a
  5-minute cooldown). On "yes", each item becomes a memory-grounded agent goal — the
  fully autonomous **see → offer → act** trigger.
- **Task offer.** Surfaces spoken tasks as proactive "run this?" chat offers.
- **Phone watcher.** When an iPhone notification arrives via Phone Link, offers (in
  chat) to reply or open the thread; on "yes", dispatches with `surface=phone_link`.
  Disable with `QUILL_PHONE_WATCH=0`.

All watchers are gated by `QUILL_AGENT`; set it to `0` to make `/chat` a pure
memory retriever and silence every proactive offer.

### 14. Model router & model log

**Files:** [app/services/model_router.py](app/services/model_router.py),
[app/services/model_log.py](app/services/model_log.py)

A model-selection layer and a log of which model served which call (written to
`data/model_calls.jsonl`, surfaced at `GET /console/models`) — the foundation for
routing across a fleet of models by cost/capability. The extractor, reflector, and each
agent tier already expose swappable model constants the router can target.

**Local-first text** (`QUILL_TEXT_LOCAL=1`, mirror of the vision tiering): every
router-served text call (chat, extract, reflect, activity summarize) runs on a local
Ollama text model ([app/services/ollama_text.py](app/services/ollama_text.py)) first
and escalates to Claude when the local model is unreachable/errors, its output doesn't
parse, it self-reports confidence below `QUILL_TEXT_ESCALATE_MIN_CONF`, or the task is
in the high-stakes set (`QUILL_TEXT_HIGH_STAKES_TASKS`, default `plan`). Each
escalation appends a `modality="text"` distill row to the same
`data/escalate_distill.jsonl` trail vision uses (prompts truncated; no transcripts).
Off (the default) keeps routing Claude-only, unchanged. The browser agent's executor
escalation (Sonnet → Opus on a stalled step, `browser_agent/config.py`) is a separate,
Claude-internal ladder and intentionally does **not** route through this policy.

---

## Interfaces & API

| Surface | What it is |
|---|---|
| **`/console`** | **Memory Console** — the trust/inspection layer. Timeline, search, speaker labels, confidence + low-confidence filter, provenance (clip/frame playback), a **Tasks** view with the approve/done/edit/dismiss review loop, reflections, agent-run history, and a models view. |
| **`/ui`** | **Live chat page** — watch capture happen, see to-do/phone offers, reply, approve. |
| **`/facts` API** | The programmatic review surface — list (filter by kind/status/review), `open_tasks`, and `approve` / `dismiss` / `done` / `edit`. |
| **`/reflections` API** | List reflections & items; `approve` / `dismiss` / `edit` / `convert`-to-task each insight; `POST /reflect/run`. |
| **`/chat`** | Dispatch a turn to the browser (or desktop/phone) agent; memory-grounded; **non-blocking** — enqueue, then poll `/chat/poll` for progress / results / approval prompts, and answer via `/chat/answer`. `/chat/new` starts a fresh session. |
| **`/graph`** | `context` / `rebuild` / `stats` over the knowledge graph. |
| **API docs** | FastAPI interactive docs at <http://127.0.0.1:8000/docs>. |
| **Browser-agent UI** | The Exec.AI web chat (Flask) at <http://127.0.0.1:5000>. |

**Full endpoint list** (see [app/api/routes.py](app/api/routes.py)):

```
GET  /health
POST /audio/start · /audio/stop
POST /vision/start · /vision/stop
POST /notifications/start · /notifications/stop
GET  /memory · /memory/search?q=
GET  /console · /console/events · /console/turns · /console/jobs · /console/models
GET  /console/agent-runs · /console/agent-runs/{run_id}
POST /console/consolidate
GET  /artifact                         (path-confined raw clip/frame serving)
GET  /graph/context · POST /graph/rebuild · GET /graph/stats
GET  /facts · /facts/open_tasks
POST /facts/{id}/approve · /dismiss · /done · /edit
POST /reflect/run
GET  /reflections · /reflections/list
POST /reflection_items/{id}/approve · /dismiss · /edit · /convert
POST /chat · GET /chat/poll · POST /chat/answer · POST /chat/new
POST /desktop · POST /phone
GET/POST /credentials
POST /speak · GET /speakers · POST /speakers/enroll
GET  /ui
```

---

## Tech stack

| Layer | Tool | Role |
|---|---|---|
| **API / server** | FastAPI + Uvicorn, Pydantic | HTTP surface, async event loop |
| **Config** | python-dotenv | env / `.env` settings |
| **Audio capture** | sounddevice, NumPy | low-latency mic |
| **VAD** | silero-vad (ONNX) | utterance segmentation |
| **ASR** | faster-whisper (CTranslate2) | transcription, no GPU/torch |
| **Speaker ID** | SpeechBrain ECAPA-TDNN | diarization + named voiceprints |
| **Vision capture** | OpenCV | webcam + frame selection |
| **Vision / VLM** | Ollama (`minicpm-v`, local) → Claude (Opus 4.8) fallback; Gemini optional | structured frame understanding |
| **Phone notifications** | winsdk (`UserNotificationListener`) | Windows toast / iPhone mirror capture |
| **Phone control** | PowerShell + UI Automation | drive Phone Link (launch / read / send SMS) |
| **Embeddings** | sentence-transformers (MiniLM, local) | semantic search vectors |
| **Vector store** | LanceDB (embedded, file-based) | meaning-based retrieval |
| **Timeline store** | SQLite | events, facts, relations, reflections, jobs, agent runs, provenance |
| **Browser agent** | Playwright (Chromium) + Claude (Sonnet / Opus / Haiku) | autonomous web actions |
| **Agent UI** | Flask | browser-agent web chat |
| **Dashboard (optional)** | Streamlit | listed, optional |

---

## Configuration reference

All settings are read from the environment / `.env` with sane defaults (see
[app/config.py](app/config.py), [desktop_agent/config.py](desktop_agent/config.py),
[browser_agent/config.py](browser_agent/config.py)). A `.credentials.env` is loaded
after `.env` with override, for secrets (path overridable via `QUILL_CREDENTIALS_FILE`).

> **Naming note:** the product is **vinceo.ai**, but the environment variables and the
> SQLite file keep the `QUILL_` / `quill.db` prefix from the project's original name —
> deliberately, so existing configs and data keep working.

### Core / server

| Var | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude vision, extraction, reflection, and the agent |
| `GOOGLE_API_KEY` | — | optional; enables the Gemini vision backend |
| `QUILL_HOST` / `QUILL_PORT` | `127.0.0.1` / `8000` | API bind |
| `QUILL_DATA_DIR` | `data` | relocate all persisted data |
| `QUILL_CREDENTIALS_FILE` | `.credentials.env` | secrets file loaded after `.env` |
| `QUILL_AUTOSTART` | `0` | `1` = start capture on server boot (set by `run_all.py`) |
| `QUILL_AGENT` | `1` | `0` reverts `/chat` to the memory-only retriever, disables watchers |

### Audio (M1)

| Var | Default | Notes |
|---|---|---|
| `QUILL_WHISPER_MODEL` | `small` | `base`, `medium`, `large-v3-turbo`, … |
| `QUILL_WHISPER_COMPUTE` | `int8` | `int8`, `float16` (GPU), `float32` |
| `QUILL_WHISPER_DEVICE` | `cpu` | `cuda` for a big speedup |
| `QUILL_ASR_LANGUAGE` | auto | e.g. `en` to skip detection |
| `QUILL_VAD_THRESHOLD` | `0.5` | Silero speech-probability threshold |
| `QUILL_MIN_SILENCE_MS` | `500` | silence to end an utterance |
| `QUILL_SPEECH_PAD_MS` | `150` | padding around detected speech |

### Speaker ID

| Var | Default | Notes |
|---|---|---|
| `QUILL_SPEAKERS` | `1` | `0` disables speaker ID |
| `QUILL_SPEAKER_CLUSTER_THRESHOLD` | `0.40` | cosine sim to merge anonymous clusters |
| `QUILL_SPEAKER_ID_THRESHOLD` | `0.45` | cosine sim to match a named voiceprint |

### Vision (M2)

| Var | Default | Notes |
|---|---|---|
| `QUILL_VISION` | `1` | `0` disables the webcam pipeline |
| `QUILL_CAMERA_INDEX` | `0` | pick a different webcam |
| `QUILL_CAMERA_BACKEND` | `dshow` (Win) | OpenCV backend; DirectShow is reliable on Windows |
| `QUILL_CAMERA_FOURCC` | `MJPG` (Win) | avoids green/noise frames from pixel-format mismatch |
| `QUILL_CAMERA_WIDTH` / `_HEIGHT` | `1280` / `720` | requested resolution (`0` = don't request) |
| `QUILL_CAMERA_WARMUP` | `20` | frames discarded so the sensor auto-exposes |
| `QUILL_VISION_MIN_BRIGHTNESS` | `8` | skip analyzing frames darker than this (0–255 mean) |
| `QUILL_VISION_MIN_INTERVAL_S` | `5` | analyze at most this often |
| `QUILL_VISION_MAX_INTERVAL_S` | `30` | force a frame at least this often |
| `QUILL_VISION_MOTION_THRESHOLD` | `12` | mean abs frame-diff to treat as "scene changed" |
| `QUILL_VISION_JPEG_QUALITY` | `80` | saved-frame quality |
| `QUILL_VISION_MODEL` | `claude-opus-4-8` | Claude vision model (fallback tier) |
| `QUILL_VISION_LOCAL` | `1` | local-first VLM via Ollama; Claude is the paid fallback |
| `QUILL_VISION_LOCAL_MODEL` | `minicpm-v` | local Ollama vision model |
| `QUILL_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama endpoint |
| `QUILL_VISION_LOCAL_TIMEOUT_S` | `60` | local VLM timeout |
| `QUILL_VISION_ESCALATE_MIN_CONF` | `0.6` | escalate a content page to Claude below this confidence |
| `QUILL_VISION_ESCALATE_MIN_CAPTURE` | `0.6` | escalate content pages with weak frame capture quality |
| `QUILL_ESCALATE_LOG` | `1` | append local→parent distill rows when Claude is invoked |
| `QUILL_ESCALATE_LOG_PATH` | `data/escalate_distill.jsonl` | distillation trail (no image bytes; uses `frame_path`) |

API: `GET /console/escalate` — escalate counts by reason + recent rows.

### One-time onboarding (new-user profile)

Seeds vinceo.ai's knowledge of a new user's day-to-day so it doesn't start cold
(see [app/services/onboarding.py](app/services/onboarding.py)).

**Preferred:** open the guided UI at [http://127.0.0.1:8000/onboarding](http://127.0.0.1:8000/onboarding)
(also linked as **Setup** from Chat). Identity → people → work → rhythm, then Save.

First boot still writes `data/onboarding_profile.json` as a backup sheet; the web
UI writes through the same ingest path (people/entities, graph edges, accepted
claims). Idempotent and delta-aware — edit later and re-save; only new answers
are added. Asked once (completion is tracked in state).

| Var | Default | Notes |
|---|---|---|
| `QUILL_ONBOARDING` | `1` | the once-only profile flow (template + auto-ingest on boot) |
| `QUILL_ONBOARDING_PROFILE` | `data/onboarding_profile.json` | JSON backup of the profile |
| `QUILL_ONBOARDING_STATE` | `data/onboarding_state.json` | asked-once / delta-ingest bookkeeping |

API: `GET /onboarding` (UI) · `GET /onboarding/status` · `GET /onboarding/profile` · `POST /onboarding/template` · `POST /onboarding/ingest`.

### Local-first text (chat / extract / plan)

Mirror of the vision tiering for TEXT (see §14, [app/services/ollama_text.py](app/services/ollama_text.py)
and [app/services/model_router.py](app/services/model_router.py)). Off by default.

| Var | Default | Notes |
|---|---|---|
| `QUILL_TEXT_LOCAL` | `0` | local-first TEXT via Ollama; off/unset = Claude-only routing, unchanged |
| `QUILL_TEXT_LOCAL_MODEL` | `llama3.2` | local Ollama text model |
| `QUILL_TEXT_LOCAL_TIMEOUT_S` | `45` | local text timeout |
| `QUILL_TEXT_ESCALATE_MIN_CONF` | `0.6` | escalate below this self-reported confidence |
| `QUILL_TEXT_HIGH_STAKES_TASKS` | `plan` | comma-separated tasks that always escalate to Claude |

`QUILL_OLLAMA_URL` is shared with vision. Escalations write `modality="text"` rows to
the same `data/escalate_distill.jsonl` trail.

### Desktop capture (passive screen + clicks)

Opt-in observation of your screen and mouse clicks (no keystrokes). Off by default.
Enable with `QUILL_DESKTOP_CAPTURE=1` or `python run_all.py --desktop-capture`.

| Var | Default | Notes |
|---|---|---|
| `QUILL_DESKTOP_CAPTURE` | `0` | master switch for passive desktop observation |
| `QUILL_DESKTOP_CAPTURE_SCREEN` | `1` | motion-gated screen frames → VLM → `VISION` events |
| `QUILL_DESKTOP_CAPTURE_CLICKS` | `1` | mouse clicks → `INPUT` events (coords + window + crop) |
| `QUILL_DESKTOP_CAPTURE_CLICK_VLM` | `0` | opt-in local-only describe of click crops (never Claude) |
| `QUILL_DESKTOP_CAPTURE_MIN_INTERVAL_S` | `8` | analyze screen at most this often |
| `QUILL_DESKTOP_CAPTURE_MAX_INTERVAL_S` | `45` | force a screen frame at least this often |
| `QUILL_DESKTOP_CAPTURE_MOTION_THRESHOLD` | `10` | mean abs frame-diff to treat as "changed" |
| `QUILL_DESKTOP_CAPTURE_MAX_WIDTH` | `1280` | downscale long edge before VLM |
| `QUILL_DESKTOP_CAPTURE_CLICK_CROP` | `420` | crop size (px) around click for context |
| `QUILL_DESKTOP_CAPTURE_CLICK_VLM_MIN_S` | `8` | min seconds between click VLM calls (if enabled) |
| `QUILL_DESKTOP_CAPTURE_CLICK_DEDUP_PX` | `12` | ignore near-duplicate clicks within this radius |
| `QUILL_DESKTOP_CAPTURE_CLICK_DEDUP_S` | `0.35` | ignore near-duplicate clicks within this window |

API: `POST /desktop-capture/start` · `POST /desktop-capture/stop`. Frames land in `data/desktop_frames/`.

### Anticipation (likely-next from activities)

Heuristic suggestions from recent app-focus activities (transitions + open tasks).
Off by default; surfaces a yes/no chat offer after an activity looks idle.

| Var | Default | Notes |
|---|---|---|
| `QUILL_ANTICIPATE` | `0` | master switch for likely-next chat offers |
| `QUILL_ANTICIPATE_MIN_CONF` | `0.6` | minimum transition/pattern confidence to offer |
| `QUILL_ANTICIPATE_COOLDOWN_S` | `600` | don't re-offer the same suggestion within this window |
| `QUILL_ANTICIPATE_IDLE_S` | `90` | newest activity must be idle this long before offering |
| `QUILL_ANTICIPATE_HISTORY` | `40` | how many recent activities to score |
| `QUILL_ANTICIPATE_MIN_ACTIVITIES` | `3` | need at least this many blocks before scoring |
| `QUILL_ANTICIPATE_MIN_TRANSITIONS` | `2` | min A→B counts from the current app |
| `QUILL_ANTICIPATE_MAX` | `1` | max candidates considered per pass |

### Phone notifications & Phone Link (Windows)

| Var | Default | Notes |
|---|---|---|
| `QUILL_NOTIFICATIONS` | `1` (Win) / `0` else | capture Windows toasts (iPhone mirror) |
| `QUILL_NOTIFICATION_POLL_S` | `2.5` | toast poll interval (seconds) |
| `QUILL_NOTIFICATIONS_PHONE_LINK_ONLY` | `1` | only ingest Phone Link / "Link to Windows" toasts |
| `QUILL_NOTIFICATION_APPS` | — | comma list to widen the filter, e.g. `phone link,slack` |
| `QUILL_AUTOSTART_NOTIFICATIONS` | `1` | (via `run_all.py`) start notification capture on boot |
| `QUILL_PHONE_LINK` | `1` | allow driving Phone Link UI (send/read SMS) |
| `QUILL_PHONE_LINK_PS` | `powershell.exe` | PowerShell executable used for the automation scripts |
| `QUILL_PHONE_WATCH` | `1` | proactively offer to reply/open incoming iPhone notifications |

### Memory, storage, hygiene, consolidation, worker, reflection

| Var | Default | Notes |
|---|---|---|
| `QUILL_SEMANTIC` | `1` | `0` falls back to substring search |
| `QUILL_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | local sentence-transformers model |
| `QUILL_SAVE_AUDIO` | `1` | `0` skips saving WAV clips |
| `QUILL_INGEST_FILTER` | `1` | ASR hygiene (drops hallucinations/dupes); many `QUILL_INGEST_*` sub-thresholds |
| `QUILL_CONSOLIDATE` | `1` | merge utterances into turns |
| `QUILL_CONSOLIDATE_MAX_GAP_S` | `8` | silence gap that starts a new turn |
| `QUILL_WORKER` | `1` | durable background job runner |
| `QUILL_WORKER_POLL_S` / `_MAX_ATTEMPTS` | `2.0` / `3` | worker poll interval / retry cap |
| `QUILL_EXTRACT` | `1` | run fact/task extraction (calls the LLM) |
| `QUILL_REFLECT` | `1` | run daily reflection (calls the LLM) |
| `QUILL_REFLECT_MODEL` | `claude-opus-4-8` | reflection model (one swappable boundary) |
| `QUILL_REFLECT_MAX_RECENT` / `_MAX_OPEN` | `120` / `40` | packet size bounds |

### Agents

| Var | Default | Notes |
|---|---|---|
| `QUILL_PLANNER` | `0` | `1` enables the Personal Agent Layer (compile goals → ActionPackets) |
| `QUILL_AGENT_CHANNEL` | — | e.g. `chrome` — browser channel for the in-app agent |
| `QUILL_AGENT_PROFILE` | — | persistent browser profile name (logged-in session reuse) |
| `AGENT_DRY_RUN` | `approval` | how far a run may go: `plan` / `navigate` / `draft` / `approval` / `full` |
| `AGENT_EXECUTOR_VISION` | `1` | let the executor read screenshots |
| `AGENT_VISION_ALWAYS` | `0` | attach a screenshot every step (vs. only when stuck) |
| `AGENT_VISION_SPARSE_AT` | `6` | attach a shot when fewer than N elements are visible |
| `AGENT_DATA_DIR` | `./sessions` | agent session storage |
| `AGENT_BROWSER_CHANNEL` | — | low-level default browser channel for `browser_agent` |

Browser-agent model tiers are constants in
[browser_agent/config.py](browser_agent/config.py): router/executor = Sonnet,
planner/escalation = Opus, verifier = Haiku.

### Desktop agent

| Var | Default | Notes |
|---|---|---|
| `QUILL_DESKTOP_JAIL` | `~/quill_desktop` | path jail for all file operations |
| `QUILL_DESKTOP_APPROVAL` | `1` | require human approval for mutating actions |
| `QUILL_DESKTOP_TIMEOUT_S` | `60` | per-command timeout |
| `QUILL_DESKTOP_MAX_ACTIONS` | `25` | per-task action budget |
| `QUILL_DESKTOP_MAX_FILE_BYTES` | `200000` | max file read/write size |

---

## Where data lives

| Data | Location |
|---|---|
| Timeline + facts + relations + reflections + jobs + agent runs | `data/quill.db` (SQLite) |
| Raw audio utterances | `data/audio/<epoch>.wav` (16-bit mono, one per utterance) |
| Captured frames | `data/frames/<epoch>.jpg` (one per analyzed frame) |
| Voiceprints | `data/speakers/*.npy` (one per enrolled person) |
| Semantic index | `data/lance/` (LanceDB, 384-d embeddings) |
| Model-call log | `data/model_calls.jsonl` |
| Browser-agent sessions & profiles | `./sessions/` |
| Model weights | `~/.cache/huggingface` |

The timeline reloads from `data/quill.db` on startup, so everything survives
restarts. Raw artifacts are served back through `/artifact`, which is **path-confined
to the data directory** so it can't be used to read arbitrary files. The entire `data/`
and `sessions/` trees are git-ignored.

---

## Project layout

```
run_all.py               launch everything (capture in-process + agent as child)
run_audio.py             M1 standalone — live transcription
run_vision.py            M2 standalone — live webcam understanding
run_desktop.py           desktop-agent standalone driver
exec_webapp.py           browser agent standalone (with vinceo.ai memory bridge)

app/
  config.py              central settings (audio / vision / notif / memory / worker …)
  events.py              Event schema + EventBus (async pub/sub)
  main.py                FastAPI app + startup wiring (worker chain, watchers)
  storage.py             SQLite: events, facts, tasks, commitments, people, entities,
                         relations, reflections, jobs, agent runs — + query helpers
  vectorstore.py         LanceDB semantic index
  api/routes.py          every HTTP endpoint + the Console HTML
  services/
    audio.py             M1: mic → VAD → Whisper → Event
    ingest_filter.py     ASR hygiene (drops hallucinations / dupes)
    speakers.py          ECAPA speaker ID (anonymous clusters + named voiceprints)
    vision.py            M2: webcam → frame selection → VLM → Event
    vlm.py / vlm_gemini.py   Claude / Gemini vision clients (structured extraction)
    notifications.py     Windows toast capture (Phone Link / iPhone mirror)
    phone_link.py        drive Phone Link (launch / read / send SMS)
    phone_watcher.py     proactive "reply to this notification?" offers
    memory.py            M3 Memory Engine (subscribes to the bus)
    embeddings.py        local sentence-transformers embedder
    consolidation.py     merge utterances → turns
    worker.py            durable background job runner (one queue, one worker)
    extractor.py         Track A: turns → tasks / commitments / claims
    resolution.py        person resolution (exact → prefix → embedding)
    reflector.py         daily facts → grounded, reviewable insights
    graph.py             M5 v1 knowledge graph (rebuild + context_for_person)
    agent_planner.py     Personal Agent Layer (goal → Plan of ActionPackets)
    agent_log.py         Recorder + ActionPacket (persists runs/packets/verdicts)
    agent_bridge.py      lazily-started worker owning the browser agent
    todo_watcher.py      proactive "run these to-dos?" offers
    task_offer.py        proactive "run this spoken task?" offers
    model_router.py      model selection layer
    model_log.py         per-call model usage log
    llm.py               memory-only retriever (fallback when QUILL_AGENT=0)
    voice.py             M4 TTS (stub)

browser_agent/           Exec.AI — route → plan → execute → verify web agent
  orchestrator.py        the run_goal() loop
  config.py              tiered models, dry-run levels, vision knobs
  tools.py               click / type / navigate / read / ask_human / request_approval / done
  perception.py          DOM + screenshot perception
  modes.py               7 task-specific policies
  failures.py            failure taxonomy + recovery ladder
  memory.py              intent@site procedural learning
  browser.py / llm.py / prompts.py / credentials.py / eval_tasks.py

desktop_agent/           guarded OS control (allowlist IS the sandbox)
  guards.py              the security boundary — pure decision logic
  driver.py              DesktopDriver (launch apps, make dirs, run allowlisted cmds)
  config.py              jail, allowlists, budgets

scripts/                 download_models · enroll_speaker · run_extract · eval_agent ·
                         eval_vision_task · eval_modes_dryrun · test_track_a ·
                         test_reflection · test_planner · test_agent_log ·
                         facts_schema_check · bench_vision · diagnose_camera · …
  phone_link/            PowerShell UI-automation scripts (launch / send / get messages)
```

---

## Development & testing

| Area | Coverage |
|---|---|
| **Facts layer** | Well covered, assertion-based: `scripts/test_track_a.py`, `scripts/facts_schema_check.py`, `scripts/run_extract.py --demo`. |
| **Reflection** | `scripts/test_reflection.py` — runs a daily reflection and checks grounding. |
| **Personal Agent Layer** | `scripts/test_planner.py` (context selection, risk table, compilers), `scripts/test_agent_log.py` (recorder / packets / verdicts). |
| **Browser agent** | Eval-based: `scripts/eval_agent.py` (routing tier tracks the safety metric — approval false-negatives, baseline 0; live tier tracks success/steps/latency/cost), `scripts/test_approval_packet.py`, `scripts/eval_modes_dryrun.py`, `scripts/eval_vision_task.py` (the canvas-only access-code test). |
| **Vision** | `scripts/check_vision.py`, `scripts/test_vision.py`, `scripts/bench_vision.py`, `scripts/gen_bench_dataset.py`, `scripts/diagnose_camera.py`. |
| **Desktop agent** | ⚠️ **Gap.** `desktop_agent/guards.py` is the security boundary but has no dedicated unit tests yet — it's pure decision logic, so it's the single highest-value place to add them (assert `rm` / `..` / secret-paths / unknown-verbs are BLOCKED and jail escapes rejected). Recommended before trusting the desktop agent in a live `/chat` run. |

Run any script from the project root with the venv active, e.g.:

```powershell
python scripts/test_track_a.py
python scripts/test_reflection.py
python scripts/eval_agent.py
```

**Conventions.** Config is centralized and env-driven (`app/config.py` frozen
dataclasses); every service degrades gracefully rather than crashing on a missing key,
camera, or model; each LLM boundary hides behind one swappable model constant so the
model router can retarget it later. New capture modalities just publish an `Event` to
the bus — no downstream code changes.

---

## Security model

vinceo.ai acts on your behalf, so its trust boundaries are explicit and layered:

1. **Memory is context, never command authority.** Perception is
   attacker-influenceable (anything said in the room, shown to the camera, or pushed
   as a notification), so retrieved memory can inform a draft but can never *approve*
   an irreversible action. Only a **live human reply** authorizes send / buy / delete /
   mutate.
2. **Risk classification is a table, not a guess.** The Planner's `RISK_TABLE` maps
   action verbs to `low / medium / high / blocked`; `blocked` never reaches an
   execution surface and anything ≥ medium forces the approval gate.
3. **Browser agent:** irreversible steps gate behind a source-grounded approval
   packet; it refuses to enter credentials or solve CAPTCHAs; dry-run levels cap how
   far any run can go; per-mode approval patterns only ever *add* to the global commit
   net.
4. **Desktop agent — the allowlist *is* the sandbox:** path jail, app allowlist,
   shell-verb allowlist, an unremovable hard-block list, `shell=False` with
   args-as-list, tiered approval, an audit log, and per-task budgets.
5. **Phone Link** sends SMS only through the approval gate.
6. **Provenance serving** (`/artifact`) is path-confined to the data directory.

> **Handle your keys carefully.** `.env` and `.credentials.env` are git-ignored, but
> they hold live API keys in plaintext — don't share them, don't paste them into
> screenshots, and rotate any key that has been exposed.

---

## Design ethos

- **Local/CPU-first.** VAD, ASR, speaker ID, embeddings, and (by default) vision all
  run without a paid API call. Claude is used only where reasoning or high-stakes
  vision needs it.
- **Cost control at every model boundary.** Frame selection, local-first vision with
  selective escalation, tiered agent models, reasoning-effort knobs, and adaptive
  screenshot attachment.
- **Graceful degradation.** A missing API key, camera, local model, or non-Windows OS
  degrades one feature — it never crashes the pipeline.
- **Provenance and a human-in-the-loop review layer** make the system trustworthy
  enough to eventually act autonomously.

---

## Known gaps & roadmap

- **Voice / TTS (`app/services/voice.py`)** — still a stub (`{"spoken": false}`);
  spoken Q&A (the rest of M4) isn't wired.
- **Personal Agent Layer is a v1 slice** — behind `QUILL_PLANNER=1` (default off).
  Task decomposition returns `[goal]`, and the Meeting/Relationship/Project cognitive
  agents beyond the Writing/Meeting sketches are stubbed (`# LLM:` markers).
- **Unit-test `desktop_agent/guards.py`** — the security boundary is only indirectly
  covered today (see [Development & testing](#development--testing)).
- **Close the Track A ↔ agent bridge** — pass a real `fact_id` + event timestamp into
  the approval packet so the "Source:" line is verbatim-from-DB, not model-generated.
- **Live-API desktop test through `/chat`** — the desktop agent is integrated but so
  far only verified with a stubbed LLM.
- **Enrich the extractor to populate `entities`** (orgs/projects) — lights up the
  knowledge graph's affiliation traversal.
- **Scheduling** — reflection is time-triggered on startup; there's no real cron yet.
- **M6 connectors** — native email / calendar / CRM (the browser agent drives those
  UIs directly for now).
- **Postgres / object storage** — deferred to a later version, gated on a second
  device.

---

## License & status

**Status:** experimental research prototype under active development — not production
software, APIs and schemas may change without notice.

**License:** no license file is present, so all rights are reserved by default. If you
intend to share or open-source this, add a `LICENSE` file (e.g. MIT) to state terms
explicitly. Note that the Phone Link PowerShell scripts under
[scripts/phone_link/](scripts/phone_link/) are adapted from the MIT-licensed
`phonelink-mcp-server` project and retain that attribution.

**Deeper docs:** the interactive API reference lives at
<http://127.0.0.1:8000/docs> when the server is running; per-subsystem design notes
are inline as module docstrings, and dated build logs are in
[july_07_2026_status.md](july_07_2026_status.md) and
[july_07_2026_status_2.md](july_07_2026_status_2.md).
