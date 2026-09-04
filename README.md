# Sparrow

**A local-first personal memory system: it captures what happens around you, builds a living network of people and work, connects to teammates on your terms, and — with your approval — acts.**

> The product and in-app assistant are **Sparrow** (Ravenry). Env vars and
> the database keep the `QUILL_` prefix — see the
> [naming note](#configuration-reference).

![status](https://img.shields.io/badge/status-experimental%20prototype-orange)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows%20(primary)%20·%20macOS%2FLinux-lightgrey)
![local-first](https://img.shields.io/badge/inference-local--first-brightgreen)
![license](https://img.shields.io/badge/license-MIT-blue)

Sparrow runs on your laptop and turns everyday signal — speech, camera, optional
screen, phone notifications — into a **searchable, provenance-linked memory**.
From that stream it grows a **people and org network** (who you work with, what
you owe them, what’s still open), and can **connect** to another Sparrow instance
or a paired phone so assistants can ask each other questions without shipping
raw memory off your machine. When you want something done, it can drive a
browser, desktop app, or Phone Link — always pausing at a human approval gate
before anything irreversible.

```
  Capture  →  Memory  →  People / Org network  →  Peer & phone connections
                              │
                              └─→ grounded chat · review · approve · act
```

> **Status (August 2026):** experimental research prototype under active
> development. Capture, memory, facts, the knowledge graph, peer/phone
> channels, local-first model routing, the meeting layer, and the
> browser/desktop/phone agents all work today, behind a hardened trust layer
> (hash-bound approvals, source policies, evidence-verified outcomes).
> ~2,400 tests pass. Recent work: the pilot erasure path (stop-everything,
> "delete everything" with a receipt, and an egress view that reads the
> machine's own logs), a resumable model pre-download, a swappable ASR engine
> seam with per-engine ingest calibration, pixel-level browser control for
> canvas pages, a Linux body for the desktop agent, stage-level latency
> measurement, and a trained classifier rung below the local LLM
> (shadow-only). Some pieces remain
> feature-flagged — see [Known gaps & roadmap](#known-gaps--roadmap). Not
> production software.

---

## Table of contents

- [What Sparrow does](#what-sparrow-does)
- [How it works — the short version](#how-it-works--the-short-version)
- [Memory](#memory)
- [People & org network](#people--org-network)
- [Connections — peers and phone](#connections--peers-and-phone)
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
- [Org AI Network (experimental)](#org-ai-network-experimental)
- [License & status](#license--status)

---

## What Sparrow does

Four capabilities sit on top of the same local store:

| Capability | What you get |
|---|---|
| **Memory** | A durable timeline of what was said and seen, searchable by meaning, every fact linked back to the clip or frame it came from. |
| **Network** | People, orgs, commitments, and open loops as a traversable graph — *who is Justin, what’s open with him, who else comes up with him?* |
| **Connection** | Pair with a teammate’s Sparrow (or a phone) so their assistant can ask yours a question. Answers are composed from *your* memory, behind *your* disclosure policy. Raw memory never leaves the machine. |
| **Action** | Browser, desktop, and phone agents that ground drafts in memory and stop for **Approve / Edit / Cancel** before send, buy, or mutate. |

Everything stays on your machine by default. Local models handle VAD, ASR,
speaker ID, embeddings, and (opt-in) vision/text; Claude is called only when
stakes or confidence warrant it, with spend caps and privacy redaction.

---

## How it works — the short version

Sparrow is a stack of five layers. Each layer only depends on the one below it,
and everything between them travels as a single `Event` on an in-process bus:

```
  ┌───────────────────────────────────────────────────────────────┐
  │  ACT       Browser agent · Desktop agent · Phone Link         │  ← touches the world, approval-gated
  ├───────────────────────────────────────────────────────────────┤
  │  DECIDE    Personal Agent Layer — goal → risk-classified plan │  ← plans, grounds, classifies risk
  ├───────────────────────────────────────────────────────────────┤
  │  UNDERSTAND Facts · People · Graph · Reflection · Activities  │  ← what the stream *means*
  ├───────────────────────────────────────────────────────────────┤
  │  REMEMBER  Memory Engine — SQLite timeline + LanceDB semantic │  ← durable, searchable, provenance-linked
  ├───────────────────────────────────────────────────────────────┤
  │  PERCEIVE  Audio · Vision · Desktop capture · Phone notifs    │  ← raw perception → Event
  └───────────────────────────────────────────────────────────────┘
```

Three principles run through every layer:

- **Local-first.** Voice-activity detection, speech-to-text, speaker ID, and
  embeddings run on the CPU with no GPU and no PyTorch. Vision — and, opt-in,
  text — go to a **local Ollama model first**; a paid Claude call happens only
  when a task is high-stakes or the local model is unsure. Every escalation is
  logged as a distillation row, so paid calls also become training data.
- **Provenance everywhere.** Every memory links back to the raw audio clip or
  frame it came from; every extracted fact carries a verbatim `source_span`
  quote and a pointer to its source event. The Console lets you play the exact
  sound a fact came from.
- **Memory is context, never command authority.** Perception is
  attacker-influenceable (anything said in the room, shown to a camera, or
  pushed as a notification). Retrieved memory — and answers from peers — can
  *inform* an action but can never *approve* one. Only a live human reply
  authorizes anything irreversible.

---

## Memory

**Files:** [app/services/memory.py](app/services/memory.py) ·
[app/storage.py](app/storage.py) · [app/vectorstore.py](app/vectorstore.py)

The Memory Engine subscribes to every `Event` on the bus and does three things:

1. **Writes a durable timeline** to SQLite (`data/quill.db`) — events, facts,
   people, relations, reflections, turns, sessions, activities, jobs, and agent
   runs in one joinable file that reloads on startup.
2. **Embeds for semantic search** into LanceDB (`all-MiniLM-L6-v2`, local CPU)
   so *"what did Marc say about pricing?"* finds the right episode even with
   little keyword overlap. Facts are indexed alongside episodes.
3. **Keeps the raw evidence** — WAV clips and JPEGs on disk, referenced from
   the event, served only through path-confined `/artifact`.

Upstream, consolidation merges utterances into **turns** and **sessions**;
extraction pulls out **tasks, commitments, and claims**; reflection asks what
changed and what’s still open. Meta-memory audits surface at-risk commitments,
stale facts, fading threads, and forget candidates for human review.

Browse and correct everything in the [Memory Console](#the-memory-console--the-trust-layer)
at `/memory`. Your living self-view is at `/profile`.

---

## People & org network

**Files:** [app/services/people_pipeline.py](app/services/people_pipeline.py) ·
[app/services/graph.py](app/services/graph.py) ·
[app/services/resolution.py](app/services/resolution.py)

Sparrow doesn’t just store transcripts — it grows a **network of people and
organizations** tied to evidence:

- **People Intelligence (v2)** resolves names and aliases into person records
  with deterministic scoring (exact → prefix → embedding). Mentions start as
  candidates; contacts are evidence-linked. Source policies decide what a
  terminal, news page, social feed, or email may mint. Kill-switch:
  `QUILL_PEOPLE_V2=0`.
- **Knowledge graph** rebuilds typed edges without LLM calls: person ↔ fact
  (`responsible_for` / `committed` / `owed`), mentions, provenance, and
  co-occurrence. Asserted edges (e.g. `works_at` from onboarding or email
  network ingest) survive rebuilds. `context_for_person(name)` answers
  relational questions flat search can’t.
- **Org briefs** at `/org/{entity_id}` show people, facts, and open work for an
  organization. Profile at `/profile` shows what the system currently believes
  about *you*, with Confirm / Edit / Forget on every card.
- **Onboarding** (`/onboarding`) seeds identity, people, work, and rhythm so
  the graph isn’t empty on day one.

API surface: `GET /graph/context` · `POST /graph/rebuild` · `GET /people/*` ·
`GET /org/{id}/data` · `GET /profile/data`.

---

## Connections — peers and phone

Sparrow is personal by default, but it can **connect** without becoming a shared
cloud memory.

### Team peer channel (`/peer`)

**File:** [app/services/peer_channel.py](app/services/peer_channel.py)

Two people each run their own Sparrow. They pair with a short-lived code; after
that, one assistant can ask the other a question. The answer is composed from
the **answerer’s** memory by **their** models, then redacted and returned.
Raw timeline rows never cross the wire.

- Pairing is mutual, single-use, and expires; tokens authenticate peers.
- Disclosure is per-peer and per-class (`availability` / `work` / `contact` /
  `personal` / `other` → `auto` | `offer` | `deny`). Default is **offer** —
  you approve each disclosure. `personal` can never be set to auto.
- Inbound asks land as observed-tier context; they never authorize actions.
- Peer ↔ Person links are user-asserted only (`POST /peer/link`).

> Peer channel over LAN needs TLS before pairing beyond localhost — see
> [roadmap](#known-gaps--roadmap).

### Phone channel

**Files:** [app/services/phone_channel.py](app/services/phone_channel.py) ·
[app/services/notifications.py](app/services/notifications.py) ·
[app/services/phone_link.py](app/services/phone_link.py)

- **QR-paired phone channel** — a direct device link (no Phone Link required)
  for ingesting phone-side events with the same trust primitives as peers.
- **Windows Phone Link** — optional capture of mirrored iPhone toasts via
  `UserNotificationListener`, and outbound SMS through the Phone Link UI after
  approval.

---

## One moment, end to end

You say, near the laptop:

> *"I still owe Justin the pricing follow-up — Marc said forty-nine a month."*

1. **Capture** ([audio.py](app/services/audio.py)). Mic → Silero VAD →
   faster-whisper → speaker ID → one `Event` with transcript, speaker, and a
   link to the saved WAV.
2. **Memory** ([memory.py](app/services/memory.py)). Written to SQLite and
   embedded into LanceDB.
3. **Consolidation** ([consolidation.py](app/services/consolidation.py)).
   Adjacent utterances become a **turn**; turns group into **sessions**.
4. **Extraction** ([extractor.py](app/services/extractor.py)). A
   **commitment** (*owe Justin a follow-up*) and a **claim** (*$49/mo*), each
   with a verbatim quote and `source_event_id`. People pipeline resolves who
   "Justin" is.
5. **Network.** Deterministic graph rebuild wires Justin ↔ commitment ↔
   evidence; daily reflection can surface it as an `open_loop`.
6. **Review** in the Console — approve / edit / dismiss with inline clip
   playback.
7. **Act** (optional) — *"email Justin the pricing follow-up."* The agent
   pulls "$49/mo" from memory, drafts the email, and stops at a
   source-grounded approval packet. Nothing sends until you say so.

Every hop is inspectable after the fact in `data/quill.db`.

---

## The Event: one schema for everything

Every input modality normalizes into a single **`Event`**
([app/events.py](app/events.py)) on an in-process **`EventBus`**:

```
        Webcam ─▶ Vision pipeline ──┐
    Laptop mic ─▶ Audio pipeline ───┤
 Screen+clicks ─▶ Desktop capture ──┤
         Phone ─▶ Notif. pipeline ──┤
                                    ├─▶ EventBus ─▶ Memory Engine ─▶ SQLite + LanceDB
   watchers (todo/phone/task) ◀─────┘                   │
        │                                               │ semantic search
        ▼          consolidate ─▶ extract ─▶ people/graph ─▶ reflect
  proactive offers                                      │
        ▼                                               ▼
   /chat ──▶ Planner ──▶ Browser / Desktop / Phone agent (memory-grounded, approval-gated)
```

Schema fields: `time, modality, raw, summary, source, confidence, people,
tasks, entities, meta`. Modalities: `audio`, `vision`, `notification`,
`input`, `system`. Adding a capture source means publishing `Event`s; no
downstream rewrite.

---

## Layer 1 — Perceive

### Audio (M1)

**Files:** [app/services/audio.py](app/services/audio.py) ·
[app/services/asr.py](app/services/asr.py) ·
[app/services/asr_calibration.py](app/services/asr_calibration.py) ·
[app/services/speakers.py](app/services/speakers.py) ·
[app/services/ingest_filter.py](app/services/ingest_filter.py)

Microphone → **Silero VAD** (ONNX) → **ASR engine** (faster-whisper by default,
CTranslate2) → `Event`.

- Capture thread stays cheap (VAD + buffer); transcription runs on a worker so
  frames aren’t dropped.
- **Engine seam.** `asr.py` is a protocol + registry: `QUILL_ASR_ENGINE` picks
  the engine and `audio.py` knows nothing about its internals, so adding one
  is a module plus a `register()` line and rolling back is a restart. Mic and
  loopback share **one model per process** (two would double RAM and make the
  pipelines fight for cores).
- **Confidence is engine-specific.** `ASRResult` names its scale via
  `confidence_kind`; ingest thresholds were calibrated against Whisper's
  `avg_logprob` and must not be reused blindly. Per-engine thresholds live in
  `data/asr_calibration.json`, fitted offline by
  `scripts/calibrate_asr_confidence.py` and hot-reloaded when the file changes.
  No entry = shipped defaults; a calibration file can move thresholds but can
  never disable the filter.
- **WER harness.** `scripts/eval_asr.py` + fixtures under
  `tests/fixtures/asr_eval/` (`make eval-asr` / `eval-asr-smoke` /
  `eval-asr-baseline`) so an engine swap is measured, not asserted.
- Ingest filter drops Whisper silence-hallucinations via `avg_logprob` /
  `no_speech_prob` (`QUILL_INGEST_FILTER=0` to disable).
- Optional word-level timings in `meta` (`QUILL_ASR_WORD_TIMESTAMPS=1`) — costly
  on Whisper, free on engines that emit them natively.
- **Speaker ID** via SpeechBrain ECAPA — anonymous clusters by default; enroll
  with `python scripts/enroll_speaker.py Marc`.
- Provenance chain per utterance: clip → transcript → corrections
  (`GET /console/provenance/{event_id}`).

### Vision (M2)

**Files:** [app/services/vision.py](app/services/vision.py) ·
[app/services/vlm.py](app/services/vlm.py)

Webcam → OpenCV frame selection → VLM structured extraction → `Event`.

- Motion-gated + rate-limited; dark frames skipped.
- Structured JSON: description, OCR, people, objects, page type
  (`todo_list` / `notes` / …) with items — `todo_list` feeds the proactive
  see → offer → act loop.
- Local-first Ollama (`minicpm-v`) with Claude fallback.
- Windows: DirectShow + MJPG by default.

### Desktop capture (screen + clicks, opt-in)

**File:** [app/services/desktop_capture.py](app/services/desktop_capture.py)

Off by default (`QUILL_DESKTOP_CAPTURE=1` or `--desktop-capture`). No
keystrokes. Screen frames and click crops become events that fold into
**activity blocks** (“what was I doing?”).

### Phone notifications (Windows)

**Files:** [app/services/notifications.py](app/services/notifications.py) ·
[app/services/phone_link.py](app/services/phone_link.py) ·
[app/services/phone_watcher.py](app/services/phone_watcher.py)

Mirrored iPhone toasts via Phone Link → `UserNotificationListener` →
`Event`. Outbound SMS is the Phone Link agent (approval-gated).

---

## Layer 2 — Remember

See [Memory](#memory) above for the product view. Implementation detail:

- SQLite timeline + LanceDB embeddings + artifact links.
- Un-indexed events backfill on startup; substring search if semantic is off
  (`QUILL_SEMANTIC=0`).

---

## Layer 3 — Understand

### Consolidation & the durable job queue

**Files:** [consolidation.py](app/services/consolidation.py) ·
[sessions.py](app/services/sessions.py) · [worker.py](app/services/worker.py)

Utterances → turns (gap `QUILL_CONSOLIDATE_MAX_GAP_S`) → sessions. Heavy work
(`consolidate` → `extract` → `graph`, plus `reflect_daily`) runs on one
`jobs` table and one background worker — crash-safe, coalesced, no Redis.
Retries are bounded with exponential backoff and a **dead-letter park** after
`QUILL_WORKER_MAX_ATTEMPTS`, visible in `GET /console/jobs`; SQLite runs WAL
with a `busy_timeout` so transient lock contention retries instead of failing
a job.

### Facts — tasks, commitments, claims

**Files:** [extractor.py](app/services/extractor.py) ·
[resolution.py](app/services/resolution.py) ·
[people_pipeline.py](app/services/people_pipeline.py)

Windowed pass over settled turns → structured facts with `source_span` +
`source_event_id`. Tasks lifecycle: `open → done → cancelled`. Console review
(approve / edit / dismiss / done) is the training signal.

### Desktop activities

**File:** [activity.py](app/services/activity.py)

App-focus stretches become activity blocks (app, windows, screen/click counts,
summary), grounding chat and the anticipation watcher.

### Reflection

**File:** [reflector.py](app/services/reflector.py)

Period insights (`change · pattern · risk · open_loop · …`), grounded only on
fact ids it was handed, each reviewable (approve / edit / dismiss /
convert-to-task). Daily auto-enqueue when stale; `POST /reflect/run` on demand.

### Knowledge graph & onboarding

See [People & org network](#people--org-network). Onboarding seeds the graph
from a guided profile at `/onboarding`.

---

## Layer 4 — Decide

**Files:** [agent_planner.py](app/services/agent_planner.py) ·
[agent_log.py](app/services/agent_log.py) ·
[readiness.py](app/services/readiness.py) ·
[multitask.py](app/services/multitask.py)

On by default (`QUILL_PLANNER=1`). Compiles a goal into a risk-classified
**Plan** of `ActionPacket`s from facts / graph / reflections / commitments:

```
select_context → decompose → compile steps → classify_risk
     → surfaces (browser / desktop / phone) → human approval → Recorder
```

- **Risk is a table**, not an LLM guess (`low / medium / high / blocked`).
- **Readiness bands** `auto / offer / review / hold` gate proactive offers
  (`GET /console/readiness`).
- Mixed messages fan out per surface in dependency order.
- Every run, packet, and human verdict (including edits) is logged at
  `GET /console/agent-runs`.

---

## Layer 5 — Act

### Browser agent — "Exec.AI"

**Directory:** [browser_agent/](browser_agent/) · **standalone:**
[exec_webapp.py](exec_webapp.py) · **bridge:**
[app/services/agent_bridge.py](app/services/agent_bridge.py)

Route → plan → execute → verify over Playwright. Source-grounded approval
packets (Action / To / Subject / Body / **Why / Source**). Mode policies, dry-run
levels (`plan / navigate / draft / approval / full`), failure taxonomy,
procedural memory per `intent@site`, and semantic search over Sparrow’s own
timeline for grounding.

**Perception beyond the DOM.** A pointer-cursor sweep plus CDP listener ground
truth (`AGENT_CDP_LISTENERS=1`) expose click-wired `<div>`s as real
`element_id`s, and each step's prompt carries a computed changes-delta.

**Pixel surfaces** (`AGENT_BROWSER_PIXEL=1`, on by default). The action
vocabulary is `element_id`-based, so anything painted into a `<canvas>` was
readable but not clickable. The scan now reports opaque surfaces (canvas,
video, embed, object, `role=application`) clipped to the viewport, including
inside same-origin frames, and promotes the largest one covering ≥25% of the
view with no interactive descendants. On those turns only, three extra actions
appear alongside the screenshot they are measured on: `click_at`, `drag`
(stepped moves), and `press_key`. Guardrails are the point:

- a **cross-origin frame stays unknown** and can never become a pixel target —
  an embedded checkout must not be blind-clickable;
- coordinates outside the surface are **refused with the bounds quoted back**,
  so a misread screenshot cannot reach a Send/Buy button;
- grounding is computed, not assumed — the driver reads the PNG's own IHDR,
  fits the shot under the API's downsize cap, and maps every coordinate through
  the recorded scale;
- verification compares **rendered frames** (a canvas move changes no DOM), and
  the repeat-action guard keys on that frame hash too.

**Learning from runs.** `ask_human` answers and post-mortems
(`AGENT_POSTMORTEM`) persist in procedural memory and reach the executor; a
once-per-run done check (`AGENT_DONE_CHECK`) rejects off-goal completions.
Every (observation → action, verified) pair lands in
`sessions/agent_distill.jsonl` ([browser_agent/distill.py](browser_agent/distill.py))
and, for successful runs only, harvests into `learning_pairs` as task type
`agent.act` ([agent_harvest.py](app/services/agent_harvest.py)) — explicitly
excluded from text-LoRA curation so agent trajectories cannot pollute the text
champion.

### Desktop agent

**Directory:** [desktop_agent/](desktop_agent/)

OS-level control where the allowlist *is* the sandbox: path jail, app
allowlist, shell-verb allowlist, hard-block list, `shell=False`, tiered
approval, budgets. Approval always from the live human, never from memory.

- **Cross-platform body.** `a11y.py` is the platform-neutral seam — UI
  Automation on Windows, AT-SPI via `a11y_linux.py` elsewhere — so the driver's
  scan / invoke / set_text vocabulary is identical on either OS. `ghost.py`
  does the same for off-screen window parking (`ghost_x11.py` + `x11_util.py`);
  X11 only, and it *says so* on Wayland rather than half-working.
  `python-xlib` is a Linux-only requirement.
- **App promotion.** A runtime-discovered app stays launch-only until you
  approve it once with `remember_app`, at which point `app_promotion.py`
  promotes it against a capability template (`app_templates.py`). Desktop
  Access rows show `remembered` / `template` / a plain-language capability
  summary instead of a bare "discovered at runtime".

### Phone Link agent

**File:** [app/services/phone_link.py](app/services/phone_link.py)

Outbound SMS via Windows Phone Link UI automation after approval
(`POST /phone`). Windows-only; `QUILL_PHONE_LINK=0` to disable.

---

## Local-first models & the escalation ladder

**Files:** [vlm.py](app/services/vlm.py) ·
[ollama_text.py](app/services/ollama_text.py) ·
[model_router.py](app/services/model_router.py) ·
[escalate_log.py](app/services/escalate_log.py) ·
[fast_heads.py](app/services/fast_heads.py) ·
[latency.py](app/services/latency.py)

The ladder now has four rungs — a trained classifier head, the hand-written
`utterance_router`, the local LLM, and Claude:

```
  head (<1 ms, shadow-only today) ─▶ local Ollama ─▶ Claude
        "does this need a model?"      "can it answer?"    stakes / unsure
```

- **Vision** (`QUILL_VISION_LOCAL=1`): Ollama first; Claude for high-stakes /
  low confidence / weak capture / unreachable local.
- **Text** (`QUILL_TEXT_LOCAL=1`, default off): local Ollama for chat /
  extract / reflect; escalate on error, parse failure, low confidence, or
  high-stakes tasks (default: `plan`). A prompt over
  `QUILL_TEXT_LOCAL_MAX_PROMPT_CHARS` (12k) skips the local tier outright — a
  7B model handed a whole attached document answers fluently from the part it
  could hold and self-reports high confidence, so the confidence gate never
  fires and length is the only signal available *before* the call.
- Fail open to Claude if local is down; never double-bill after a usable local
  answer.
- **Speculative work is local-only, enforced twice.** A `speculative=True` call
  never escalates and returns `""` rather than spending; `_complete_claude`
  itself raises `SpeculativeCloudCall` inside a speculative scope, so a future
  code path that forgets the policy fails loudly instead of quietly spending.
  The invariant ("no speculative row names a cloud provider") is asserted over
  the call trail.
- Distill trail: `data/escalate_distill.jsonl` · summary `GET /console/escalate`.
- Telemetry: `data/model_calls.jsonl` · `GET /console/models`.

### Classifier heads — the bottom rung (shadow-only)

`fast_heads.py` answers one binary question per task — *does this input need a
model at all?* — from a MiniLM embedding plus a few cheap scalars. Same idea as
the hand-written `utterance_router`, learned rather than authored, under the
escalation router's three-mode contract: `off` (default) · `shadow` (predicts
and logs beside what the LLM actually did; the LLM always still runs) ·
`active` (only a confident "nothing here" skips).

- **Deliberately asymmetric.** A head that wrongly runs the LLM costs a few
  hundred milliseconds of local compute; one that wrongly skips silently drops
  a real commitment out of memory and nothing notices. So the uncertain middle
  band always runs the model, and the activation gate is measured over the
  would-have-skipped population alone.
- Heads only ever *remove* local calls and never choose a rung, so they cannot
  raise cloud spend.
- Wired from the measured hot spots: **vision** (`frame_keep`) and **extract**.
  `query_route` / `mention_detect` are declared and unwired.
- A head's break-even is a skip *rate*, not a certainty — the shared MiniLM
  encode it depends on costs ~26 ms whether or not the head skips, which the
  module documents as arithmetic so nobody adds a head over a cheap call.
- `head_observations` stores numbers and enum-ish strings only, never input
  text. `GET /console/heads` reports per-head volume, skip rate, disagreement,
  and why each head is not yet activatable. Nothing is activated today: the
  gate needs 200 labels and a shadow week.

### Latency — measure before optimizing

`latency.py` records *where* the time went (queue wait, cold model load,
prefill, generation, retrieval, post-processing), not just how long a call
took. Two rules keep it honest: it never breaks the path it measures (every
public function swallows its own exceptions), and it never adds a probe to the
hot path — Ollama already returns `load_duration` / `prompt_eval_duration` /
`eval_duration` on every reply, and the audio path is *bridged* from the
`asr_latency_ms` / `queue_wait_ms` it already writes rather than
re-instrumented.

- `QUILL_LATENCY_SPANS=1` (off by default) → `data/latency_spans.jsonl` ·
  `GET /console/latency` aggregates p50/p90/p99 per stage per task, stages
  ranked by share of total time.
- **Residency:** `keep_alive` is sent on every completion
  (`QUILL_OLLAMA_KEEP_ALIVE`, default `30m`) so the model stops being evicted
  after ~5 min idle; an optional startup warm-up (`QUILL_OLLAMA_WARMUP=1`) means
  the first interaction never pays the ~3.4 s cold load either.
- **Prefix ordering:** the exemplar block is handed to `ollama_text` as its own
  argument instead of being concatenated, so the system prompt emits
  static-first and the prefix cache works (measured: prefill 77 ms → 28 ms on a
  repeated task). Same bytes reach the model.
- Benches: `make bench-latency` · `make latency-baseline` · `make listen-idle` ·
  [scripts/bench_bakeoff.py](scripts/bench_bakeoff.py) ·
  [scripts/bench_listen_idle.py](scripts/bench_listen_idle.py).

---

## The learning loop — verdicts → exemplars → router → LoRA

**Files:** [learning_store.py](app/services/learning_store.py) ·
[exemplar_store.py](app/services/exemplar_store.py) ·
[shadow_eval.py](app/services/shadow_eval.py) ·
[escalation_router.py](app/services/escalation_router.py) ·
[fast_heads.py](app/services/fast_heads.py) ·
[agent_harvest.py](app/services/agent_harvest.py) ·
[few_shot.py](app/services/few_shot.py) ·
[scripts/eval_exemplars.py](scripts/eval_exemplars.py) ·
[scripts/train_lora.py](scripts/train_lora.py) ·
[scripts/demo_learning_loop.py](scripts/demo_learning_loop.py)

Every verdict you give anywhere — approving or editing an extracted task,
👍/👎/✏️ on a chat answer, confirming an audit item, merging two people,
adjudicating a claim — becomes one canonical **learning pair**
(`learning_pairs` in SQLite, visible and deletable in the Console's
**Learning** tab). Four mechanisms consume them, in order of how fast they
pay off:

```
any human verdict ──► learning pair (redacted, provenance-linked)
  ├─ 1. EXEMPLARS (same day)   confirmed pairs are embedded; the next similar
  │      QUILL_EXEMPLARS=1     input retrieves your correction into the local
  │                            prompt — no GPU, rollback = delete a row.
  │                            A/B-measured per type; losing types auto-gate off.
  ├─ 2. SHADOW EVAL (nightly)  while idle, Claude re-grades a small batch of
  │      QUILL_SHADOW_EVAL=1   kept local answers (cost-capped, never
  │                            personal-classed content); disagreements become
  │                            pairs you confirm with one click.
  ├─ 3. ROUTER (weekly-ish)    a small calibrated classifier learns "will the
  │      QUILL_ROUTER=shadow   local model fail on this?" — first in shadow
  │                            (logged next to the heuristic), activated only
  │                            by your explicit flag flip.
  └─ 4. LoRA (graduation)      when a task type saturates the exemplar store
         QUILL_IDLE_TRAIN=1    (~300 confirmed pairs + plateaued A/B gains),
                               the idle QLoRA pipeline fires — and promotes
                               only if it beats the exemplar-augmented
                               incumbent on the holdout bench.
```

Two feeds sit beside the human verdicts. **Agent trajectories** — verified
steps from *successful* browser runs — harvest out of
`sessions/agent_distill.jsonl` into `learning_pairs` as task type `agent.act`,
marked machine-trusted (`shadow.agent_verified`) so the exemplar store refuses
them unless `QUILL_SHADOW_AUTOTRUST=1`, and excluded from text-LoRA curation
outright. And the same label pool trains the **classifier heads** described
above — a fifth mechanism, still `off` by default, that decides whether the
local model runs at all rather than which rung answers.

**What it is not:** nothing here changes what Sparrow is *allowed* to do —
learning affects the quality of proposals, never the authority to act (risk
stays a lookup table, enforced by tests). Nothing personal-classed leaves
the machine through shadow eval. Promotion is always an offer with a
rollback line, never a silent swap — for exemplars (delete the row), the
router (flip back to `shadow`), and LoRA tags alike. New installs never
ship user-specific weights — personalization accrues from natural verdicts
(`tests/test_no_user_tailoring.py`).

Answers show **Sources:** (person graph / open tasks / screen & camera /
timeline / activity). See the whole loop run end-to-end on a fixture DB:
`python scripts/demo_learning_loop.py`.

---

## The Memory Console — the trust layer

**Where:** <http://127.0.0.1:8000/memory> (`/console` redirects here)

| Tab | What you see |
|---|---|
| **All / Audio / Vision** | Event timeline with clips/frames and semantic search |
| **Desktop** | Screen analyses and clicks |
| **Activity** | “What was I doing?” blocks |
| **Turns / Sessions** | Consolidated conversation + playback |
| **Tasks** | Fact review: approve / done / edit / dismiss + source quote |
| **Reflection** | Daily insights with convert-to-task |
| **Audio Health / Low-confidence** | Capture quality and unsure items |
| **Learning** | Every harvested verdict pair (deletable), exemplars ("what Sparrow has learned", with a pause switch), shadow-eval agreement rates, one-click confirm for shadow-found corrections |

Related surfaces: `/today` · `/chat` · `/ui` · `/profile` · `/org/{id}` ·
`/meetings` · `/triggers` · `/peer` · `/desktop-access` · `/onboarding` ·
`/docs`.

Operational read-outs: `GET /console/latency` (stage p50/p90/p99) ·
`GET /console/heads` (per-head skip rate, disagreement, why not activatable) ·
`GET /console/jobs` (queue + dead-letter park) ·
`GET /console/desktop-access` (allowlist, remembered apps, capabilities).

---

## Proactive behavior

Gated by `QUILL_AGENT` (`0` = pure memory retriever, no offers). Offers are
yes/no in chat — nothing runs without your reply.

- **To-do watcher** — vision `todo_list` → offer to run items in the browser.
- **Task offer** — spoken tasks as “run this?”, readiness-gated.
- **Phone watcher** — incoming notification → offer to reply/open.
- **Anticipation** (`QUILL_ANTICIPATE=1`) — likely-next from activity
  patterns after an idle gap.

---

## Quick start

### 1. Prerequisites

- **Python 3.10+**
- **Windows 11** primary (audio, vision, desktop capture, Phone Link).
  macOS/Linux run capture + memory + agents; Windows-only pieces degrade
  cleanly.
- Microphone / webcam optional for live capture. On Linux you need
  PortAudio (`sudo apt install libportaudio2`) for the mic and a V4L2
  camera (`/dev/video*`) for the webcam pipeline. Desktop control on Linux
  needs AT-SPI and an **X11** session (`python-xlib`, installed from
  `requirements.txt`); the ghost-window path declines on Wayland rather than
  half-working.
- `GET /capture/status` reports what each capture source can actually do on
  this platform, so the Privacy sheet disables what won't work and says why
  instead of failing at the click.
- **Anthropic API key** for Claude tiers (vision fallback, extraction,
  reflection, browser agent). Local pieces run without it.

### 2. Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/download_models.py            # ~460 MB speech models, one-time
playwright install chromium                  # browser agent
```

Testers can use **`install.bat`** / **`start.bat`** — see
[TESTER_SETUP.md](TESTER_SETUP.md) — macOS testers use [TESTER_SETUP-macos.md](TESTER_SETUP-macos.md).

### 3. Add your API key

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
# optional
GOOGLE_API_KEY=...
```

Put this in `.env` or `.credentials.env` at the project root — never commit it.

### 4. (Optional) local-first models

```powershell
ollama pull minicpm-v    # vision (default on)
ollama pull llama3.2     # text  (opt-in: QUILL_TEXT_LOCAL=1)
```

### 5. Run everything

```powershell
python run_all.py
```

Then open:

- **Memory Console** — <http://127.0.0.1:8000/memory>
- **Live chat** — <http://127.0.0.1:8000/ui>
- **You (profile)** — <http://127.0.0.1:8000/profile>
- **Team (peer)** — <http://127.0.0.1:8000/peer>
- **Onboarding** — <http://127.0.0.1:8000/onboarding>
- **API docs** — <http://127.0.0.1:8000/docs>
- **Browser agent** — <http://127.0.0.1:5000>

Flags: `--no-audio` · `--no-vision` · `--no-notifications` · `--no-browser` ·
`--desktop-capture` · `--system-audio` · `--browser-headless` ·
`--no-org-coordinator` · `--port` · `--browser-port` · `--host` ·
`--log-level` (uvicorn access logs are off by default).

---

## Usage examples

### Run one piece at a time

```powershell
python run_audio.py                 # live transcription
python run_vision.py                # live webcam understanding
uvicorn app.main:app --reload       # API server alone
python exec_webapp.py               # browser agent + memory bridge
```

### Teach it who's talking

```powershell
python scripts/enroll_speaker.py Marc
python scripts/enroll_speaker.py Justin 15
python scripts/enroll_speaker.py Marc clip.wav
```

### Memory-grounded action from the API

```powershell
$r = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/chat `
     -ContentType application/json `
     -Body '{"message": "email Justin the pricing follow-up"}'
$since = $r.since
Invoke-RestMethod -Uri "http://127.0.0.1:8000/chat/poll?since=$since"
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/chat/answer `
     -ContentType application/json -Body '{"text": "approve"}'
```

### Cap how far a run may go

```jsonc
{ "message": "/draft reply to Marc about the Series A timeline" }
// levels: /plan · /navigate · /draft · /approval (default) · /full
```

### Search your memory

```bash
curl -s "http://127.0.0.1:8000/memory/search?q=whiteboard%20series%20A"
```

### Ask a teammate's Sparrow (after pairing on `/peer`)

```bash
# Chat shorthand once a Person is linked to a peer:
# "ask Name: when is Justin free Thursday?"
```

---

## Interfaces & API

| Surface | What it is |
|---|---|
| **`/memory`** | Memory Console (timeline, tasks, reflection, search) |
| **`/profile`** | Living user profile — confirm / edit / forget |
| **`/org/{id}`** | Org living brief — people, facts, open work |
| **`/peer`** | Team pairing + per-peer disclosure policy |
| **`/people/*`** | People roster, unresolved mentions, merge/rename |
| **`/ui` · `/chat`** | Live chat, offers, approvals |
| **`/meetings`** | Meeting sessions, briefs, session chat |
| **`/triggers`** | Standing data-row triggers (offer-only) |
| **`/onboarding`** | Guided profile seeding |
| **`/graph`** | Graph context / rebuild / stats |
| **`/facts` · `/reflections`** | Programmatic review APIs |
| **Browser agent** | Exec.AI UI at <http://127.0.0.1:5000> |

Every surface is its own module under [app/api/](app/api/) (`chat_page`,
`console_page`, `auth_page`, `desktop_access_page`, `home_page`, …) — page HTML
no longer lives inline in `routes.py`, which keeps the routing.
[mnemos_theme.py](app/api/mnemos_theme.py) is the **single source of brand
tokens** (palette, type, motion, the mark), applied via `apply()` /
`apply_plain()` so a page declares `@@BRAND@@` / `@@ROOT@@` / `@@FONTS@@`
instead of restating hex codes; `exec_webapp.py`'s standalone page goes through
the same path so the browser-agent UI cannot drift.

Fonts, KaTeX, and the shared CSS/JS are **vendored under
[app/static/](app/static/)** rather than fetched from a CDN at page load — the
product is offline-first, and a page that needs the network to render is not.
`scripts/fetch_ui_static.py` pulls upstream, `scripts/sync_ui_static.py` copies
the theme/JS out of the Python modules, and `tests/test_ui_tokens.py` enforces
that pages render from tokens with no external asset host.

**Selected endpoints** (full list in [app/api/routes.py](app/api/routes.py)):

```
GET  /health
POST /audio/start · /audio/stop · /vision/start · /vision/stop
POST /notifications/start · /desktop-capture/start
GET  /memory · /memory/search?q=
GET  /console/*  (events, turns, sessions, activity, jobs, models, escalate,
                  readiness, provenance, agent-runs, latency, heads,
                  desktop-access, attention, economy, hardening)
GET  /artifact
GET  /graph/context · /graph/stats · POST /graph/rebuild
GET  /people/list · /people/{id} · /people/unresolved-mentions
GET  /profile · /profile/data · /org/{id} · /org/{id}/data
GET/POST /peer/*  (pair, ask, answer, policy, link)
GET  /facts · /facts/open_tasks · POST /facts/{id}/approve|dismiss|done|edit
POST /reflect/run · GET /reflections*
POST /chat · GET /chat/poll · GET /chat/stream · POST /chat/answer · POST /chat/new
GET  /capture/status  (per-source support + reason on this platform)
POST /privacy/stop  (revoke consent AND halt the running pipelines)
GET  /privacy/egress  (spend cap + what has actually left this machine)
GET  /privacy/wipe/preview · POST /privacy/wipe  (erase + deletion receipt)
POST /desktop · POST /phone
GET  /onboarding* · GET /ui · POST /speak · GET /speakers
```

---

## Tech stack

| Layer | Tool | Role |
|---|---|---|
| **API / server** | FastAPI + Uvicorn, Pydantic | HTTP surface |
| **Config** | python-dotenv | env / `.env` |
| **Audio** | sounddevice, Silero VAD, faster-whisper | capture → transcript |
| **Speaker ID** | SpeechBrain ECAPA-TDNN | clusters + voiceprints |
| **Vision** | OpenCV + Ollama `minicpm-v` → Claude | frame understanding |
| **Local text** | Ollama (opt-in) → Claude | chat / extract / reflect |
| **Phone** | winsdk toasts · PowerShell Phone Link · QR phone channel | notify / SMS / pair |
| **Peers** | HTTP + mutual tokens | Sparrow↔Sparrow asks |
| **Embeddings** | sentence-transformers MiniLM | semantic vectors |
| **Stores** | SQLite + LanceDB | timeline + meaning search |
| **Browser agent** | Playwright + Claude tiers | web actions |
| **Desktop agent** | UIA (Win) / AT-SPI (Linux), allowlisted OS control | app-level actions |
| **UI** | server-rendered pages + vendored fonts/KaTeX/CSS/JS | offline-first, one theme |

---

## Configuration reference

All settings are read from the environment / `.env` with sane defaults (see
[app/config.py](app/config.py), [desktop_agent/config.py](desktop_agent/config.py),
[browser_agent/config.py](browser_agent/config.py)). A `.credentials.env` is
loaded after `.env` with override, for secrets.

> **Naming note:** the company is **Ravenry**; the product and in-app assistant
> are **Sparrow** ([app/api/mnemos_theme.py](app/api/mnemos_theme.py)). Env vars
> and the SQLite file keep the `QUILL_` / `quill.db` prefix from an earlier
> name — deliberately, so existing configs and data keep working.

### Core / server

| Var | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude vision, extraction, reflection, and the agent |
| `GOOGLE_API_KEY` | — | optional; enables the Gemini vision backend |
| `QUILL_HOST` / `QUILL_PORT` | `127.0.0.1` / `8000` | API bind |
| `QUILL_DATA_DIR` | `data` | relocate all persisted data |
| `QUILL_CREDENTIALS_FILE` | `.credentials.env` | secrets file loaded after `.env` |
| `QUILL_AUTOSTART` | `0` | `1` = start capture on server boot (set by `run_all.py`) |
| `QUILL_AGENT` | `1` | `0` = memory-only retriever, disables watchers |

### Audio (M1)

| Var | Default | Notes |
|---|---|---|
| `QUILL_ASR_ENGINE` | `whisper` | which ASR engine (`app/services/asr.py`); set back to `whisper` to roll back a swap |
| `QUILL_ASR_WORD_TIMESTAMPS` | `0` | word-level timings in `meta`; costs time on Whisper, free on engines that emit them natively |
| _(file)_ `data/asr_calibration.json` | none | per-engine ingest thresholds fitted by `scripts/calibrate_asr_confidence.py`; absent = every engine judged on Whisper's confidence scale |
| `QUILL_WHISPER_MODEL` | `small` | `base`, `medium`, `large-v3-turbo`, … |
| `QUILL_WHISPER_COMPUTE` | `int8` | `int8`, `float16` (GPU), `float32` |
| `QUILL_WHISPER_DEVICE` | `cpu` | `cuda` for a big speedup |
| `QUILL_AUDIO_DEVICE` | (PortAudio default) | mic index or name substring |
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
| `QUILL_CAMERA_BACKEND` | `dshow` (Win) / `v4l2` (Linux) | `gstreamer`/`any` also valid on Linux; Windows: `dshow`/`msmf`/`any` |
| `QUILL_CAMERA_FOURCC` | `MJPG` (Win), empty (Linux) | Win: avoids green/noise frames; leave empty on Linux |
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
| `QUILL_TEXT_LOCAL` | `0` | local-first TEXT via Ollama; off = Claude-only |
| `QUILL_TEXT_LOCAL_MODEL` | `llama3.2` | local Ollama text model |
| `QUILL_TEXT_LOCAL_TIMEOUT_S` | `45` | local text timeout |
| `QUILL_TEXT_ESCALATE_MIN_CONF` | `0.6` | escalate below this self-reported confidence |
| `QUILL_TEXT_HIGH_STAKES_TASKS` | `plan` | comma-separated tasks that always escalate |
| `QUILL_TEXT_LOCAL_MAX_PROMPT_CHARS` | `12000` | prompts longer than this skip the local tier (`0` disables the guard) |
| `QUILL_OLLAMA_KEEP_ALIVE` | `30m` | keep the local model resident instead of evicting it after ~5 min idle |
| `QUILL_OLLAMA_WARMUP` | `0` | `1` = load the local model at startup on its own thread |

### Latency spans & classifier heads

| Var | Default | Notes |
|---|---|---|
| `QUILL_LATENCY_SPANS` | `0` | record stage spans → `data/latency_spans.jsonl` (`GET /console/latency`) |
| `QUILL_LATENCY_READ_LIMIT` | `20000` | rows the console/aggregator read from the tail of the trail |
| `QUILL_HEADS` | `off` | `off` \| `shadow` (log-only) \| `active` (only a confident "nothing here" skips the LLM) |
| `QUILL_HEADS_T_LOW` / `_T_HIGH` | `0.10` / `0.60` | three-band thresholds on p(needs the model) |
| `QUILL_HEAD_MIN_LABELS` | `200` | labels before a head may be fit at all (higher than the router's 50 — a thin fit is a silent dropper) |
| `QUILL_HEADS_RETRAIN_LABELS` | `50` | new labels that trigger an idle retrain |
| `QUILL_HEADS_MAX_DISAGREEMENT` | `0.02` | activation gate, measured over would-have-skipped events only |
| `QUILL_HEADS_MIN_SHADOW` | `200` | shadow observations before that share means anything |
| `QUILL_HEADS_DIR` | `data/heads` | versioned per-head model files |

### Chat attachments & research writeback

| Var | Default | Notes |
|---|---|---|
| `QUILL_ATTACH_CONTEXT_CHARS` | `16000` | how much of an attached document rides along in the turn after the upload (fact mining still reads the whole file in the background) |
| `QUILL_RESEARCH_INGEST` | `1` | write agent/web research answers back to memory as `chat.research` events + facts |
| `QUILL_RESEARCH_INGEST_SYNC` | `1` | `0` = async (production-like); `1` = the next memory question sees the facts immediately |
| `QUILL_RESEARCH_INGEST_MIN_CHARS` | `40` | shorter answers are refusals/status lines, not research |

### Learning loop (verdict harvesting → exemplars → shadow eval → router → LoRA)

| Var | Default | Notes |
|---|---|---|
| `QUILL_LEARNING` | `1` | harvest every human verdict into `learning_pairs` (local-only writes) |
| `QUILL_LEGACY_DISTILL` | `1` | keep dual-writing `escalate_distill.jsonl` during the transition |
| `QUILL_EXEMPLARS` | `0` | exemplar-store retrieval into the local prompt (few_shot stays the fallback) |
| `QUILL_EXEMPLAR_K` | `3` | exemplars injected per call |
| `QUILL_EXEMPLAR_MIN_SIM` | `0.75` | cosine floor per retrieved exemplar |
| `QUILL_EXEMPLAR_TOKEN_BUDGET` | `1200` | total added tokens per call |
| `QUILL_SHADOW_EVAL` | `0` | idle re-grading of kept local outputs by Claude |
| `QUILL_SHADOW_BATCH` | `20` | outputs graded per night (stratified); raise it to drain a backlog |
| `QUILL_SHADOW_LOOKBACK_DAYS` | `30` | how far back the sampler may reach (a 24h window silently discarded surplus labels) |
| `QUILL_SHADOW_BUDGET_TOKENS` | `250000` | hard daily token ceiling (≈$0.40–0.50/day at Haiku 4.5 list rates) |
| `QUILL_SHADOW_MODEL` | `claude-haiku-4-5` | grader tier |
| `QUILL_SHADOW_AUTOTRUST` | `0` | let UNCONFIRMED shadow pairs train (lowers label quality) |
| `QUILL_ROUTER` | `off` | `off` \| `shadow` (log-only) \| `active` (explicit flip; rollback = `shadow`) |
| `QUILL_ROUTER_T_LOW` / `_T_HIGH` | `0.25` / `0.6` | three-band thresholds on p(local fails) |
| `QUILL_ROUTER_MIN_LABELS` | `50` | labels required before the first fit |
| `QUILL_ROUTER_RETRAIN_LABELS` | `25` | new labels that trigger an idle retrain |
| `QUILL_LORA_MIN_TYPE_PAIRS` | `300` | confirmed pairs per task type before LoRA saturation |
| `QUILL_LORA_PLATEAU_EPS` | `0.01` | exemplar A/B delta change that counts as plateaued |

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
| `QUILL_FIRST_RUN_MODE` | `full` | `meeting` = calendar-window capture only; `ambient` = consented always-on; `full` = existing consent-resume. Tester profile pins `meeting`. |
| `QUILL_MEETING_PAD_MIN` | `5` | minutes of audio pad before/after a calendar meeting |
| `QUILL_UNLOCK_AFTER_BRIEFS` | `3` | Console shows a one-time ambient-capture card after N briefs |
| `QUILL_PROFILE` | — | `tester` pins meeting-first and hard-off for Phone Link / anticipate / desktop capture |
| `QUILL_EXHAUST_INGEST` | `1` | Gmail/Calendar *metadata* cold-start (headers + attendees, never bodies) |
| `QUILL_EXHAUST_DAYS` | `90` | lookback for exhaust ingest |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | — | Desktop OAuth client for exhaust (loopback redirect) |
| `QUILL_MCP` | `0` | read-only MCP memory server (`python -m mcp_server`) |
| `QUILL_EXTERNAL_CAPTURE` | `0` | Omi / phone-as-mic ingest at `POST /capture/external` |

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

### Memory, people, consolidation, worker, reflection

| Var | Default | Notes |
|---|---|---|
| `QUILL_SEMANTIC` | `1` | `0` falls back to substring search |
| `QUILL_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | local sentence-transformers model |
| `QUILL_SAVE_AUDIO` | `1` | `0` skips saving WAV clips |
| `QUILL_INGEST_FILTER` | `1` | ASR hygiene; many `QUILL_INGEST_*` sub-thresholds |
| `QUILL_CONSOLIDATE` | `1` | merge utterances into turns |
| `QUILL_CONSOLIDATE_MAX_GAP_S` | `8` | silence gap that starts a new turn |
| `QUILL_WORKER` | `1` | durable background job runner |
| `QUILL_WORKER_POLL_S` / `_MAX_ATTEMPTS` | `2.0` / `5` | worker poll / retry cap before a job is dead-lettered |
| `QUILL_EXTRACT` | `1` | run fact/task extraction (calls the LLM) |
| `QUILL_PEOPLE_V2` | `1` | People Intelligence v2 (candidates, contacts, policies) |
| `QUILL_OPEN_LOOPS` | `1` | waiting-on-me / waiting-on-them / unanswered-question detectors |
| `QUILL_MEETING_MODE` | `1` | meeting layer (calendar join, jots, briefs, session chat) |
| `QUILL_REFLECT` | `1` | run daily reflection (calls the LLM) |
| `QUILL_REFLECT_MODEL` | `claude-opus-4-8` | reflection model |
| `QUILL_REFLECT_MAX_RECENT` / `_MAX_OPEN` | `120` / `40` | packet size bounds |

### Agents

| Var | Default | Notes |
|---|---|---|
| `QUILL_PLANNER` | `1` | Personal Agent Layer on by default |
| `QUILL_APPROVAL_BIND` | `enforce` | `off` / `shadow` / `enforce` — hash-bind approvals |
| `QUILL_AGENT_CHANNEL` | — | e.g. `chrome` — browser channel for the in-app agent |
| `QUILL_AGENT_PROFILE` | — | persistent browser profile (logged-in session reuse) |
| `AGENT_DRY_RUN` | `approval` | `plan` / `navigate` / `draft` / `approval` / `full` |
| `AGENT_EXECUTOR_VISION` | `1` | let the executor read screenshots |
| `AGENT_VISION_ALWAYS` | `0` | attach a screenshot every step (vs. only when stuck) |
| `AGENT_VISION_SPARSE_AT` | `6` | attach a shot when fewer than N elements visible |
| `AGENT_DATA_DIR` | `./sessions` | agent session storage |
| `AGENT_BROWSER_CHANNEL` | — | low-level default browser channel |
| `AGENT_BROWSER_PIXEL` | `1` | pixel actions (`click_at` / `drag` / `press_key`) on canvas-like surfaces; `0` turns the whole path off |
| `AGENT_PIXEL_SHOT_MAX_EDGE` | `1400` | screenshot long edge the coordinates are measured on |
| `AGENT_PIXEL_EDGE_PAD` | `8` | keep-out margin inside the surface bounds |
| `AGENT_PIXEL_DRAG_STEPS` | `24` | intermediate mouse moves per drag (a single jump reads as a click) |
| `AGENT_CDP_LISTENERS` | `1` | CDP listener ground truth — click-wired `<div>`s become element_ids |
| `AGENT_CDP_SPARSE_AT` / `_MAX_NODES` | `8` / `300` | skip on busy pages / listener probes per scan |
| `AGENT_DONE_CHECK` | `1` | once-per-run check that rejects off-goal completions |
| `AGENT_POSTMORTEM` | `1` | post-run analysis persisted into procedural memory |
| `AGENT_DISTILL` / `_OBS_CAP` | `1` / `6000` | append `sessions/agent_distill.jsonl` / chars of observation kept |

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
| Timeline + facts + people + relations + reflections + activities + jobs + agent runs | `data/quill.db` (SQLite) |
| Raw audio utterances | `data/audio/<epoch>.wav` |
| Captured webcam frames | `data/frames/<epoch>.jpg` |
| Desktop screen frames + click crops | `data/desktop_frames/*.jpg` |
| Voiceprints | `data/speakers/*.npy` |
| Semantic index | `data/lance/` (LanceDB) |
| Model-call log | `data/model_calls.jsonl` |
| Stage-latency trail | `data/latency_spans.jsonl` (`QUILL_LATENCY_SPANS=1`) |
| Escalation distill trail | `data/escalate_distill.jsonl` |
| Agent-step distill trail | `sessions/agent_distill.jsonl` (+ `agent_harvest_state.json` watermark) |
| Per-engine ASR ingest thresholds | `data/asr_calibration.json` |
| Classifier-head models | `data/heads/` |
| Onboarding profile | `data/onboarding_profile.json` |
| First-run / capture-opt-in state | `data/first_run.json` |
| Exhaust ingest ledger | `data/exhaust_ledger.json` |
| MCP bearer token | `data/mcp_token` |
| Tester crash zips | `data/logs/sparrow-report-*.zip` |
| Source policy table (checked in) | `data/source_policies.json` |
| Peer / phone channel state | under `data/` (peer + phone registries) |
| Browser-agent sessions & profiles | `./sessions/` |
| Model weights | `~/.cache/huggingface` |

The timeline reloads from `data/quill.db` on startup. `/artifact` is
**path-confined to the data directory**. `data/` and `sessions/` are
git-ignored except checked-in config tables (`model_prices.json`,
`source_policies.json`).

**Erasure.** Captured content lands in three places, not one — `data/`, the
browser agent's `sessions/`, and `desktop_agent/sessions/` — so "delete
`data/`" leaves page captures behind.
[app/services/wipe.py](app/services/wipe.py) is the single enumeration of all
three (it reads each agent's own `SESSIONS_ROOT`, both of which are
env-relocatable), and three doors lead to it: **Privacy controls → Delete
everything** in the app, `scripts/uninstall.py` (`uninstall.bat` /
`uninstall.command`) with the server closed, and the Inno uninstaller's wipe
checkbox. Capture is stopped and the SQLite handles closed *before* the walk —
otherwise the delete races a live writer, and on Windows an open connection is
simply undeletable. Each run writes a **deletion receipt** beside the install
(never inside the directory it emptied) recording per-target counts and
anything that survived; shipped config is kept unless `--all` / `full=True`.

---

## Project layout

```
desktop_app.py           packaged desktop build: window (pywebview) + tray
run_all.py               launch everything (capture in-process + agent as child)
run_audio.py             live transcription
run_vision.py            live webcam understanding
run_desktop.py           desktop-agent standalone driver
exec_webapp.py           browser agent standalone (with Sparrow memory bridge)

app/
  config.py              central settings (frozen dataclasses, env-driven)
  runtime.py             frozen-build paths (bundle vs per-user writable dir)
  events.py              Event schema + EventBus
  main.py                FastAPI app + startup wiring
  storage.py             SQLite: events, facts, people, relations, …
  vectorstore.py         LanceDB semantic index
  api/routes.py          HTTP endpoints (routing only — page HTML lives in the page modules)
  api/mnemos_theme.py    single source of brand tokens (apply / apply_plain)
  api/chat_page.py · console_page.py · auth_page.py · desktop_access_page.py
  api/peer_page.py       Team pairing UI
  api/org_network_page.py Org AI Network UI
  api/profile_page.py    Living user profile UI
  api/org_page.py        Org living brief UI
  static/                vendored fonts · KaTeX · shared CSS/JS (offline-first)
  services/
    memory.py            Memory Engine
    asr.py · asr_calibration.py   ASR engine seam + per-engine ingest thresholds
    fast_heads.py        trained classifier rung below the local LLM
    latency.py           stage spans (queue / load / prefill / generation)
    agent_harvest.py     agent_distill.jsonl → learning_pairs (agent.act)
    research_ingest.py   research answers → memory · fact_titles.py display titles
    people_pipeline.py   People Intelligence v2
    graph.py             knowledge graph rebuild + context_for_person
    peer_channel.py      Sparrow↔Sparrow asks + disclosure policy
    org_client.py        Org Coordinator HTTP client
    org_digest.py        Upward redacted status digests
    org_priority.py      Downward company priority guidance
    org_escalate.py      Strategic people-escalation (not model distill)
    phone_channel.py     QR-paired phone channel
    meta_memory.py       at-risk / stale / forget audits
    audio.py · vision.py · desktop_capture.py · notifications.py
    consolidation.py · sessions.py · activity.py · worker.py
    extractor.py · resolution.py · reflector.py · onboarding.py
    first_run.py · exhaust_ingest.py · mcp_tools.py · trust.py
    external_capture.py · crash_report.py · meeting_capture.py
    export.py            backup / takeout · wipe.py  erase + deletion receipt
    model_fetch.py       resumable weight download (CLI + packaged first run)
mcp_server/              read-only stdio MCP adapter (HTTP to localhost)
docs/mcp.md · trust-layer.md · macos-meeting.md · key-vending.md ·
     pilot-agreement.md  what the pilot cohort is promised, in writing
uninstall.bat · uninstall.command  offline erase (scripts/uninstall.py)
packaging/               PyInstaller + Inno Setup for tester builds
    agent_planner.py · agent_bridge.py · readiness.py · multitask.py
    model_router.py · ollama_text.py · escalate_log.py · few_shot.py
    grounding.py · llm.py · voice.py
    todo_watcher.py · task_offer.py · anticipation.py · phone_watcher.py
    meeting_*.py         meeting join / notes / enhance / chat / mode

browser_agent/           Exec.AI web agent (DOM + pixel surfaces, distill.py trail)
desktop_agent/           guarded OS control
                         a11y.py → UIA (Win) / a11y_linux.py (AT-SPI)
                         ghost.py → ghost_x11.py + x11_util.py (X11 only)
                         app_promotion.py · app_templates.py (remember_app)
org_coordinator/         Hybrid Org AI Network coordinator (roles/goals/digests)
tests/                   ~2,400 unit tests
scripts/                 download_models (resumable) · uninstall · restore_backup ·
                         enroll_speaker · eval_* · distill_* ·
                         calibrate_asr_confidence · bench_* · feature_smoke ·
                         diagnose_today · fetch_ui_static · sync_ui_static ·
                         train_lora · kg_cutover_soak · org_network_smoke · phone_link/ …
```

---

## Development & testing

```powershell
python -m pytest tests -q               # ~2,400 tests
make eval                               # golden harness (hard thresholds)
make eval-people                        # people entity-resolution goldens
make eval-people-live                   # smoke against local quill.db (no LLM)
make eval-asr                           # ASR WER/latency against the fixture manifest
make asr-calibrate                      # fit per-engine ingest thresholds
make bench-latency · make listen-idle   # local-path latency and idle-cost benches
```

Full-system smoke, in tiers — cheap enough to run anywhere, deep when you ask:

```powershell
python scripts/feature_smoke.py              # Tier 1: API only, ~30s, no API key
python scripts/feature_smoke.py --ui         # + Playwright UI pages
python scripts/feature_smoke.py --live       # + agent routing eval (needs a key)
python scripts/feature_smoke.py --everything # + capture, browser live, goldens
python scripts/diagnose_today.py             # against a live server: the /today banner
```

| Suite | What it covers |
|---|---|
| **Unit suite** | Approval binding, source policies, commitment lifecycle, meeting layer, ranking snapshots, escalate log, local routing, few-shot, grounding, LoRA gate, peer channel, people pipeline, ASR engine seam + calibration, latency spans, classifier heads, pixel actions — hermetic (no network). |
| **Golden evals** | Commitment ownership, entity resolution, contact attribution with CI-able exit codes. |
| **ASR evals** | `scripts/eval_asr.py` over `tests/fixtures/asr_eval/` — WER + latency per engine, so a swap is measured. Audio itself is git-ignored; the manifest is checked in. |
| **Desktop agent** | `tests/test_desktop_guards.py` — path jail, hard blocks, default-deny, autonomy ladder. `test_desktop_a11y_linux.py` / `test_ghost_desktop_linux.py` cover the Linux body. |
| **Browser pixel** | `tests/test_browser_pixel.py` — live headless Chromium against a canvas game and an iframe-embedded one, plus the out-of-bounds refusal. |
| **UI** | `tests/test_ui_tokens.py` — pages render from theme tokens with **no external asset host**. |

CI runs the theme/static sync before its smoke tests.
`.github/workflows/perception.yml` guards the listening path on every PR that
touches it — unit tests on Linux (no models loaded) plus the ASR harness on
Windows against a real engine, because an engine swap or a threshold edit
doesn't raise, it quietly changes what becomes a memory.

---

## Security model

1. **Memory is context, never command authority.** Retrieved memory and peer
   answers can inform a draft; only a **live human reply** authorizes send /
   buy / delete / mutate.
2. **Risk classification is a table**, not a guess (`blocked` never reaches a
   surface).
3. **Browser agent:** hash-bound, source-grounded approval packets; no
   credential entry or CAPTCHA solving; dry-run levels cap every run.
   **Pixel actions are fenced:** they exist only on a promoted opaque surface,
   coordinates outside it are refused with the bounds quoted back, and a
   cross-origin frame never qualifies — so a misread screenshot cannot reach a
   Send/Buy button.
4. **Desktop agent:** allowlist *is* the sandbox (path jail, app/shell
   allowlists, hard-block list, budgets).
5. **Phone / peer channels:** pairing codes, token auth, redaction, offer-by-
   default disclosure; inbound messages never grant command authority.
6. **Desktop capture is opt-in**; no keystrokes.
7. **`/artifact` is path-confined** to the data directory.
8. **Approval hash binding** (`QUILL_APPROVAL_BIND=enforce`) — what you
   approved is what executes; drift forces a fresh ask. Approvals expire;
   duplicate sends are caught.
9. **Source policies** (`data/source_policies.json`) bound what each source
   class may mint. Missing table → deny, never allow.
10. **Cloud egress is privacy-gated** — never-send refused, sensitive content
    redacted, spend capped. Outcomes need evidence, not LLM claims alone.
11. **Speculative work never reaches a paid model** — enforced in the router
    and again inside the Claude client (`SpeculativeCloudCall`), and asserted
    over the call trail.
12. **Telemetry stores numbers, not text** — `head_observations` and
    `usage_daily` hold scalars and enum-ish strings only; the agent distill
    trail redacts observations before writing, and drops the observation
    entirely if redaction is unavailable.

> **Handle your keys carefully.** `.env` and `.credentials.env` are
> git-ignored but hold live API keys in plaintext.

---

## Design ethos

- **Memory first.** Capture exists to feed a trustworthy personal store —
  searchable, provenance-linked, human-correctable.
- **Network over notes.** People, orgs, and open loops are first-class so the
  system can answer relational questions and keep work from falling through.
- **Connection without surrender.** Peers and phones get composed answers under
  your policy; raw memory stays local.
- **Local-first.** VAD, ASR, speaker ID, embeddings, and opt-in vision/text run
  without a paid call. Claude is for stakes and distillation.
- **Graceful degradation.** Missing key, camera, local model, or non-Windows OS
  drops a feature — never the pipeline.
- **Trust is earned in the Console.** Every inference is traceable; every
  action is approvable; every verdict is training signal.

---

## Known gaps & roadmap

- **KG v2 read cutover** — affiliation primary-read follows `cutover_ready()`
  or `QUILL_KG_READ_V2`. Parity while `QUILL_KG_SHADOW=1`. Soak:
  `python scripts/kg_cutover_soak.py`. Rollback: `QUILL_KG_READ_V2=0`.
- **People calibration** — `make eval-people` / `make eval-people-live`.
  Threshold overrides: `QUILL_PEOPLE_AUTO_RESOLVE` /
  `QUILL_PEOPLE_AUTO_MARGIN` / `QUILL_PEOPLE_CREATE_NEW` (re-run goldens
  before loosening defaults).
- **Peer channel over LAN needs TLS** before pairing beyond localhost.
  Peer↔Person links are user-asserted only (`POST /peer/link`).
- **M6 connectors** — native Gmail/Outlook/HubSpot OAuth or IMAP still
  deferred. Capture-first slice: Outlook/Gmail windows classify as `email` and
  enrich People v2 contacts + `works_at` via
  `people_pipeline.ingest_email_network`.
- **Postgres / object storage** — deferred, gated on a second device.
- **Org AI Network (experimental)** — hybrid Org Coordinator + local digests /
  priority cascade / smart escalation. OFF by default (`QUILL_ORG_NETWORK=0`).
  See [Org AI Network](#org-ai-network-experimental).
- **Classifier heads are shadow-only.** Nothing activates: the gate needs 200
  labels and a shadow week, and `shadow` mode is what produces both. `frame_keep`
  in particular has a lower ceiling than the text heads — it cannot embed what
  it judges (the shared encoder is text, the subject is pixels), so it reads the
  window title plus the frame-diff scalar the capture loop already computes.
- **Latency program.** Phase 0 (stage spans) and Phase 1 (residency, prefix
  ordering) landed; the serial-vs-pipelined decision and local distillation are
  gated on baseline data and a shadow week that hasn't run.
- **Non-Whisper ASR engines** — the seam, calibration, and WER harness are in;
  no second engine ships yet. Any engine reporting a different
  `confidence_kind` needs `make asr-calibrate` before it decides what becomes a
  memory.
- **Desktop agent on Wayland** — X11 only; the ghost-window path declines
  rather than half-working.
- **The desktop build is unsigned.** `SparrowSetup.exe` triggers SmartScreen
  ("Windows protected your PC") on first launch, and the macOS bundle is not
  notarized. Code signing is the last gate before handing the installer to
  anyone outside the team. The packaged build also omits the browser agent and
  Org Coordinator — both spawn `[sys.executable, "script.py"]` children, which
  re-executes `Sparrow.exe` when frozen.
- `/chat/stream`'s generator isn't bounded by response close, so its route test
  is skipped rather than driven (it wedges `TestClient` lifespan teardown).

Closed recently: a packaged desktop build that actually runs (windowed shell,
per-user data, `--self-test`, and a CI job that launches what it built),
one-click erasure with a deletion receipt (`wipe.py`, `scripts/uninstall.py`)
and the `/privacy/egress` view behind it, resumable model pre-download, an
unattended install mode plus the `clean-install` CI job that runs the real
installer end-to-end on fresh Windows/macOS runners, local TTS (`voice.py`), Personal Agent Layer on by default,
desktop-agent guard tests, verdict → few-shot/LoRA learning loop, idle LoRA
scheduler, the ASR engine seam + per-engine ingest calibration, browser pixel
control for canvas pages, the desktop agent's Linux body and app promotion,
the UI split out of `routes.py` behind one theme with vendored assets, and the
tiered feature smoke harness.

---

## Org AI Network (experimental)

Hybrid company intelligence on top of personal Sparrow — **does not replace**
capture, memory, peer consent, or approval gates.

```
  Employee Sparrow (local)          Org Coordinator (:8100)
  ───────────────────────          ───────────────────────
  capture → memory → digest ──►    directory (roles, reports_to)
  priority injection ◄──────────   goals + cascade
  peer org_* kinds                 escalate router + rollups
```

| Flag / env | Purpose |
|---|---|
| `QUILL_ORG_NETWORK` | Master switch (default off) |
| `QUILL_ORG_COORDINATOR_URL` | Coordinator base URL (default `http://127.0.0.1:8100`) |
| `QUILL_ORG_ROLE` | `ic` / `manager` / `exec` / `ceo` |
| `QUILL_ORG_REPORTS_TO` | Manager's `node_id` |
| `QUILL_ORG_MANAGER_PEER_ID` | Paired peer id for digest/escalate delivery |
| `QUILL_ORG_DIGEST_MODEL` etc. | Anthropic parent models for org tasks |

**Run with Sparrow** (`QUILL_ORG_NETWORK=1` in `.env`):

```powershell
python run_all.py
# auto-starts Org Coordinator on :8100 unless already running
# --no-org-coordinator to skip
```

**Or coordinator alone:**

```powershell
python -m org_coordinator.main
```

**UI:** <http://127.0.0.1:8000/org-network> — register, run digests, pull
priorities, create goals. Also linked from Today / Team nav as **Org**.

**Smoke (in-process, no live server):**

```powershell
python scripts/org_network_smoke.py
```

Raw memory never leaves the laptop. Coordinator stores role-scoped summaries
and goals only. Claude remains the high-stakes parent via ModelRouter tasks
`org_digest` / `org_rollup` / `org_escalate` / `org_cascade`. People
escalation is logged separately in `data/org_escalations.jsonl` (not
`escalate_distill`).

---

## License & status

**Status:** experimental research prototype under active development — not
production software; APIs and schemas may change without notice.

**Copyright:** Copyright 2026 Ravenry LLC.

**License:** MIT — see [LICENSE](LICENSE). The Phone Link PowerShell scripts
under [scripts/phone_link/](scripts/phone_link/) are adapted from the
MIT-licensed `phonelink-mcp-server` project and retain that attribution.

**Deeper docs:** interactive API at <http://127.0.0.1:8000/docs> when the
server is running; per-subsystem design notes live in module docstrings.
Tester install: [TESTER_SETUP.md](TESTER_SETUP.md) (Windows) · [TESTER_SETUP-macos.md](TESTER_SETUP-macos.md) (macOS).
