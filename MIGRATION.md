# MIGRATION.md — Desktop Perception v1 (D0)

Cutover plan for consolidating the existing desktop-capture pipeline into the
four-layer perception subsystem (L0 metadata / L1 text / L2 frames / L3
semantics). **One pipeline, never two:** at every point in the cutover exactly
one producer emits `desktop.screen` events, selected by a flag, with a startup
assert refusing to run both. A double-write is a failure of the milestone.

Phases (from the implementation prompt — each ships and is reviewable alone):

- **Phase A — Safety floor** (shipped): L0 metadata stream + `gaps`,
  labeled pre-pixel privacy gate, redaction-before-any-log/egress (email/phone
  added), USD/day spend cap on cloud enrichment, pause→gap wiring + recent-
  capture view, cascading erasure. No capture-path behavior changes beyond the
  gate upgrades; the new pipeline's tables are created but L1/L2/L3 do not run
  until their phase flags flip.
- **Phase B — Text** (shipped behind flag): L1 delta OCR + FTS5 + `ocr_blocks`
  embeddings. `QUILL_PERCEPTION_L1=1` flips the `desktop.screen` producer from
  `desktop_capture._analyze_screen` to L1. Default remains **off** for soak.
- **Phase C — Frames** (shipped): L2 CAS WebP full+thumb on every L1
  capture, pin/unpin promotion, age+budget compactor (pixels → thumbs → never
  text). H.264 packing deferred. `QUILL_PERCEPTION_L2` default **on** (kill
  switch; L2 is a side-effect of L1, not a second producer).
- **Phase D — Semantics/corpus** (shipped behind flag): L3 jobs on the
  existing `jobs` queue (`l3_segment`, `l3_extract`, `l3_vlm_fallback`,
  `perception_export`). `QUILL_PERCEPTION_L3=1` stops `screen_extract`
  scheduling in the same commit (mutual exclusion). Chat/console still read
  `activities` until a follow-up flips them to `activity_blocks`. Parquet
  export + watermarks live; default flag remains **off** for soak.

## Per-module disposition

| Module | Disposition | When |
|---|---|---|
| `app/services/desktop_capture.py` | **Bridge now, replace loop in B/C.** Phase A upgrades its silent sensitive-window skip to `app/perception/privacy_gate.py` — every blocked frame now writes a `captures(kind='excluded', exclusion_rule=…)` row (labeled redaction, not a hole) before any pixels are encoded or saved; the cloud-escalating VLM call comes under the spend cap. **Phase B:** when `QUILL_PERCEPTION_L1=1`, `start()` runs `perception.l1_capture` instead of `_screen_loop` and refuses dual producers; Event shape stays compatible (`raw`=OCR text, `meta.capture_id`, `meta.window`). Click capture unchanged. Phase C replaces any residual JPEG save with content-addressed thumbnails. | A (gate), B (L1 flag), C (frames) |
| `app/services/screen_extract.py` | **Fold into L3.** Runs unchanged through Phase A–C. Phase D: when `QUILL_PERCEPTION_L3=1`, prompt + `_persist_facts` hygiene path run from `perception/l3_workers.py` as job kind `l3_extract` (idempotent per `capture_id`); `screen_extract` is not registered/enqueued in that same commit. Flag default **off** so soak before cutover. | D |
| `app/services/activity.py` | **Bridge, then supersede the screen-derived portion.** Audio-derived rollup (`heard:` join) stays as-is permanently. Phase D ships `activity_blocks` writers via `l3_segment`; chat grounding + console still read `activities` until a follow-up flip. Screen branch of `activity.rebuild()` deleted in that follow-up — not this commit. | D |
| `app/services/escalate_log.py` | **Keep; hardened in Phase A.** `_clean_payload` (and the `edited`/`local_error`/`meta` paths) now route through `perception/redaction.py`'s log tier: existing secret patterns **plus email + phone masking**. Applies to every new row regardless of caller. Existing rows: `scripts/purge_secret_rows.py` remains the retro sweep. | A |
| `app/services/vlm.py` / `vlm_gemini.py` | **Keep; budgeted in Phase A, L3-only in D.** Phase A inserts the spend-cap check before every cloud escalation inside `VLMRouter.describe` (budget exhausted → keep the local result, reason `budget_exhausted`, no cloud call). The existing secret-skip gate (local OCR saw a secret ⇒ no image bytes leave) is unchanged. Phase D restricts callers to L3 jobs. | A (cap), D (callers) |
| `app/services/model_router.py` | **Keep; budgeted in Phase A.** Ambient tasks (`extract`, `reflect`, `activity` — not user-initiated `chat`/`plan`) check the same USD/day budget before `_complete_claude`. Exhausted ⇒ `BudgetExhausted`; the local-first path already converts a parent failure into "keep local" and callers like `screen_extract` already treat an extract failure as retry-later, which is exactly the required degrade/re-queue behavior. | A |
| `app/services/surface_filters.py` | **Keep.** It stays the CLI/log-noise scrubber. The secret/PII redactor lives in `app/perception/redaction.py` (wrapping `app/services/redact.py`), not here — one redaction module, two tiers. | — |
| `app/services/redact.py` | **Keep as the secrets tier.** `perception/redaction.py` layers PII (email/phone) and the user blocklist on top. Existing callers (vlm `_tag`, desktop title gate) keep their current behavior. | A |
| `app/services/worker.py` + `jobs` table | **Reuse for L3.** No new lease table. Phase D adds a `capture_id`-keyed idempotency payload convention plus dedupe on `(type, norm_span_key, capture_id)` in the extractions layer; claim/lease/`requeue_stale_jobs` are used as-is. | D |
| `events` table / KG | **Unchanged; joined, not duplicated.** L0/L1 records live in `data/perception.db`. The bridge: every perception-era `desktop.screen` event carries `meta.capture_id`; `captures.meta_event_id` links to L0; L3 extraction lands in the KG through the existing `source_event_id` + verbatim-span evidence path. A KG claim → `extractions.capture_id` → `captures` → (`frame_line_map`→`ocr_lines` | `thumb_sha256` | `frame_sha256`) is the full degradation-aware join. | B–D |

## Why a separate `data/perception.db`

The prompt requires `PRAGMA user_version` + real migration steps; `quill.db`
has add-column-only migrations and no version stamp, and retrofitting a stamp
onto a live DB shared by 60+ services is riskier than starting the new tables
clean. A dedicated WAL DB also gives L0 its single-writer discipline (1 Hz
stream never contends with the busy `quill.db` lock) and makes the erasure
cascade auditable. Cross-store joins are by id in Python — already the repo's
pattern for SQLite↔LanceDB.

## Cutover order

1. **A1** — shipped: `perception/` package, tables (user_version = 1),
   privacy-gate upgrade inside `desktop_capture`, escalate-log PII redaction,
   spend cap wired into `vlm` + ambient `model_router` tasks, pause→gap,
   erasure job + endpoints. L0 starts/stops with the existing consent +
   pause plumbing (`screen` source).
2. **B1** — shipped behind flag: `QUILL_PERCEPTION_L1=1` flips the
   `desktop.screen` producer from `desktop_capture._analyze_screen` to L1
   (same `Event` shape + `meta.capture_id`). Default is **0**. The flag
   selects exactly one producer; start() refuses both. Old events are not
   backfilled into `captures` — pre-cutover history stays queryable where
   it lives today (documented, honest seam).
3. **B2** — soak: compare event volume/quality; `screen_extract` keeps
   consuming events from either producer unmodified.
4. **C1** — shipped: L2 CAS thumbnails + full frames + compactor replace
   ad-hoc pixel retention for L1 captures; `meta.frame_path` is the CAS thumb
   path; pin/unpin exempts full frames from the 72h drop. H.264 deferred.
5. **D1** — shipped behind flag: L3 jobs (segmentation, extraction,
   VLM-fallback, salience) on the existing queue; when
   `QUILL_PERCEPTION_L3=1`, `screen_extract` is not registered/enqueued.
   Screen branch of `activity.rebuild` + chat/console flip to
   `activity_blocks` remain follow-up work (avoids dual-rollup UX break).
6. **D2** — shipped: Parquet export (`perception_export` job) +
   `export_watermarks` (user_version = 3). Erasure already deletes
   overlapping partitions.

## Rollback

- **Phase A:** additive. `data/perception.db` can be deleted; the privacy-gate
  upgrade degrades to the old silent skip if the perception store is
  unavailable (gate never blocks less than before); spend cap has an explicit
  escape hatch `QUILL_CLOUD_BUDGET_USD_DAY=0` = unlimited (shipped default is
  **$2/day**); escalate-log PII masking rolls back by reverting one import.
  Nothing in A changes what is captured — it only labels, meters, and erases.
- **Phase B/C:** flip `QUILL_PERCEPTION_L1` (resp. `_L2`) back to 0 — the old
  loop resumes at the next capture start; events remain shape-compatible so
  downstream (extract/activity/chat) never notices. Perception tables keep
  their rows; no destructive migration in either direction.
- **Phase D:** flip `QUILL_PERCEPTION_L3` back to 0 — `screen_extract` /
  activity chaining re-register on next boot (one commit revert of the flag).
  `extractions` dedupe keys make a replay after rollback/re-cutover
  idempotent.

## Erasure contract (Phase A, applies to both old and new stores)

`perception/erasure.py::erase_window(ts_start, ts_end)` cascades over: all
`perception.db` tables in range → `quill.db` `desktop.screen`/`desktop.click`
events (+ facts whose `source_event_id` points at an erased event) → LanceDB
vectors for those event/fact ids → frame files (`data/desktop_frames`, future
`data/frames` CAS) → `escalate_distill.jsonl` rows (frame-path or
source+time match) → `export/<table>/date=*/…` Parquet partitions overlapping
the window. It reports a per-store deletion manifest and writes a
`gap(reason='privacy_excluded')` spanning the erased window so the timeline
stays honest.
