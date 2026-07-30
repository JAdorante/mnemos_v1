# vinceo.ai

**A personal memory + agent system that hears, sees, remembers, reflects — and, with your approval, acts.**

![status](https://img.shields.io/badge/status-experimental%20prototype-orange)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows%20(primary)%20·%20macOS%2FLinux-lightgrey)
![local-first](https://img.shields.io/badge/inference-local--first-brightgreen)
![license](https://img.shields.io/badge/license-unspecified-lightgrey)

vinceo.ai is a laptop prototype of a wearable (a "pen") that continuously **hears**
(microphone), **sees** (webcam, and optionally your screen), and — on Windows —
mirrors your **phone notifications**. It distills that raw stream into a
searchable, provenance-linked memory; extracts the **tasks, commitments, and
claims** buried in ordinary conversation; rolls your desktop into "what was I
doing?" **activity blocks**; reflects daily into durable **insights**; and then
**acts** — driving a real web browser, your desktop, or Phone Link — pausing at
a human approval gate before anything irreversible.

The one-sentence pitch is the **hear → act loop**: vinceo.ai overhears
*"I'll send Justin the pricing follow-up — Marc said forty-nine a month,"* files
the commitment with a verbatim source quote and the audio clip to prove it, and
later hands that task to an autonomous browser agent that drafts the email and
stops for your **Approve / Edit / Cancel** before anything sends.

> The laptop is the hardware prototype: prove the software experience first,
> shrink it into a pen later.

> **Status:** experimental research prototype under active development. Capture,
> memory, facts, reflection, the knowledge graph, local-first model routing, and
> the browser/desktop/phone agents all work today; some pieces are stubs or
> feature-flagged off — see [Known gaps & roadmap](#known-gaps--roadmap). Not
> production software.

---

## Table of contents

- [How it works — the short version](#how-it-works--the-short-version)
- [One moment, end to end](#one-moment-end-to-end)
- [The Event: one schema for everything](#the-event-one-schema-for-everything)
- [Layer 1 — Perceive](#layer-1--perceive)
- [Layer 2 — Remember](#layer-2--remember)
- [Layer 3 — Understand](#layer-3--understand)
- [Layer 4 — Decide](#layer-4--decide)
- [Layer 5 — Act](#layer-5--act)
- [Local-first models & the escalation ladder](#local-first-models--the-escalation-ladder)
- [The learning loop — from verdicts to weights](#the-learning-loop--from-verdicts-to-weights)
- [The Memory Console — the trust layer](#the-memory-console--the-trust-layer)
- [Proactive behavior](#proactive-behavior)
- [Quick start](#quick-start)
- [Usage examples](#usage-examples)
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

## How it works — the short version

vinceo.ai is a stack of five layers. Each layer only depends on the one below it,
and everything between them travels as a single `Event` on an in-process bus:

```
  ┌───────────────────────────────────────────────────────────────┐
  │  ACT       Browser agent (Exec.AI) · Desktop agent · Phone Link│  ← touches the world, approval-gated
  ├───────────────────────────────────────────────────────────────┤
  │  DECIDE    Personal Agent Layer — goal → risk-classified plan  │  ← plans, grounds, classifies risk
  ├───────────────────────────────────────────────────────────────┤
  │  UNDERSTAND Facts · Activities · Reflection · Knowledge graph  │  ← what the stream *means*
  ├───────────────────────────────────────────────────────────────┤
  │  REMEMBER  Memory Engine — SQLite timeline + LanceDB semantic  │  ← durable, searchable, provenance-linked
  ├───────────────────────────────────────────────────────────────┤
  │  PERCEIVE  Audio · Vision · Desktop capture · Phone notifs     │  ← raw perception → Event
  └───────────────────────────────────────────────────────────────┘
```

Three principles run through every layer:

- **Local-first.** Voice-activity detection, speech-to-text, speaker ID, and
  embeddings run on the CPU with no GPU and no PyTorch. Vision — and, opt-in,
  text — go to a **local Ollama model first**; a paid Claude call happens only
  when a task is high-stakes or the local model is unsure. Every escalation is
  logged as a distillation row, so the paid calls are also training data for
  making the local models better.
- **Provenance everywhere.** Every memory links back to the raw audio clip or
  frame it came from; every extracted fact carries a verbatim `source_span`
  quote and a pointer to its source event. Nothing is a black box you have to
  trust — the Console lets you play the exact sound a fact came from.
- **Memory is context, never command authority.** Perception is
  attacker-influenceable (anything said in the room, shown to a camera, or
  pushed as a notification). Retrieved memory can *inform* an action but can
  never *approve* one — only a live human reply authorizes anything
  irreversible.

---

## One moment, end to end

The fastest way to understand the program is to follow one sentence through it.
You say, near the laptop:

> *"I still owe Justin the pricing follow-up — Marc said forty-nine a month."*

1. **Capture** ([app/services/audio.py](app/services/audio.py)). The mic stream
   runs through Silero VAD, which segments it into an utterance; faster-whisper
   transcribes it on a worker thread; SpeechBrain ECAPA voiceprints attribute
   the speaker. An ingest filter drops Whisper's silence-hallucinations before
   they pollute memory. The result is one `Event` (modality `audio`, the
   transcript, the speaker, a confidence, and a link to the saved WAV clip),
   published to the bus.
2. **Memory** ([app/services/memory.py](app/services/memory.py)). The Memory
   Engine subscribes to the bus: the event is written to the SQLite timeline
   (`data/quill.db`) and embedded into LanceDB, so it's findable later by
   *meaning* ("what did Marc say about pricing?"), not just keywords.
3. **Consolidation** ([app/services/consolidation.py](app/services/consolidation.py)).
   A durable background worker merges adjacent utterances into a conversational
   **turn**, so extraction sees whole thoughts, not fragments. Turns group
   further into **sessions** at long silence gaps.
4. **Extraction** ([app/services/extractor.py](app/services/extractor.py)). A
   windowed pass over settled turns distills structured facts: a **commitment**
   (*owe Justin a follow-up*) and a **claim** (*$49/mo*), each with the verbatim
   quote and a `source_event_id` pointing back at the audio. Person resolution
   (exact → prefix → embedding) works out who "Justin" is.
5. **Review** (the [Console](#the-memory-console--the-trust-layer)). The fact
   appears in the Tasks tab with approve / edit / dismiss / done controls and
   inline playback of the source clip. Correcting a misheard name here is the
   human signal the whole system learns from.
6. **Graph & reflection.** A deterministic rebuild wires the fact into the
   knowledge graph (Justin ↔ commitment ↔ evidence); the daily reflection later
   notices if it's still open and surfaces it as an `open_loop` insight.
7. **Act** — you ask in chat (or vinceo.ai proactively offers): *"email Justin the
   pricing follow-up."* The router picks the browser surface, the agent pulls
   "$49/mo" straight from memory (no re-explaining), drafts the email, and stops
   at a source-grounded approval packet — Action / To / Subject / Body /
   **Why / Source** — with **Approve / Edit / Cancel**. Nothing sends until you
   say so.

Every hop in that chain is inspectable after the fact: the clip, the transcript,
the turn, the fact, the packet, and your verdict are all in `data/quill.db`.

---

## The Event: one schema for everything

Every input modality is independent and normalizes what it perceives into a
single **`Event`** ([app/events.py](app/events.py)) pushed onto an in-process
**`EventBus`** (a minimal async pub/sub). Subscribers react without knowing
about each other:

```
        Webcam ─▶ Vision pipeline ──┐
    Laptop mic ─▶ Audio pipeline ───┤
 Screen+clicks ─▶ Desktop capture ──┤
         Phone ─▶ Notif. pipeline ──┤
                                    ├─▶ EventBus ─▶ Memory Engine ─▶ SQLite + LanceDB
   watchers (todo/phone/task) ◀─────┘                   │
        │                                               │ semantic search
        ▼          consolidate ─▶ extract facts ─▶ rebuild graph ─▶ reflect (daily)
  proactive offers               (durable background worker)
        │                                               │
        ▼                                               ▼
   /chat ──▶ Planner ──▶ Browser / Desktop / Phone agent (memory-grounded, approval-gated)
```

The schema is the lingua franca: `time, modality, raw, summary, source,
confidence, people, tasks, entities, meta`. `Modality` is one of `audio`,
`vision`, `notification`, `input`, or `system`; `source` says which pipeline
produced it (`audio.whisper`, `vision.claude`, `desktop.screen`,
`desktop.click`, …). Everything downstream — memory, search, facts, activities,
reflection, the graph, the agents, the console — speaks `Event`. Adding a new
capture modality means publishing `Event`s to the bus; no downstream code
changes.

The bus supports async producers (`publish`) and synchronous ones on other
threads (`publish_nowait`, used by the audio-capture and notification-poll
threads), so capture never blocks on downstream processing.

---

## Layer 1 — Perceive

### Audio (M1)

**Files:** [app/services/audio.py](app/services/audio.py) ·
[app/services/speakers.py](app/services/speakers.py) ·
[app/services/ingest_filter.py](app/services/ingest_filter.py)

Microphone → **Silero VAD** (ONNX) segments the stream into utterances →
**faster-whisper** (CTranslate2, no PyTorch/GPU) transcribes → `Event`.

- **Two-thread design.** Capture runs on the `sounddevice` audio thread doing
  only cheap work (VAD + buffering); transcription runs on a separate worker
  thread, so audio frames are never dropped waiting on the ASR.
- **Ingest hygiene.** Whisper hallucinates on silence (endless *"Thank you."*).
  The ingest filter uses per-segment `avg_logprob` and `no_speech_prob` to
  hard-drop hallucinations and confident duplicates, and flags-but-keeps
  low-confidence utterances so the Console can surface them. Disable with
  `QUILL_INGEST_FILTER=0`.
- **Speaker ID.** SpeechBrain ECAPA-TDNN voice embeddings. Anonymous out of the
  box (`Speaker 1`, `Speaker 2`, … clustered by cosine similarity); named once
  you enroll someone (`python scripts/enroll_speaker.py Marc`). Voiceprints
  persist as `.npy` files.
- **Provenance chain.** Each utterance keeps its evidence trail — raw clip →
  enhanced audio → transcript → the ordered correction log — addressable per
  event (`GET /console/provenance/{event_id}`), so any fact can be traced to
  the exact sound it came from.

### Vision (M2)

**Files:** [app/services/vision.py](app/services/vision.py) ·
[app/services/vlm.py](app/services/vlm.py) ·
[app/services/vlm_gemini.py](app/services/vlm_gemini.py)

Webcam → OpenCV frame selection → VLM structured extraction → `Event`.

- **Frame selection is the cost control.** Capture continuously, but analyze a
  frame only when the scene *changes* (mean absolute pixel difference over a
  threshold), rate-limited between `min_interval_s` and `max_interval_s`.
  Dark/covered-lens frames are skipped so a black frame never burns a VLM call;
  a frame-quality gate tells "the camera is broken" apart from "the model
  failed".
- **Structured output.** The VLM returns a JSON schema: description, verbatim
  OCR text, people count, objects, scene type — plus **page understanding**: a
  shown page is classified (`todo_list / questions / notes / table / code /
  diagram …`) and its discrete `items` transcribed with per-item confidence.
  The `todo_list` classification is what fires the proactive see → offer → act
  loop.
- **Local-first.** Frames go to a local Ollama model (`minicpm-v`) first;
  Claude is the paid fallback — see
  [the escalation ladder](#local-first-models--the-escalation-ladder).
- **Windows camera quirks handled.** The default MSMF backend often fails;
  vinceo.ai uses **DirectShow** (`dshow`) and forces **MJPG** to avoid mis-strided
  green frames. All overridable.

### Desktop capture (screen + clicks, opt-in)

**File:** [app/services/desktop_capture.py](app/services/desktop_capture.py)

Passive observation of your own screen — off by default, enabled with
`QUILL_DESKTOP_CAPTURE=1` (or `python run_all.py --desktop-capture`). No
keystrokes are captured.

- **Screen frames** are motion-gated like webcam vision and go through the same
  local-first VLM → `Event` (modality `vision`, source `desktop.screen`, with
  the focused window title in `meta.window`).
- **Mouse clicks** become lightweight `Event`s (modality `input`, source
  `desktop.click`) with coordinates, button, window title, and a context crop
  saved to `data/desktop_frames/`. Click crops are described only by the local
  model, and only if you opt in — clicks never trigger a paid call.
- Downstream, these events fold into **activity blocks** (see
  [Layer 3](#layer-3--understand)) and get their own Desktop and Activity tabs
  in the Console.

### Phone notifications (Windows)

**Files:** [app/services/notifications.py](app/services/notifications.py)
(capture) · [app/services/phone_link.py](app/services/phone_link.py) (control) ·
[app/services/phone_watcher.py](app/services/phone_watcher.py) (proactive)

Microsoft ships no public Phone Link API, so vinceo.ai reads iPhone notifications
the way they actually surface: as ordinary **Windows toast notifications** from
the Phone Link app, via `UserNotificationListener` (`winsdk`). Each becomes an
`Event` with `Modality.NOTIFICATION` in the same pipeline as speech and vision.
App-filtered (Phone Link only by default; widen with `QUILL_NOTIFICATION_APPS`),
needs a one-time OS grant (Settings → Privacy → Notifications → allow Python),
Windows-only and cleanly off elsewhere. Sending back out (SMS) is the
[Phone Link agent](#layer-5--act).

---

## Layer 2 — Remember

**Files:** [app/services/memory.py](app/services/memory.py) ·
[app/storage.py](app/storage.py) (SQLite) ·
[app/vectorstore.py](app/vectorstore.py) (LanceDB) ·
[app/services/embeddings.py](app/services/embeddings.py)

The Memory Engine subscribes to the bus; every `Event` is:

- **Written to SQLite** (`data/quill.db`) — a durable timeline that reloads on
  startup, so transcripts and frames survive restarts. The same database holds
  facts, relations, reflections, turns, sessions, activities, jobs, and agent
  runs — one file, everything joinable.
- **Embedded into LanceDB** with local sentence-transformers
  (`all-MiniLM-L6-v2`, 384-d, CPU) for **semantic search** — *"what did I see
  on the whiteboard?"* matches a frame described as *"Series A timeline"* even
  with zero word overlap. Falls back to substring search if disabled;
  un-indexed events are backfilled on startup. Extracted facts are indexed into
  the same store, so search returns episodes and facts side by side.
- **Linked to its raw artifact** — WAV clips and JPEGs are saved to disk and
  referenced from `meta.audio_path` / `meta.frame_path`; the `/artifact`
  endpoint serves them back, path-confined to the data directory.

---

## Layer 3 — Understand

### Consolidation & the durable job queue

**Files:** [app/services/consolidation.py](app/services/consolidation.py) ·
[app/services/sessions.py](app/services/sessions.py) ·
[app/services/worker.py](app/services/worker.py)

Adjacent utterances merge into **turns** (a new turn starts after a silence gap
exceeds `QUILL_CONSOLIDATE_MAX_GAP_S`); turns group into **sessions** at
long-gap boundaries. All heavy processing — `consolidate` → `extract` →
`graph`, plus time-driven `reflect_daily` — runs on **one `jobs` table and one
background worker thread**, off the capture path. It survives crashes,
coalesces bursts (a flurry of new audio queues exactly one re-consolidation,
not dozens), and drains backlogs in small batches. No Celery, no Redis. The
chain is wired in [app/main.py](app/main.py) at startup.

### Facts — tasks, commitments, claims

**Files:** [app/services/extractor.py](app/services/extractor.py) ·
[app/services/resolution.py](app/services/resolution.py)

The heart of "captures **and** understands." A windowed pass over settled turns
produces **tasks**, **commitments**, and **claims** via structured LLM output.
Every fact carries a `source_span` (verbatim quote) and a `source_event_id`.
Person resolution cascades exact match → prefix (Chris/Christopher) → embedding
similarity; fuzzy merges are recorded as aliases, never silently collapsed.
Tasks have a lifecycle (`open → done → cancelled`); vision to-do items become
task facts too. **The review loop is the training layer**: the Console exposes
every fact with approve / edit / dismiss / done and inline source-clip playback.

### Desktop activities — "what was I doing?"

**File:** [app/services/activity.py](app/services/activity.py)

The desktop-capture stream folds into **activity blocks** per app-focus
stretch: app, window titles, screen/click counts, and a summary — the same
derived-table pattern as turns, built by the same worker. Activities are
multimodal where the data allows (what you *heard* and *saw* during the block
is joined in), ground the chat's "recent desktop activity" context, and feed
the [anticipation watcher](#proactive-behavior). Browse them in the Console's
Activity tab; each block expands to its underlying evidence rows.

### Reflection — durable intelligence

**File:** [app/services/reflector.py](app/services/reflector.py)

The extractor answers *"what was said?"*; reflection answers *"what changed,
what matters, what is unresolved, what should happen next?"* over a period. It
reads the facts the pipeline already produced and emits structured, individually
reviewable **insights** (`change · pattern · risk · open_loop · project_update ·
relationship_update · policy · recommendation`).

- **Grounded:** the model may only cite fact ids it was handed; invented ids
  are dropped before persistence. No ungrounded oracle.
- **Reviewable:** each insight gets approve / edit / dismiss /
  **convert-to-task** — a recommendation becomes a task only when a human
  converts it.
- **Time-driven:** a daily reflection auto-enqueues on startup if the last one
  is stale (>20h); run on demand via `POST /reflect/run`.

### Knowledge graph (M5 v1)

**File:** [app/services/graph.py](app/services/graph.py)

Turns the nodes the facts pipeline already produces (people, facts, events)
into a traversable graph — **deterministically, no LLM calls**. `rebuild()`
recomputes typed person↔fact edges (`responsible_for` / `committed` / `owed`),
mention edges, provenance edges, and weighted co-occurrence; *asserted* edges
(e.g. `works_at`, including those seeded by onboarding) are preserved across
rebuilds. `context_for_person(name)` answers *"who is this, what's open with
them, who do they come up with"* in one traversal — relational questions flat
text search can't do. API: `GET /graph/context?name=…` · `POST /graph/rebuild` ·
`GET /graph/stats`.

### Onboarding — don't start cold

**File:** [app/services/onboarding.py](app/services/onboarding.py)

A one-time guided profile (identity → people → work → rhythm) at
[/onboarding](http://127.0.0.1:8000/onboarding) seeds people, entities,
asserted graph edges, and accepted claims, so a new user's vinceo.ai knows their
world on day one. Idempotent and delta-aware: edit and re-save later, only new
answers are ingested. A JSON backup lands at `data/onboarding_profile.json`.

---

## Layer 4 — Decide

**Files:** [app/services/agent_planner.py](app/services/agent_planner.py) ·
[app/services/agent_log.py](app/services/agent_log.py) ·
[app/services/readiness.py](app/services/readiness.py) ·
[app/services/multitask.py](app/services/multitask.py)

The brain above the hands. Behind `QUILL_PLANNER=1`, the **Personal Agent
Layer** compiles a user goal into a grounded, risk-classified **Plan** of
`ActionPacket`s before anything touches a browser:

```
Facts / Graph / Reflections / Commitments
        │  select_context()   (the 3 relevant memories, not 30)
        ▼
compile(goal) → Plan
   1. select_context   2. decompose   3. per step: choose compiler → ActionPacket
   4. classify_risk    (read/draft = low … send/buy = high, delete = blocked)
        │
        ▼
Execution surfaces (browser / desktop / phone) → human approval → Recorder logs the verdict
```

- **Risk is a table, not an LLM guess.** A precise, inspectable `RISK_TABLE`
  maps action verbs to `low / medium / high / blocked`: `blocked` never reaches
  a surface, `high` always forces the approval gate, and any brush with a
  sensitive domain (medical / financial / password / …) escalates.
- **Action readiness.** Each open task also gets a unified, risk-aware
  readiness score with decision bands — `auto / offer / review / hold` — the
  same score the proactive offer gate keys off (`GET /console/readiness`).
- **Multi-task fan-out.** A mixed message ("email Justin, then text Marc")
  is split *before* routing, each task dispatched to its own surface in
  dependency order.
- **Cognitive agents are plug-ins.** An `IntentCompiler` turns *(goal,
  context)* into an `ActionPacket`; a Writing Agent (drafts bodies from memory
  instead of asking "what should I say?") and a Meeting Agent (read-only
  briefing from the relationship graph) ship today; adding one is a single
  `register()` call.
- **The substrate.** [agent_log.py](app/services/agent_log.py) persists every
  run, compiled packet, and human verdict — including the *edit* revision, the
  richest training signal — surfaced at `GET /console/agent-runs`.
- **Best-effort & reversible.** If the Planner is off or errors, callers fall
  back to handing the raw goal to the browser agent.

---

## Layer 5 — Act

### Browser agent — "Exec.AI"

**Directory:** [browser_agent/](browser_agent/) · **standalone:**
[exec_webapp.py](exec_webapp.py) · **memory bridge:**
[app/services/agent_bridge.py](app/services/agent_bridge.py)

A self-contained autonomous web agent with a mature **route → plan → execute →
verify** loop over deterministic Playwright actions.

- **Routing.** An envelope (intent, `requires_browser`, `requires_approval`,
  `site`, `surface`) decides answer-directly vs. drive-the-browser vs. hand off
  to the desktop or phone agent.
- **Tiered models** ([browser_agent/config.py](browser_agent/config.py)):
  Sonnet for routing/execution (the hot path), Opus for planning and
  escalation, Haiku for high-volume yes/no verification. This ladder is
  Claude-internal by design — it does not route through the local-first text
  policy below.
- **Executor vision.** DOM + screenshot; Claude reads the pixels itself.
  Adaptive — the screenshot is attached only when the accessibility tree is
  thin or the agent is stuck. Proven load-bearing: an eval renders an access
  code inside a `<canvas>` (no DOM text); the agent fails text-only and
  succeeds with vision on.
- **Source-grounded approval packets.** Irreversible steps stop at a structured
  Action / To / Subject / Body / **Why / Source** packet with **Approve / Edit /
  Cancel** (an edit feeds a revision back for re-drafting).
- **Modes & safety.** 7 task-specific policies (email / calendar / research /
  shopping / crm / form / general), each only ever *adding* approval patterns
  to the global commit net; research mode is read-only. Dry-run levels
  (`plan / navigate / draft / approval / full`) cap how far any run may go. A
  failure taxonomy (login / captcha / timeout / wrong page / no-progress) maps
  blocks to recovery actions; it refuses to enter credentials or solve
  CAPTCHAs.
- **Learning + sessions.** Procedural memory recalls what worked for
  `intent@site`; a named persistent browser profile (`QUILL_AGENT_PROFILE`)
  reuses your hand-authenticated Gmail/CRM session; `QUILL_AGENT_CHANNEL=chrome`
  uses real installed Chrome.
- **Memory-grounded.** The agent semantic-searches vinceo.ai's own timeline, so
  *"follow up on what Marc said about pricing"* pulls the "$49/mo" it overheard.

### Desktop agent

**Directory:** [desktop_agent/](desktop_agent/)
([guards.py](desktop_agent/guards.py) · [driver.py](desktop_agent/driver.py))

OS-level control for tasks that live in apps rather than the web. There is no
browser sandbox here, so **the allowlist *is* the sandbox** — layered guards:
a path jail (`QUILL_DESKTOP_JAIL`), an app allowlist (launch by key, never raw
path), a shell-verb allowlist (read verbs auto-run, mutating verbs
approval-gated, everything else blocked), a hard-block list no prompt can
unlock (`rm`, `del`, `format`, `reg`, `sudo`, shell metacharacters, secret-path
markers, `..`), args-as-list with `shell=False`, tiered human approval, an
audit log, per-task action budgets, and command timeouts. The router's
`surface == 'desktop'` dispatches into a guarded observe→act loop
(also `POST /desktop`). Approval always comes from the live human, never from
memory.

### Phone Link agent

**File:** [app/services/phone_link.py](app/services/phone_link.py) ·
**scripts:** [scripts/phone_link/](scripts/phone_link/)

The outbound counterpart to notification capture: drives the installed Windows
**Phone Link** UI via PowerShell + UI Automation (adapted from the MIT-licensed
`phonelink-mcp-server`) to launch it, read conversations, and **send SMS** from
the laptop — after the approval gate. *"Text Justin I'll be late"* → router
picks `surface = phone_link` → approval → sent. Windows-only
(`QUILL_PHONE_LINK=0` to disable), also reachable via `POST /phone`.

---

## Local-first models & the escalation ladder

**Files:** [app/services/vlm.py](app/services/vlm.py) (vision) ·
[app/services/ollama_text.py](app/services/ollama_text.py) +
[app/services/model_router.py](app/services/model_router.py) (text) ·
[app/services/escalate_log.py](app/services/escalate_log.py) ·
[app/services/model_log.py](app/services/model_log.py)

The same pattern twice — **local model first, Claude only when it matters, and
every escalation logged as future training data**:

- **Vision** (`QUILL_VISION_LOCAL=1`, default on): every selected frame goes to
  Ollama `minicpm-v` (strong local OCR). Claude is called only for high-stakes
  pages (`todo_list` / `form` / `code`), low local confidence, weak capture
  quality, or when Ollama is unreachable.
- **Text** (`QUILL_TEXT_LOCAL=1`, default **off**): router-served text calls
  (chat, extract, reflect, activity summaries) run on a local Ollama text model
  (`QUILL_TEXT_LOCAL_MODEL`; `qwen2.5:7b-instruct` benched champion over
  `llama3.2` — +19pt pass rate, ⅓ fewer escalations) and escalate to Claude
  when the local model is unreachable/errors, its output doesn't parse, its
  *calibrated* confidence falls below `QUILL_TEXT_ESCALATE_MIN_CONF`, the
  answer looks suspect (a refusal despite substantive context, or an echo of
  the request), or the task is in the high-stakes set (default: `plan`). Off
  keeps Claude-only routing, unchanged.
- **Fail open, never double-bill.** Local down → straight to Claude; Claude
  failing after a usable local answer → keep the local answer. A local success
  costs zero paid calls.
- **The distill trail.** Each escalation appends a row to
  `data/escalate_distill.jsonl` — task, reason (`local_error` /
  `low_confidence` / `parse_failure` / `high_stakes_task` / …), the local
  attempt, and the parent answer (prompts truncated; frames by path, never
  bytes). That file is the dataset for eventually distilling Claude's judgment
  into the local models. Summary at `GET /console/escalate`.
- **Telemetry.** Every model call (local and paid) is logged to
  `data/model_calls.jsonl` with latency and estimated cost, aggregated at
  `GET /console/models` — the measure of what local-first actually saves.

---

## The learning loop — from verdicts to weights

**Files:** [app/services/few_shot.py](app/services/few_shot.py) ·
[app/services/grounding.py](app/services/grounding.py) ·
[app/services/self_quiz.py](app/services/self_quiz.py) ·
[scripts/bench_text.py](scripts/bench_text.py) ·
[scripts/distill_curate.py](scripts/distill_curate.py) ·
[scripts/train_lora.py](scripts/train_lora.py) · design doc:
[phase3_lora_architecture.md](phase3_lora_architecture.md)

The distill trail isn't just a log — it's a complete, closed learning loop.
The local model gets measurably better from nothing but normal use:

```
every chat answer → 👍/👎/✏️ verdict → distill row (data/escalate_distill.jsonl)
      │
      ├─ few-shot (minutes): similar verified answers are retrieved into the
      │    LOCAL prompt as worked examples — lessons apply the same day
      ├─ calibration: retrieval evidence floors the model's miscalibrated
      │    self-confidence, but only when its answer AGREES with the verified
      │    answer it matched; refusal-despite-context and echo answers force
      │    escalation at any confidence
      ├─ self-quiz (idle): the model quizzes itself on human-approved facts;
      │    failures become auto-labeled lessons whose gold already exists —
      │    zero human labeling, zero paid calls
      ├─ bench (on demand): replay labeled rows, score vs the human gold by
      │    embedding similarity — "is it getting better?" is a number
      └─ LoRA (periodic, ~100+ pairs): one command curates → trains a QLoRA
           adapter (Unsloth under WSL2) → packages a dated Ollama tag → gates
           on a held-out exam vs the incumbent. No promotion without numbers;
           rollback is a config flip.
```

Rules learned the hard way, now enforced in code: **refusal-shaped answers
are never taught as examples** (correct answer ≠ good exemplar — they poison
the model into refusing everything); **training uses the clean stored prompt,
never the few-shot-augmented one** (the model must learn the skill, not the
crutch); **the bench holdout is excluded from both retrieval and training**
(the exam is never in the study guide); and **a 👍 on a kept-local answer
makes the local text itself the verified gold** — every answer bubble is
labelable, so wrong-but-confident local answers are correctable, and every
verdict is a training pair.

Every answer also shows its work: a collapsible **Sources:** line names the
grounding sections it drew from (person graph / open tasks / screen & camera /
timeline memories / recent activity), so a bad answer is diagnosable at a
glance — wrong drawer vs. right drawer, wrong words.

This is also the product's onboarding invariant: **new users never train
anything.** Day one runs stock models at parent quality; personalization
accrues silently from natural verdicts, and the per-user adapter is a derived
artifact of each install's own trail — user-specificity lives in data and
weights, never in code (`tests/test_no_user_tailoring.py` enforces it).

---

## The Memory Console — the trust layer

**Where:** <http://127.0.0.1:8000/console> (all endpoints + the embedded UI live
in [app/api/routes.py](app/api/routes.py))

vinceo.ai asks you to trust what it heard, saw, and inferred — the Console is
where you check. One page, tabbed:

| Tab | What you see |
|---|---|
| **All / Audio / Vision** | The raw event timeline, newest first: transcript or frame description, speaker, confidence, and the **actual clip or frame** inline. Semantic search across everything. |
| **Desktop** | Only desktop-capture events (`source=desktop.*`) — screen analyses and clicks, badged `screen`/`click`, window title shown, click-crop thumbnails. |
| **Activity** | "What was I doing?" blocks: app badge, window pills, `N screens · N clicks`, duration, summary — each expandable to the underlying evidence rows. |
| **Turns / Sessions** | Consolidated conversation view, with per-turn audio playback. |
| **Tasks** | The fact review queue: open tasks and commitments with approve / done / edit / dismiss and verbatim source quote + clip. |
| **Reflection** | The latest daily reflection and its insights, each with approve / edit / dismiss / convert-to-task. |
| **Audio Health** | Utterance/drop rates, ASR latency, SNR/clipping quality mix, low-confidence and speaker-unknown rates, offer surfaced/accept rates. |
| **Low-confidence** | One click to see exactly what the system is *unsure* about. |

Vision rows carry a **provider pill** (which VLM produced the description, with
the routing reason on hover) — local-first routing, validated per row. Rebuild
buttons re-derive turns / sessions / activities on demand. Related pages:
`/ui` (live chat + offers), `/desktop-access` (desktop capture control +
metrics), `/onboarding` (profile setup), `/docs` (Swagger).

---

## Proactive behavior

All watchers are gated by `QUILL_AGENT` (set `0` to make `/chat` a pure memory
retriever and silence every offer). Offers are exactly that — a yes/no in chat;
nothing runs without your reply.

- **To-do watcher** ([todo_watcher.py](app/services/todo_watcher.py)) — vision
  classifies a page as `todo_list` → vinceo.ai offers in chat to run the items
  through the browser agent (debounced by items-hash + cooldown). The fully
  autonomous **see → offer → act** trigger.
- **Task offer** ([task_offer.py](app/services/task_offer.py)) — spoken tasks
  surface as "run this?" offers, gated by a two-signal readiness check so it
  isn't chatty.
- **Phone watcher** ([phone_watcher.py](app/services/phone_watcher.py)) — an
  iPhone notification arrives → offer to reply or open the thread
  (`QUILL_PHONE_WATCH=0` to disable).
- **Anticipation** ([anticipation.py](app/services/anticipation.py), opt-in
  `QUILL_ANTICIPATE=1`) — heuristic likely-next suggestions from your recent
  activity blocks (app-transition patterns + open tasks), offered only after an
  idle gap, with cooldowns.

---

## Quick start

### 1. Prerequisites

- **Python 3.10+** (uses `X | None` type hints).
- **Windows 11** is the primary target (audio, vision, desktop capture, and
  Phone Link are developed there). macOS/Linux run the capture + memory + agent
  stack; the Windows-only pieces degrade gracefully.
- A working **microphone** and **webcam** for live capture (optional — you can
  run headless and drive it over the API).
- An **Anthropic API key** for the Claude tiers (vision fallback, extraction,
  reflection, the browser agent). Everything local runs without it; paid
  features simply skip if the key is missing.

### 2. Install

```powershell
# from the project root
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # Windows PowerShell
pip install -r requirements.txt
```

**Pre-download the speech models** (one-time, ~460 MB, cached under
`~/.cache/huggingface`) so the first live run starts instantly:

```powershell
python scripts/download_models.py            # the configured Whisper model + VAD
python scripts/download_models.py base small # or specific sizes
```

**One-time browser setup** (for the agent):

```powershell
playwright install chromium
```

### 3. Add your API key

Put it in a `.env` (or `.credentials.env`) at the project root — **never commit
this file**:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
# optional: enables the Gemini vision backend
GOOGLE_API_KEY=...
```

Without a key the system degrades gracefully — frames are still captured and
saved, just not analyzed by Claude; extraction and reflection are skipped. It
never crashes.

### 4. (Optional) local-first models

Install [Ollama](https://ollama.com) and pull the local models:

```powershell
ollama pull minicpm-v    # vision (default on)
ollama pull llama3.2     # text  (opt-in: QUILL_TEXT_LOCAL=1)
```

If Ollama isn't running, everything falls back to Claude automatically — this
step is safe to skip.

### 5. Run everything

```powershell
python run_all.py
```

One command starts the Memory Engine, live audio + vision + notification
capture, the FastAPI server, and the browser-agent UI. Then open:

- **Memory Console** — <http://127.0.0.1:8000/console>
- **Live chat UI** — <http://127.0.0.1:8000/ui>
- **Onboarding (first run)** — <http://127.0.0.1:8000/onboarding>
- **API docs (Swagger)** — <http://127.0.0.1:8000/docs>
- **Browser-agent UI** — <http://127.0.0.1:5000>

`Ctrl+C` stops everything, including the agent's Chromium.
**Flags:** `--no-audio` · `--no-vision` · `--no-notifications` · `--no-browser`
· `--desktop-capture` · `--browser-headless` · `--port` · `--browser-port` ·
`--host`.

---

## Usage examples

### Run one piece at a time

```powershell
python run_audio.py                 # live transcription in the terminal
python run_vision.py                # live webcam understanding
uvicorn app.main:app --reload       # API server alone
python exec_webapp.py               # browser agent standalone (with memory bridge)
```

### Teach it who's talking

```powershell
python scripts/enroll_speaker.py Marc          # record 10s of Marc from the mic
python scripts/enroll_speaker.py Justin 15     # 15 seconds
python scripts/enroll_speaker.py Marc clip.wav # or from a 16 kHz mono WAV
```

### Drive the hear → act loop from the API

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

### Cap how far a run may go (safe demos)

Prefix a message with a dry-run level, or set `AGENT_DRY_RUN` globally:

```jsonc
{ "message": "/draft reply to Marc about the Series A timeline" }
// levels: /plan · /navigate · /draft · /approval (default) · /full
```

`/draft` prepares everything but stops at the first commit gate without
prompting — perfect for a demo that must never actually send.

### Search your memory

```bash
curl -s "http://127.0.0.1:8000/memory/search?q=whiteboard%20series%20A"
```

---

## Interfaces & API

| Surface | What it is |
|---|---|
| **`/console`** | The Memory Console (see [above](#the-memory-console--the-trust-layer)). |
| **`/ui`** | Live chat — watch capture happen, see offers, reply, approve. |
| **`/desktop-access`** | Desktop-capture control + metrics page. |
| **`/onboarding`** | One-time guided profile setup. |
| **`/facts` API** | Programmatic review — list/filter, `open_tasks`, approve / dismiss / done / edit. |
| **`/reflections` API** | List reflections & insights; approve / dismiss / edit / convert; `POST /reflect/run`. |
| **`/chat`** | Dispatch a turn to the agents; memory-grounded; non-blocking — poll `/chat/poll`, answer via `/chat/answer`, `/chat/new` for a fresh session. |
| **`/graph`** | `context` / `rebuild` / `stats` over the knowledge graph. |
| **Browser-agent UI** | The Exec.AI web chat (Flask) at <http://127.0.0.1:5000>. |

**Full endpoint list** (see [app/api/routes.py](app/api/routes.py)):

```
GET  /health
POST /audio/start · /audio/stop
POST /vision/start · /vision/stop
POST /notifications/start · /notifications/stop
POST /desktop-capture/start · /desktop-capture/stop
GET  /memory · /memory/search?q=
GET  /console · /console/events (modality/source/q filters) · /console/turns
GET  /console/sessions · /console/activity · /console/activity/events?ids=
POST /console/consolidate · /console/sessions/rebuild · /console/activity/rebuild
GET  /console/jobs · /console/models · /console/escalate · /console/readiness
GET  /console/cognition · /console/camera-health · /console/audio-health
GET  /console/provenance/{event_id}
GET  /console/agent-runs · /console/agent-runs/{run_id}
GET  /artifact                         (path-confined raw clip/frame serving)
GET  /graph/context · POST /graph/rebuild · GET /graph/stats
GET  /facts · /facts/open_tasks
POST /facts/{id}/approve · /dismiss · /done · /edit
POST /reflect/run
GET  /reflections · /reflections/list
POST /reflection_items/{id}/approve · /dismiss · /edit · /convert
POST /chat · GET /chat/poll · POST /chat/answer · POST /chat/new
POST /desktop · POST /phone
GET  /onboarding · /onboarding/status · /onboarding/profile
POST /onboarding/template · /onboarding/ingest
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
| **Vision capture** | OpenCV | webcam/screen + frame selection |
| **Vision / VLM** | Ollama `minicpm-v` (local) → Claude fallback; Gemini optional | structured frame understanding |
| **Local text** | Ollama `llama3.2` (opt-in) → Claude fallback | chat / extract / reflect / summaries |
| **Phone notifications** | winsdk (`UserNotificationListener`) | Windows toast / iPhone mirror capture |
| **Phone control** | PowerShell + UI Automation | drive Phone Link (launch / read / send SMS) |
| **Embeddings** | sentence-transformers (MiniLM, local) | semantic search vectors |
| **Vector store** | LanceDB (embedded, file-based) | meaning-based retrieval |
| **Timeline store** | SQLite | events, facts, relations, reflections, activities, jobs, agent runs |
| **Browser agent** | Playwright (Chromium) + Claude (Sonnet / Opus / Haiku) | autonomous web actions |
| **Agent UI** | Flask | browser-agent web chat |

---

## Configuration reference

All settings are read from the environment / `.env` with sane defaults (see
[app/config.py](app/config.py), [desktop_agent/config.py](desktop_agent/config.py),
[browser_agent/config.py](browser_agent/config.py)). A `.credentials.env` is
loaded after `.env` with override, for secrets.

> **Naming note:** the product is **vinceo.ai**, but env vars and the SQLite file
> keep the `QUILL_` / `quill.db` prefix from the project's original name —
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
| `QUILL_CAMERA_BACKEND` | `dshow` (Win) | DirectShow is the reliable Windows backend |
| `QUILL_CAMERA_FOURCC` | `MJPG` (Win) | avoids green/noise frames |
| `QUILL_CAMERA_WIDTH` / `_HEIGHT` | `1280` / `720` | requested resolution (`0` = don't request) |
| `QUILL_CAMERA_WARMUP` | `20` | frames discarded so the sensor auto-exposes |
| `QUILL_VISION_MIN_BRIGHTNESS` | `8` | skip frames darker than this (0–255 mean) |
| `QUILL_VISION_MIN_INTERVAL_S` | `5` | analyze at most this often |
| `QUILL_VISION_MAX_INTERVAL_S` | `30` | force a frame at least this often |
| `QUILL_VISION_MOTION_THRESHOLD` | `12` | mean abs frame-diff = "scene changed" |
| `QUILL_VISION_JPEG_QUALITY` | `80` | saved-frame quality |
| `QUILL_VISION_MODEL` | `claude-opus-4-8` | Claude vision model (fallback tier) |
| `QUILL_VISION_LOCAL` | `1` | local-first VLM via Ollama |
| `QUILL_VISION_LOCAL_MODEL` | `minicpm-v` | local Ollama vision model |
| `QUILL_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama endpoint (shared with text) |
| `QUILL_VISION_LOCAL_TIMEOUT_S` | `60` | local VLM timeout |
| `QUILL_VISION_ESCALATE_MIN_CONF` | `0.6` | escalate below this confidence |
| `QUILL_VISION_ESCALATE_MIN_CAPTURE` | `0.6` | escalate on weak frame quality |
| `QUILL_ESCALATE_LOG` | `1` | append local→parent distill rows |
| `QUILL_ESCALATE_LOG_PATH` | `data/escalate_distill.jsonl` | distillation trail |

### Local-first text (chat / extract / plan)

| Var | Default | Notes |
|---|---|---|
| `QUILL_TEXT_LOCAL` | `0` | local-first TEXT via Ollama; off = Claude-only, unchanged |
| `QUILL_TEXT_LOCAL_MODEL` | `llama3.2` | local Ollama text model |
| `QUILL_TEXT_LOCAL_TIMEOUT_S` | `45` | local text timeout |
| `QUILL_TEXT_ESCALATE_MIN_CONF` | `0.6` | escalate below this self-reported confidence |
| `QUILL_TEXT_HIGH_STAKES_TASKS` | `plan` | comma-separated tasks that always escalate |

### Desktop capture (passive screen + clicks)

Off by default; enable with `QUILL_DESKTOP_CAPTURE=1` or
`python run_all.py --desktop-capture`. No keystrokes are captured.

| Var | Default | Notes |
|---|---|---|
| `QUILL_DESKTOP_CAPTURE` | `0` | master switch |
| `QUILL_DESKTOP_CAPTURE_SCREEN` | `1` | motion-gated screen frames → VLM → events |
| `QUILL_DESKTOP_CAPTURE_CLICKS` | `1` | mouse clicks → events (coords + window + crop) |
| `QUILL_DESKTOP_CAPTURE_CLICK_VLM` | `0` | opt-in local-only describe of click crops (never Claude) |
| `QUILL_DESKTOP_CAPTURE_MIN_INTERVAL_S` | `8` | analyze screen at most this often |
| `QUILL_DESKTOP_CAPTURE_MAX_INTERVAL_S` | `45` | force a frame at least this often |
| `QUILL_DESKTOP_CAPTURE_MOTION_THRESHOLD` | `10` | mean abs frame-diff = "changed" |
| `QUILL_DESKTOP_CAPTURE_MAX_WIDTH` | `1280` | downscale long edge before VLM |
| `QUILL_DESKTOP_CAPTURE_CLICK_CROP` | `420` | crop size (px) around a click |
| `QUILL_DESKTOP_CAPTURE_CLICK_VLM_MIN_S` | `8` | min seconds between click VLM calls |
| `QUILL_DESKTOP_CAPTURE_CLICK_DEDUP_PX` | `12` | ignore near-duplicate clicks within radius |
| `QUILL_DESKTOP_CAPTURE_CLICK_DEDUP_S` | `0.35` | …within this window |

### Anticipation (likely-next from activities)

| Var | Default | Notes |
|---|---|---|
| `QUILL_ANTICIPATE` | `0` | master switch for likely-next chat offers |
| `QUILL_ANTICIPATE_MIN_CONF` | `0.6` | min transition/pattern confidence to offer |
| `QUILL_ANTICIPATE_COOLDOWN_S` | `600` | don't repeat a suggestion within this window |
| `QUILL_ANTICIPATE_IDLE_S` | `90` | newest activity must be idle this long first |
| `QUILL_ANTICIPATE_HISTORY` | `40` | recent activities to score |
| `QUILL_ANTICIPATE_MIN_ACTIVITIES` | `3` | minimum blocks before scoring |
| `QUILL_ANTICIPATE_MIN_TRANSITIONS` | `2` | min A→B counts from the current app |
| `QUILL_ANTICIPATE_MAX` | `1` | max candidates per pass |

### One-time onboarding

| Var | Default | Notes |
|---|---|---|
| `QUILL_ONBOARDING` | `1` | the once-only profile flow |
| `QUILL_ONBOARDING_PROFILE` | `data/onboarding_profile.json` | JSON backup of the profile |
| `QUILL_ONBOARDING_STATE` | `data/onboarding_state.json` | asked-once / delta bookkeeping |

### Phone notifications & Phone Link (Windows)

| Var | Default | Notes |
|---|---|---|
| `QUILL_NOTIFICATIONS` | `1` (Win) / `0` else | capture Windows toasts (iPhone mirror) |
| `QUILL_NOTIFICATION_POLL_S` | `2.5` | toast poll interval |
| `QUILL_NOTIFICATIONS_PHONE_LINK_ONLY` | `1` | only Phone Link toasts |
| `QUILL_NOTIFICATION_APPS` | — | widen the filter, e.g. `phone link,slack` |
| `QUILL_AUTOSTART_NOTIFICATIONS` | `1` | (via `run_all.py`) start on boot |
| `QUILL_PHONE_LINK` | `1` | allow driving Phone Link (send/read SMS) |
| `QUILL_PHONE_LINK_PS` | `powershell.exe` | PowerShell used for the automation scripts |
| `QUILL_PHONE_WATCH` | `1` | proactive reply/open offers for incoming notifications |

### Memory, storage, hygiene, consolidation, worker, reflection

| Var | Default | Notes |
|---|---|---|
| `QUILL_SEMANTIC` | `1` | `0` falls back to substring search |
| `QUILL_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | local sentence-transformers model |
| `QUILL_SAVE_AUDIO` | `1` | `0` skips saving WAV clips |
| `QUILL_INGEST_FILTER` | `1` | ASR hygiene; many `QUILL_INGEST_*` sub-thresholds |
| `QUILL_CONSOLIDATE` | `1` | merge utterances into turns |
| `QUILL_CONSOLIDATE_MAX_GAP_S` | `8` | silence gap that starts a new turn |
| `QUILL_WORKER` | `1` | durable background job runner |
| `QUILL_WORKER_POLL_S` / `_MAX_ATTEMPTS` | `2.0` / `3` | worker poll / retry cap |
| `QUILL_EXTRACT` | `1` | run fact/task extraction (calls the LLM) |
| `QUILL_REFLECT` | `1` | run daily reflection (calls the LLM) |
| `QUILL_REFLECT_MODEL` | `claude-opus-4-8` | reflection model |
| `QUILL_REFLECT_MAX_RECENT` / `_MAX_OPEN` | `120` / `40` | packet size bounds |

### Agents

| Var | Default | Notes |
|---|---|---|
| `QUILL_PLANNER` | `0` | `1` enables the Personal Agent Layer |
| `QUILL_AGENT_CHANNEL` | — | e.g. `chrome` — browser channel for the in-app agent |
| `QUILL_AGENT_PROFILE` | — | persistent browser profile (logged-in session reuse) |
| `AGENT_DRY_RUN` | `approval` | `plan` / `navigate` / `draft` / `approval` / `full` |
| `AGENT_EXECUTOR_VISION` | `1` | let the executor read screenshots |
| `AGENT_VISION_ALWAYS` | `0` | attach a screenshot every step (vs. only when stuck) |
| `AGENT_VISION_SPARSE_AT` | `6` | attach a shot when fewer than N elements visible |
| `AGENT_DATA_DIR` | `./sessions` | agent session storage |
| `AGENT_BROWSER_CHANNEL` | — | low-level default browser channel |

Browser-agent model tiers are constants in
[browser_agent/config.py](browser_agent/config.py): router/executor = Sonnet,
planner/escalation = Opus, verifier = Haiku.

### Desktop agent

| Var | Default | Notes |
|---|---|---|
| `QUILL_DESKTOP_JAIL` | `~/quill_desktop` | path jail for all file operations |
| `QUILL_DESKTOP_APPROVAL` | `1` | require approval for mutating actions |
| `QUILL_DESKTOP_TIMEOUT_S` | `60` | per-command timeout |
| `QUILL_DESKTOP_MAX_ACTIONS` | `25` | per-task action budget |
| `QUILL_DESKTOP_MAX_FILE_BYTES` | `200000` | max file read/write size |

---

## Where data lives

| Data | Location |
|---|---|
| Timeline + facts + relations + reflections + activities + jobs + agent runs | `data/quill.db` (SQLite) |
| Raw audio utterances | `data/audio/<epoch>.wav` (16-bit mono, one per utterance) |
| Captured webcam frames | `data/frames/<epoch>.jpg` |
| Desktop screen frames + click crops | `data/desktop_frames/*.jpg` |
| Voiceprints | `data/speakers/*.npy` (one per enrolled person) |
| Semantic index | `data/lance/` (LanceDB, 384-d embeddings) |
| Model-call log | `data/model_calls.jsonl` |
| Escalation distill trail | `data/escalate_distill.jsonl` |
| Onboarding profile | `data/onboarding_profile.json` |
| Browser-agent sessions & profiles | `./sessions/` |
| Model weights | `~/.cache/huggingface` |

The timeline reloads from `data/quill.db` on startup, so everything survives
restarts. Raw artifacts are served back through `/artifact`, which is
**path-confined to the data directory** so it can't read arbitrary files. The
entire `data/` and `sessions/` trees are git-ignored.

---

## Project layout

```
run_all.py               launch everything (capture in-process + agent as child)
run_audio.py             M1 standalone — live transcription
run_vision.py            M2 standalone — live webcam understanding
run_desktop.py           desktop-agent standalone driver
exec_webapp.py           browser agent standalone (with vinceo.ai memory bridge)

app/
  config.py              central settings (frozen dataclasses, env-driven)
  events.py              Event schema + EventBus (async pub/sub)
  main.py                FastAPI app + startup wiring (worker chain, watchers)
  storage.py             SQLite: events, facts, people, relations, reflections,
                         turns, sessions, activities, jobs, agent runs
  vectorstore.py         LanceDB semantic index
  api/routes.py          every HTTP endpoint + the Console HTML
  services/
    audio.py             mic → VAD → Whisper → Event
    ingest_filter.py     ASR hygiene (drops hallucinations / dupes)
    speakers.py          ECAPA speaker ID (clusters + named voiceprints)
    vision.py            webcam → frame selection → VLM → Event
    vlm.py / vlm_gemini.py   vision clients (local-first / Claude / Gemini)
    desktop_capture.py   screen + click capture (opt-in)
    notifications.py     Windows toast capture (Phone Link / iPhone mirror)
    phone_link.py        drive Phone Link (launch / read / send SMS)
    phone_watcher.py     proactive "reply to this?" offers
    memory.py            Memory Engine (subscribes to the bus)
    embeddings.py        local sentence-transformers embedder
    consolidation.py     utterances → turns
    sessions.py          turns → sessions
    activity.py          desktop events → activity blocks
    worker.py            durable background job runner (one queue, one worker)
    extractor.py         turns → tasks / commitments / claims
    resolution.py        person resolution (exact → prefix → embedding)
    reflector.py         daily facts → grounded, reviewable insights
    graph.py             knowledge graph (rebuild + context_for_person)
    onboarding.py        one-time profile seeding
    agent_planner.py     Personal Agent Layer (goal → Plan of ActionPackets)
    agent_log.py         Recorder + ActionPacket (runs / packets / verdicts)
    agent_bridge.py      lazily-started worker owning the browser agent
    multitask.py         mixed-message split-before-route fan-out
    readiness.py         unified action-readiness score (auto/offer/review/hold)
    anticipation.py      likely-next offers from activity patterns (opt-in)
    todo_watcher.py      proactive "run these to-dos?" offers
    task_offer.py        proactive "run this spoken task?" offers
    model_router.py      model selection + local-first text policy + suspect gates
    ollama_text.py       local Ollama text client
    escalate_log.py      local→parent distillation trail (+ human verdicts)
    few_shot.py          retrieval few-shot correction from verified verdicts
    grounding.py         structured chat grounding (graph/tasks/screen first) + sources
    self_quiz.py         idle self-quiz on approved facts → auto-labeled lessons
    model_log.py         per-call model usage log
    llm.py               chat entry (memory retriever; local-first when enabled)
    voice.py             TTS (stub)

browser_agent/           Exec.AI — route → plan → execute → verify web agent
  orchestrator.py        the run_goal() loop
  config.py              tiered models, dry-run levels, vision knobs
  tools.py               click / type / navigate / read / ask_human / request_approval
  perception.py          DOM + screenshot perception
  modes.py               7 task-specific policies
  failures.py            failure taxonomy + recovery ladder
  memory.py              intent@site procedural learning
  browser.py / llm.py / prompts.py / credentials.py / eval_tasks.py

desktop_agent/           guarded OS control (the allowlist IS the sandbox)
  guards.py              the security boundary — pure decision logic
  driver.py              DesktopDriver (launch apps, make dirs, allowlisted cmds)
  config.py              jail, allowlists, budgets

tests/                   unit tests (unittest; run with
                         `python -m unittest discover -s tests`)
scripts/                 download_models · enroll_speaker · run_extract ·
                         eval_agent · eval_vision_task · test_track_a ·
                         test_reflection · test_planner · bench_vision ·
                         diagnose_camera · …
  bench_text.py          learning-loop bench: replay labeled rows vs human gold
  distill_label.py       CLI verdicts on distill rows (list / show / label)
  distill_curate.py      training-set curation + readiness report
  self_quiz.py           run the idle self-quiz
  train_lora.py          Phase 3: curate → train (WSL2) → package → gate
  lora_train_wsl.py      the Linux half — Unsloth QLoRA + merged-GGUF export
  phone_link/            PowerShell UI-automation scripts
```

---

## Development & testing

```powershell
python -m unittest discover -s tests    # the unit suite
python scripts/test_track_a.py          # facts layer, assertion-based
python scripts/test_reflection.py       # daily reflection + grounding check
python scripts/test_planner.py          # context selection, risk table, compilers
python scripts/eval_agent.py            # browser-agent eval (routing + live tiers)
```

| Area | Coverage |
|---|---|
| **Unit suite** (`tests/`) | 420+ tests: escalate log, text/vision local routing, few-shot recall, grounding, self-quiz, bench + curation logic, the LoRA gate, chat verdicts, and friends — fast, mostly no network. |
| **Facts layer** | `scripts/test_track_a.py`, `scripts/facts_schema_check.py`, `scripts/run_extract.py --demo`. |
| **Reflection** | `scripts/test_reflection.py` — runs a daily reflection, checks grounding. |
| **Personal Agent Layer** | `scripts/test_planner.py`, `scripts/test_agent_log.py`. |
| **Browser agent** | `scripts/eval_agent.py` (routing tier tracks approval false-negatives, baseline 0), `scripts/test_approval_packet.py`, `scripts/eval_modes_dryrun.py`, `scripts/eval_vision_task.py` (the canvas-only access-code test). |
| **Vision** | `scripts/check_vision.py`, `scripts/bench_vision.py`, `scripts/diagnose_camera.py`. |
| **Desktop agent** | `tests/test_desktop_guards.py` (31) — the security boundary under direct test: path jail (traversal, absolute paths, Windows reserved device names), hard-block scan (destructive verbs, nested shells, metacharacters, secret paths), default-deny classification, launch-arg jailing, per-app open-target capability, and the autonomy auto-approve ladder (shell as its own axis). |

**Conventions.** Config is centralized and env-driven (`app/config.py` frozen
dataclasses); every service degrades gracefully rather than crashing on a
missing key, camera, or model; each LLM boundary hides behind one swappable
model constant so the router can retarget it; new capture modalities just
publish `Event`s to the bus — no downstream changes.

---

## Security model

vinceo.ai acts on your behalf, so its trust boundaries are explicit and layered:

1. **Memory is context, never command authority.** Perception is
   attacker-influenceable (anything said in the room, shown to the camera, or
   pushed as a notification), so retrieved memory can inform a draft but can
   never *approve* an irreversible action. Only a **live human reply**
   authorizes send / buy / delete / mutate.
2. **Risk classification is a table, not a guess.** The Planner's `RISK_TABLE`
   maps action verbs to `low / medium / high / blocked`; `blocked` never
   reaches an execution surface, anything ≥ medium forces the approval gate.
3. **Browser agent:** irreversible steps gate behind a source-grounded approval
   packet; it refuses to enter credentials or solve CAPTCHAs; dry-run levels
   cap every run; per-mode approval patterns only ever *add* to the global
   commit net.
4. **Desktop agent — the allowlist *is* the sandbox:** path jail, app
   allowlist, shell-verb allowlist, an unremovable hard-block list,
   `shell=False` with args-as-list, tiered approval, audit log, budgets.
5. **Phone Link** sends SMS only through the approval gate.
6. **Desktop capture is opt-in**, captures no keystrokes, and click crops never
   leave the machine unless screen VLM escalation is warranted.
7. **Provenance serving** (`/artifact`) is path-confined to the data directory.

> **Handle your keys carefully.** `.env` and `.credentials.env` are git-ignored
> but hold live API keys in plaintext — don't share them, don't paste them into
> screenshots, rotate anything exposed.

---

## Design ethos

- **Local-first.** VAD, ASR, speaker ID, embeddings, vision — and, opt-in,
  text — all run without a paid API call. Claude is used only where reasoning
  or high stakes genuinely need it, and every such call is logged as
  distillation data for shrinking the dependency over time.
- **Cost control at every model boundary.** Frame selection, motion gating,
  local-first routing with selective escalation, tiered agent models,
  reasoning-effort knobs, adaptive screenshot attachment.
- **Graceful degradation.** A missing API key, camera, local model, or
  non-Windows OS degrades one feature — it never crashes the pipeline.
- **Provenance + human review make trust earnable.** Every inference is
  traceable to raw perception, every action is approvable, and every human
  verdict is recorded as training signal.

---

## Known gaps & roadmap

- **Voice / TTS** ([app/services/voice.py](app/services/voice.py)) — still a
  stub; spoken Q&A isn't wired.
- **Personal Agent Layer is a v1 slice** — behind `QUILL_PLANNER=1`; task
  decomposition and several cognitive agents are stubbed (`# LLM:` markers).
- ~~**Unit-test `desktop_agent/guards.py`**~~ — **closed 2026-07-17**:
  `tests/test_desktop_guards.py` (31 tests) pins the jail, hard blocks,
  default-deny classification, and the autonomy ladder.
- ~~**Output-side feedback loop**~~ — **closed 2026-07-17**: verdicts flow
  back as few-shot corrections, calibration evidence, bench data, and LoRA
  training pairs (see [the learning loop](#the-learning-loop--from-verdicts-to-weights)).
  Remaining: data volume to the first training run (~100 curated pairs).
- **Enrich the extractor to populate `entities`** (orgs/projects) — lights up
  the graph's affiliation traversal.
- **Scheduling** — reflection is time-triggered on startup; self-quiz and
  LoRA training are manual/cron-able scripts; no real idle scheduler yet.
- **M6 connectors** — native email / calendar / CRM (the browser agent drives
  those UIs directly for now).
- **Postgres / object storage** — deferred, gated on a second device.

---

## License & status

**Status:** experimental research prototype under active development — not
production software; APIs and schemas may change without notice.

**License:** no license file is present, so all rights are reserved by default.
If you intend to share or open-source this, add a `LICENSE` file (e.g. MIT).
The Phone Link PowerShell scripts under
[scripts/phone_link/](scripts/phone_link/) are adapted from the MIT-licensed
`phonelink-mcp-server` project and retain that attribution.

**Deeper docs:** the interactive API reference lives at
<http://127.0.0.1:8000/docs> when the server is running; per-subsystem design
notes are inline as module docstrings; dated build logs are in
[july_07_2026_status.md](july_07_2026_status.md),
[july_07_2026_status_2.md](july_07_2026_status_2.md), and
[july_17_2026_status.md](july_17_2026_status.md) (the day the learning loop
closed); architecture deep-dives in
[phase3_lora_architecture.md](phase3_lora_architecture.md) and
[voice_pipeline_architecture.md](voice_pipeline_architecture.md); the previous
README is preserved as [readme_archive_2026-07-16.md](readme_archive_2026-07-16.md).
