# QUILL — Program Progress Report #2

*Status snapshot — July 07, 2026 (evening). Supersedes `july_07_2026_status.md`.*

## TL;DR — what moved since report #1

Since the morning snapshot, QUILL went from "captures but doesn't understand" to a
working **hear → understand → review → act** loop, and grew a second actuator:

- **Facts layer (Track A) — COMPLETE.** Episodic events are now distilled into
  structured **tasks / commitments / claims** with provenance, person resolution,
  a status lifecycle, and a human **approve / edit / dismiss** review loop in the
  Console. This was the #1 priority in report #1.
- **Browser agent — 6 of 10 upgrades shipped**, incl. source-grounded approval
  packets, an eval suite, executor vision (DOM + screenshot), agent modes,
  dry-run levels, and a failure taxonomy.
- **Desktop agent — NEW subsystem**, integrated. Guarded OS control (open apps,
  make project folders, run allowlisted commands) behind an allowlist-is-the-sandbox
  design.

The three subsystems were built in parallel by multiple models against cleanly
separated files, so they compose rather than collide.

---

## 1. Capture & memory spine (M1–M3) — stable, built earlier

Unchanged foundation, still solid:

- **Audio (M1):** mic → Silero VAD → faster-whisper → `Event`, with ingest hygiene
  (drops Whisper hallucinations) and SpeechBrain ECAPA speaker ID (anonymous
  clusters + named voiceprints).
- **Vision (M2):** webcam → motion-based frame selection → Claude Opus 4.8 vision →
  structured `Event`, including page classification (`todo_list`, `notes`, …).
- **Memory (M3):** every `Event` → SQLite (`data/quill.db`) + LanceDB semantic
  index (MiniLM, 384-d, CPU). Raw WAV/JPEG saved and linked for provenance.
- **Consolidation:** adjacent utterances merged into conversational **turns**
  before extraction, so facts come from whole thoughts, not fragments.
- **Durable job queue:** a `jobs` table + single background `worker` drains
  processing off the capture/request path (`consolidate`, `extract`), survives
  crashes, coalesces bursts. "One queue, one worker" — no Celery/Redis.

## 2. Facts layer (Track A) — ✅ COMPLETE (new since report #1)

The "episodic events vs. extracted facts" split, built in five steps plus loose ends.

| Step | What | Status |
|---|---|---|
| 1 | Schema + additive migration (`facts`/`tasks`/`commitments`/`people`/`entities`, `events.extracted_at`, `facts.review`) | ✅ |
| 2 | Windowed **extractor** over settled turns → tasks/commitments/claims (Opus, structured output, one swappable `EXTRACTOR_MODEL`) | ✅ |
| 3 | **Person resolution** — cascade exact → prefix (Chris/Christopher) → embedding cosine; fuzzy merges recorded as aliases | ✅ |
| 4 | Vision to-do items → task facts; **status lifecycle** (open/done/cancelled); watcher rewire | ✅ |
| 5 | **Console review loop** — `/facts` list + approve/edit/dismiss/done + facts UI in `/console` with inline source-audio playback | ✅ |
| — | Loose ends: facts indexed into shared LanceDB (searchable alongside episodes); extract runs off the `jobs` queue | ✅ |

**Key properties:**
- Every fact carries a `source_span` (verbatim quote) + `source_event_id` →
  provenance line ("heard · 2:14 PM") and, in the Console, the actual audio clip.
- The Console is the **training layer**: correcting a speaker, killing a
  hallucinated commitment, or confirming a real one is the human signal that makes
  the agent trustworthy enough to act.
- Pipeline is genuinely producing real output: ~26 open tasks / 16 commitments
  distilled from the current timeline.

**Files:** `app/storage.py`, `app/services/extractor.py`, `app/services/resolution.py`,
`app/services/memory.py`, `/facts` routes + facts UI in `app/api/routes.py`.
**Tests:** `scripts/test_track_a.py`, `scripts/facts_schema_check.py`,
`scripts/run_extract.py --demo` — all pass.

**One known gap:** the browser-agent approval packet's "Source:" line is still
model-generated from the memory text block, not verbatim from the fact row. Wiring
real `fact_id` + timestamp into the packet is the last bridge between Track A and
the agent.

## 3. Browser agent ("Exec.AI", `browser_agent/`) — 6 of 10 upgrades shipped

A mature observe → act → verify agent (route → plan → execute → verify, recovery
ladder, procedural memory, approval gate). Against the 10-point "executive
assistant" wishlist:

**Already existed:** pre-action planning (#1), per-step verification (#4),
skill/playbook memory (#6).

**Shipped this cycle:**
- **#2/#7 Source-grounded approval packets** — `request_approval` now shows a
  structured Action / To / Subject / Body / **Why / Source** packet with
  **Approve / Edit / Cancel** (edit feeds a revision back so it re-drafts).
- **#5 Executor vision** — DOM + screenshot; Claude reads pixels itself (its own
  OCR, no extra engine). Adaptive: attaches the shot only when the accessibility
  tree is thin or it's stuck, to keep the per-step hot path cheap.
- **#9 Eval suite** — `scripts/eval_agent.py` + `browser_agent/eval_tasks.py`.
  Routing tier (no browser) tracks the safety-critical metric: approval
  false-negatives (baseline **0**). Live tier (read-only) tracks
  success/steps/latency/cost.
- **#3 Agent modes** (`browser_agent/modes.py`) — 7 task-specific policies
  (email / calendar / research / shopping / crm / form / general). Each carries
  guidance, extra approval patterns (**additive** to the global commit net, never
  subtractive), a posture line, and a `read_only` flag (research). `resolve_mode`
  keyword-matches the router envelope and is wired into the loop.
- **#8 Dry-run levels** (`cfg.DRY_RUN`: plan / navigate / draft / approval / full)
  — how far a run may go, independent of task; a safety lever for demos.
- **#10 Failure taxonomy** (`browser_agent/failures.py`) — `classify` labels
  blocks (login / captcha / timeout / wrong page / no-progress …) into recovery
  actions (LOGIN / REPLAN / INSTRUCT / STOP) and offers the human a numbered menu;
  explicitly refuses to enter credentials or solve CAPTCHAs.

**Vision proven load-bearing:** `scripts/eval_vision_task.py` renders an access
code entirely inside a `<canvas>` (no DOM text) and shows the agent fails to read
it text-only but succeeds with vision on — the hard case, not just the mechanism.

**Remaining wishlist:** reusable *site* playbooks beyond the current intent@site
recipes.

**Tests:** `scripts/test_approval_packet.py`, `scripts/check_vision.py`,
`scripts/eval_agent.py`, `scripts/eval_modes_dryrun.py`, `scripts/eval_vision_task.py`.
Note: modes/failures are covered by *live evals*, not assertion unit tests.

## 4. Desktop agent (`desktop_agent/`) — NEW, integrated

OS-level control, the counterpart to the browser agent. Because there is no
browser sandbox, **the allowlist IS the sandbox**.

- **Guardrails (all tested):** path **jail** under `QUILL_DESKTOP_JAIL`; **app
  allowlist** (cursor/code/notepad/explorer/chrome/terminal — launch by key, never
  raw path); **shell-verb allowlist** (read verbs auto, mutate verbs gated,
  else blocked); **hard-block list** (rm/del/format/reg/sudo/… + shell metachars +
  secret-path markers + `..`) that no prompt can unlock; **args-as-list,
  shell=False** (no injection); **tiered human approval** on mutating actions;
  **audit log**; per-task action budget + command timeout.
- **Integration:** the router now emits a `surface` field (browser | desktop |
  none); `surface=='desktop'` dispatches to a guarded observe→act loop over
  `DesktopDriver`. Approval is routed to the **live human**, never to memory.
- **Flagship:** "open Cursor and start a new project" works via make_dir +
  launch_app — no pixel automation. Verified end-to-end with a stubbed LLM.
- **Critical design rule carried forward:** QUILL memory is grounded in
  mic/camera content (attacker-influenceable), so retrieved memory is **context,
  never command authority** — only a live human reply may approve mutating actions.

**Not yet done:** a real live-API test through `/chat`; screenshot-based control
for GUI apps that lack a CLI.

## 5. Interfaces

- **`/console`** — Memory Console: timeline, search, provenance (clip/frame
  playback), confidence, low-confidence filter, and now a **Tasks** view with the
  approve/done/edit/dismiss review loop.
- **`/facts` API** — list (filter by kind/status/review), open_tasks, and
  approve/dismiss/done/edit — the programmatic review surface.
- **`/chat` + `/ui`** — dispatch a turn to the browser (or desktop) agent;
  memory-grounded; non-blocking poll for progress/approval.
- **`run_all.py`** — one command runs capture + API + browser-agent UI; flags
  `--no-audio/--no-vision/--no-browser/--browser-headless/--port/--browser-port`.

## 6. Still stub / aspirational

- **Voice / TTS** (`app/services/voice.py`) — still a stub (`{"spoken": false}`).
  M4 spoken Q&A not wired.
- **M5 knowledge graph** — partially realized: the facts layer now has
  people/entities/commitments tables, but no graph queries/relationships yet.
- **M6 broader agent surface** — email/calendar/CRM connectors not built (the
  browser agent drives these UIs directly instead).
- **Postgres / MinIO** — deferred to v0.3, gated on a second device.
- **Handwriting OCR** (Surya/Paddle) — deferred; Claude vision reads clear notes.

## 7. Testing coverage — an honest gap

The **facts layer is well-tested** (`test_track_a.py` + schema check + extractor
demo, all assertion-based). The **new agent modules are not**: `modes.py`,
`failures.py`, and the entire `desktop_agent/` (config/guards/driver) have **no
dedicated unit tests** — they're exercised only indirectly or by live eval scripts
that need an API key + Chromium. Given the desktop agent's guards *are* the
security boundary (allowlist-is-the-sandbox), `desktop_agent/guards.py` is the
single highest-value place to add real unit tests (it's pure decision logic —
trivially testable): assert `rm`/`..`/secret-paths/unknown-verbs are BLOCKED and
that jail escapes are rejected. Recommended before the desktop agent is trusted
in a live `/chat` run.

## 8. Docs drift to fix

`README.md` is now materially stale: it lists memory as *"in-memory for now"*,
`/chat` and the RAG brain as *stubs*, and the Layout section omits the facts layer,
the browser/desktop agents, the Console, and the job worker. All of those are built.
A README refresh is a good low-risk task to hand off.

## 9. Recommended next steps

1. **Unit-test `desktop_agent/guards.py`** — the security boundary is currently
   only indirectly covered; it's pure logic, so this is cheap and high-value.
2. **Close the Track A ↔ agent bridge** — pass real `fact_id` + event timestamp
   into the approval packet so "Source:" is verbatim-from-DB, not model-generated.
   Makes the whole hear→act loop provably grounded.
3. **Live-API desktop test through `/chat`** — the desktop agent is integrated but
   only verified with a stubbed LLM.
4. **README refresh** — bring docs back in line with reality.
5. **ModelRouter** — the extractor already exposes one swappable model constant;
   fold routing in alongside a second extraction model.

---

*Subsystems built in parallel; files are cleanly separated (facts layer in
`app/`, browser agent in `browser_agent/`, desktop agent in `desktop_agent/`), so
multiple models can keep extending them independently.*
