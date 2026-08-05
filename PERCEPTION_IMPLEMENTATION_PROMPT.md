# Implementation Prompt: Desktop Perception Capture & Ingest Pipeline (v1)

> Hand this to a coding agent working **inside the existing Mnemos Windows prototype** (`nexus_v1`). This is a **brownfield** task: a desktop-capture path already exists and has known security holes this milestone must close, not step around. Python 3.11+, SQLite (WAL) + LanceDB + an existing memory/KG layer are present. Read the "BROWNFIELD CONTEXT" and "SECURITY PRECONDITIONS" sections before writing any code — they override the greenfield-sounding architecture below wherever they conflict.

---

## ROLE

You are a senior systems engineer and ML data engineer building the **desktop perception subsystem** for a local-first personal intelligence platform. Capture what happens on the user's screen with high fidelity, low storage cost, and a data layout designed for (a) grounded recall with provenance and (b) future training of small local models. **Correctness of capture and ingest is the top priority: no silent data loss, no unlabeled gaps, no ambiguous timestamps — and no secret/PII leaving the machine or landing in a log.**

---

## BROWNFIELD CONTEXT (read first — this is not greenfield)

The repo already contains an overlapping capture pipeline. You are **replacing and consolidating it**, not building a second one beside it. A double-write system is an explicit failure of this milestone (it re-creates the duplicate-extraction / lost-provenance risk the codebase already fights).

Existing modules and how they relate to the four layers below:

| Existing module | ~LOC | What it does today | Disposition in this milestone |
|---|---|---|---|
| `app/services/desktop_capture.py` | 454 | screen frames + click CV; emits `Event(source="desktop.screen" / "desktop.click")` onto the in-proc bus | **Replace** its capture loop with L0+L1+L2. Keep emitting into the same downstream so nothing breaks, but behind the new privacy+redaction gates. |
| `app/services/screen_extract.py` | 161 | frames → facts/entities via VLM | **Fold into L3** (async enrichment). No inline model calls in capture. |
| `app/services/activity.py` | 341 | `activities` rollup (app/focus/click trail, heard:/saw: join) | **L3 activity_blocks supersedes** the screen-derived portion. Migrate or bridge; do not duplicate. Audio-derived activity stays as-is. |
| `app/services/escalate_log.py` | 307 | writes `data/escalate_distill.jsonl` (model distill rows) | **Must go through the redaction stage** (see SECURITY). Today it leaks secrets — fix as part of this work. |
| `app/services/vlm.py` / `vlm_gemini.py` | 473 / 88 | local + cloud VLM | **Callable only from L3**, only after redaction, only under the spend cap. |
| `app/services/surface_filters.py` | 408 | `scrub_vision_result` (strips CLI/log lines only — **not secrets**) | Extend: add the secret/PII redactor here or in a new `perception/redaction.py`; wire it BEFORE model calls and log writes. |
| `app/services/worker.py` + `storage.py` `jobs` table | 119 | durable single-worker queue: `enqueue_job` / `claim` / `mark_done` / `requeue_stale_jobs` (at-least-once already) | **Reuse this queue for L3.** Do NOT invent a new lease table — extend the existing `jobs` table with the `capture_id`-keyed idempotency you need. |
| `events` table (`time,modality,raw,summary,source,meta,audio_path`) | — | what the KG extractor consumes | New L0/L1 records are their own tables; L3 extractions land in the KG as belief candidates via the **existing** `source_event_id` + verbatim-span provenance path. Define the join from `captures` → `events`/KG explicitly. |

**Deliverable D0 (write this first): `MIGRATION.md`** — for each module above, state exactly: replace / fold-in / bridge / delete, the cutover order, how existing `desktop.screen` events are migrated or dual-emitted during transition, and a rollback. No new capture code merges until this is reviewed.

---

## SECURITY PRECONDITIONS (these fix live P0s — non-negotiable, ship before the corpus)

An audit of the current capture path found these **open, live** issues. This milestone must not re-open them, and Phase A (below) must close them:

1. **Redaction before ANY model call or log write.** A real API key is currently sitting in `data/escalate_distill.jsonl` because OCR/VLM text is sent to the cloud and logged with no scrubbing. Build a mandatory `redact(text_or_payload) -> redacted, hits[]` stage applied to: OCR line text, VLM captions, the frame region handed to any VLM, and **every** row written to `escalate_distill.jsonl` / any distill/telemetry log. Patterns at minimum: `sk-[a-zA-Z0-9-]{20,}`, `AIza[0-9A-Za-z_\-]{20,}`, bearer/JWT, private-key headers, email, phone, and the privacy blocklist domains. On a secret-shaped hit in OCR, **prefer skipping the model call entirely** over redact-then-send. Log a `redaction` counter, never the secret. Add a test asserting a planted `sk-ant-…` never appears in any log or egress payload.
2. **Hard spend cap on enrichment.** Nothing in `ModelRouter` enforces a dollar ceiling today. L3 must draw remote model calls from a configurable USD/day budget (default e.g. $2/day) enforced **before** the call; when exhausted, degrade to local-only or re-queue — never call the cloud unmetered. The `<40-char-OCR → VLM` heuristic bounds frequency but is NOT a budget; both must hold.
3. **Local-first by default for enrichment, not just capture.** No network in the capture path (hard rule). L3 defaults to **local models**; remote escalation is an explicit, budgeted, redacted, per-user opt-in, and every capture that escalated records what left the machine. The product is marketed local-first; the corpus you build must not silently be a "shipped-my-screen-to-the-cloud" corpus.
4. **Consent surface — pause, live indicator, cascading erasure.** Because this OCRs every screen indefinitely, these are consent-critical, not v2 polish:
   - **Global pause** that immediately stops L1/L2 and writes `gap(reason='user_pause')`; capture starts **paused on first run** until an explicit in-UI opt-in.
   - **Persistent "capturing now / paused" indicator** + a "what was captured in the last N minutes" view.
   - **True erasure** job that cascades across SQLite rows + FTS5 + LanceDB vectors + the frame store + **exported Parquet** (the existing "delete" leaves media/vectors/logs on disk — this must not).

---

## PRIME DIRECTIVES

1. **Evidence, not screenshots.** Every derived claim carries pointers to immutable, content-addressed evidence records.
2. **Layered fidelity.** Cheap signals always; expensive signals selectively. Each layer has its own retention. The most expensive layer (pixels) is never a dependency for the most common queries.
3. **Training-ready by construction.** Typed, schema-versioned, append-only, Parquet-exportable without transformation gymnastics. Supervision signals (corrections, approvals, queries) are first-class from day one.
4. **Honest gaps.** If capture stops (sleep, crash, pause, privacy exclusion), write an explicit `gap` record with a reason. Downstream never has to infer whether silence means "nothing happened" or "we weren't looking."
5. **Local-first.** No network in the capture path. Enrichment defaults local; remote is budgeted, redacted, consented (see SECURITY).

---

## ARCHITECTURE: FOUR LAYERS

```
L0 Metadata stream   (always on)      → SQLite WAL, append-only
L1 Text layer        (on change)      → OCR deltas → SQLite FTS5 + LanceDB embeddings
L2 Frame layer       (salient only)   → content-addressed WebP + chunked H.264
L3 Semantic layer    (async)          → tasks/entities/activity blocks → KG, with L0–L2 provenance
```

Cost flows strictly upward: L0 gates L1, L1 gates L2, L3 consumes L0–L2 asynchronously. A failure in any higher layer must never stall a lower one.

### L0 — Metadata Stream (always on)

Poll OS accessibility / Win32 at 1 Hz; emit only on state change (debounce 500 ms). Per record: `ts_utc` (monotonic-corrected UTC ms), `session_id`, `seq`; `app_name`, `app_exe_hash`, `window_title`, `window_id`; `browser_url` + `url_domain` (registrable domain stored separately); `doc_path`; `input_state` = key/mouse **counts only, never contents** + idle flag (no input ≥ 60 s); `display_topology_hash`.

Notes: UIA/`pywinauto` or direct Win32 (`GetForegroundWindow`, `GetWindowText`, `QueryFullProcessImageName`); low-level hooks for counts only. Single writer thread → SQLite WAL; batch commit every 2 s; fsync on batch. On start, reconcile: if the last record's `ts_utc` is older than 2× cadence, write a `gap(reason='process_down')` spanning the hole.

**Windows reality (budget for these, don't assume):** `browser_url` via UIA is flaky on this codebase (has returned the notification feed as content before) — require a full-URL + registrable-domain parse with a graceful `url_unavailable` path rather than a wrong URL. Define "machine is on" precisely across sleep / hibernate / lock / fast-startup / modern-standby — the monotonic-clock correction must handle S3/modern-standby wake without emitting a false continuous span.

### L1 — Text Layer (change-triggered OCR with delta storage)

**Trigger (in order):** (1) L0 foreground/app/URL change → capture after 700 ms settle; (2) perceptual change: dHash (64-bit) of a downscaled foreground grab every 5 s, trigger on Hamming > 10; (3) max-interval fallback 120 s; (4) **scroll suppression:** if ≥ 70 % of new OCR lines already in the window's rolling line cache (last 2,000 line-hashes per `window_id`), mark `kind='scroll_delta'`, store only novel lines.

**Pipeline:** grab **foreground window only** at native res → **privacy gate + redaction pre-check (see below)** → local OCR with layout (Windows.Media.Ocr default, engine behind an interface so PaddleOCR/Tesseract can swap; record `ocr_engine`, `ocr_version`, mean conf) → normalize to `(line_text, bbox, confidence)`, drop conf < 0.55 but count in `dropped_low_conf` → per-line SHA-1, diff vs window cache, persist **only novel lines** + a `frame_line_map` so full visible text is reconstructable → insert novel lines into FTS5; embed **merged blocks** (contiguous lines ≥ 20 chars) into LanceDB keyed by `capture_id`.

**Privacy/sensitivity gate (pre-OCR, hard-blocking):** rule-based on (`app_exe_hash`, `window_title`, `url_domain`): password managers, banking, incognito/private windows, OS credential dialogs, user-editable blocklist. On match: no pixels leave RAM, no OCR; write `excluded_capture` with the rule ID (labeled redaction, not a hole). Default-exclude ONLY credential/financial surfaces — do not silently skip whole categories (e.g. terminals). Exclusions are visible/configurable in the UI.

**Redaction (post-OCR, pre-store/pre-egress):** run the SECURITY-#1 redactor over OCR text before it is stored *or* embedded *or* handed to L3. Secret-shaped content is dropped/masked at the line level and counted.

### L2 — Frame Layer (salient pixels, decaying retention)

Every L1 capture also produces a **thumbnail**: 960-px WebP, quality 55 (~30–60 KB), content-addressed `frames/<sha256[:2]>/<sha256>.webp`. Full-fidelity retention is **promotion-based** via the importance oracle (pen activity ±120 s, audio salience, calendar overlap, explicit pin). Promoted groups → 2–4 fps H.264, 5-min segments, CRF 30. Unpromoted full frames deleted after 72 h; thumbnails after 30 d; **OCR/embeddings kept indefinitely**. Citations store `frame_sha256` + `capture_id`; on expiry they degrade gracefully pixels → thumbnail → OCR text, and the record notes the degradation level.

### L3 — Semantic Layer (async enrichment)

Runs in a separate worker process **on the existing durable `jobs` queue** (`storage.enqueue_job` / claim-lease / `requeue_stale_jobs`), at-least-once, **idempotent per `capture_id`**. Do not build a new lease table; extend `jobs`.

Jobs: (1) **Activity segmentation** → `activity_blocks` (contiguous stable app/domain/doc, ended by ≥ 5 min idle or ≥ 3 min context switch), with member `capture_id`s — supersedes the screen-derived part of the existing `activities` rollup. (2) **Extraction** via **local** LLM over new L1 blocks → typed candidates (`task`, `commitment`, `entity_mention`, `decision`), each with confidence, verbatim span, `capture_id`; **typed JSON only**, schema-validated, reject+re-queue on failure (max 2, then dead-letter with raw output preserved). Land in the KG as belief candidates via the existing evidence path. (3) **VLM fallback** only when OCR < 40 chars AND thumbnail non-blank → local VLM caption, redacted, embedded — exception path, not default, and under the spend cap. (4) **Importance oracle** → append (never overwrite) to `salience_scores` with `model_version` — this is training data.

**Idempotency/dedupe:** key on `(type, normalized_span, capture_id)` — NOT raw span hash. LLM extraction is non-deterministic about exact spans; a raw-span key produces duplicate "distinct" candidates across re-runs. Normalize (or use a semantic key) before dedupe.

---

## STORAGE SCHEMAS (SQLite; every table carries `schema_version`)

```sql
-- L0
CREATE TABLE meta_events (
  id INTEGER PRIMARY KEY, session_id TEXT, seq INTEGER, ts_utc INTEGER,
  utc_offset_minutes INTEGER,
  app_name TEXT, app_exe_hash TEXT, window_id TEXT, window_title TEXT,
  browser_url TEXT, url_domain TEXT, doc_path TEXT,
  key_count INTEGER, mouse_count INTEGER, is_idle INTEGER,
  display_hash TEXT, schema_version INTEGER
);
CREATE TABLE gaps (id INTEGER PRIMARY KEY, ts_start INTEGER, ts_end INTEGER,
  reason TEXT, schema_version INTEGER);   -- reason ∈ process_down|sleep|user_pause|privacy_excluded|crash

-- L1
CREATE TABLE captures (
  capture_id TEXT PRIMARY KEY,            -- ulid
  ts_utc INTEGER, window_id TEXT, meta_event_id INTEGER,
  kind TEXT CHECK(kind IN ('full','scroll_delta','excluded','vlm_only')),
  trigger TEXT, frame_sha256 TEXT, thumb_sha256 TEXT,
  ocr_engine TEXT, ocr_version TEXT, ocr_mean_conf REAL, dropped_low_conf INTEGER,
  redaction_hits INTEGER, exclusion_rule TEXT,
  novel_line_count INTEGER, total_line_count INTEGER, schema_version INTEGER
);
CREATE TABLE ocr_lines (
  line_hash TEXT, window_id TEXT, first_capture_id TEXT, text TEXT,
  bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL, conf REAL,
  PRIMARY KEY (line_hash, window_id)
);
CREATE TABLE frame_line_map (capture_id TEXT, line_hash TEXT, line_order INTEGER);
CREATE VIRTUAL TABLE ocr_fts USING fts5(text, content='ocr_lines');

-- L3
CREATE TABLE activity_blocks (block_id TEXT PRIMARY KEY, ts_start INTEGER, ts_end INTEGER,
  dominant_app TEXT, dominant_domain TEXT, dominant_doc TEXT,
  input_intensity REAL, capture_ids TEXT, summary TEXT, schema_version INTEGER);
CREATE TABLE extractions (extraction_id TEXT PRIMARY KEY, block_id TEXT, capture_id TEXT,
  type TEXT, payload_json TEXT, confidence REAL, source_span TEXT, norm_span_key TEXT,
  model TEXT, model_version TEXT, egress TEXT,   -- egress ∈ local|remote:<provider>
  ts_utc INTEGER, schema_version INTEGER);
CREATE TABLE salience_scores (capture_group_id TEXT, score REAL, features_json TEXT,
  model_version TEXT, ts_utc INTEGER);

-- Supervision (training corpus, append-only)
CREATE TABLE supervision_events (
  id INTEGER PRIMARY KEY, ts_utc INTEGER,
  kind TEXT CHECK(kind IN ('query','query_click','extraction_confirm','extraction_reject',
                           'extraction_edit','action_approved','action_rejected','pin','unpin','exclusion_added')),
  target_type TEXT, target_id TEXT, payload_json TEXT, schema_version INTEGER
);
```

Add `PRAGMA user_version` + a real migration step number (the existing DB has add-column-only migrations and no version stamp — don't inherit that gap). Nightly job exports each table's new rows to `export/<table>/date=YYYY-MM-DD/*.parquet`; embeddings stay in LanceDB keyed by `capture_id` so Parquet + LanceDB join cleanly. The erasure job (SECURITY-#4) must also delete matching Parquet partitions.

---

## CORRECTNESS REQUIREMENTS (acceptance criteria)

1. **No unlabeled gaps:** for any 24 h window, `meta_events ∪ gaps` covers 100 % of wall-clock time while the machine is on (precise "on" definition per L0 notes). Hourly self-audit job verifies.
2. **Reconstruction fidelity:** for any `capture_id`, `frame_line_map → ocr_lines` reproduces the full visible text **byte-identical** to what was OCR'd (deltas are a storage trick, never lossy).
3. **Timestamp discipline:** all timestamps UTC ms, monotonic-corrected; record `utc_offset_minutes` at session start and on change; never store local time.
4. **Crash safety:** kill -9 the capture process at any point → no corrupt rows (WAL), a `gap` is written, line caches rebuild from `ocr_lines` without re-OCR.
5. **Privacy gate is pre-pixel:** test proves that for a blocklisted app, no frame bytes are ever written to disk and no OCR call occurs.
6. **Redaction is pre-egress and pre-log:** test proves a planted `sk-ant-…`/email/phone in OCR appears in **no** log, distill row, embedding, or model-call payload. (Closes the live P0.)
7. **Spend cap holds:** test proves L3 stops calling remote models once the USD/day budget is exhausted and degrades to local/requeue.
8. **Budget enforcement / degradation order:** configurable disk budget (default 15 GB/user-year). Daily compactor enforces tiers and logs deletions; degrades by dropping pixels first, thumbnails second, and **never** text/metadata/supervision.
9. **Throughput/overhead:** capture subsystem ≤ 3 % avg CPU and ≤ 400 MB RSS during normal 8 h use **measured with the audio pipeline running concurrently** (it competes for CPU with two Whisper instances + embeddings — measure in that contention, not in isolation); OCR ≤ 1.5 s p95 per capture on target hardware; enrichment backlog drains within 10 min of idle.
10. **Idempotent ingest:** re-running any L3 job over the same inputs produces no duplicate KG candidates (dedupe on `(type, norm_span_key, capture_id)`).
11. **Erasure is complete:** test proves the erase job removes the target's SQLite rows, FTS entries, LanceDB vectors, frame files, AND Parquet partitions — nothing survives on disk.

## TEST PLAN (build before declaring done)

- **Synthetic session harness:** scripted window-switch + scroll + typing with known ground truth; assert trigger counts, scroll-suppression ≥ 80 % on the scroll segment, full-text reconstruction.
- **Property tests** on the delta store: random insert/evict sequences → reconstruction always equals simulated screen text.
- **Chaos tests:** kill capture and enrichment mid-write; assert criteria 4 and 10.
- **Privacy tests:** mock password-manager window → assert criterion 5 and that the timeline UI shows a labeled redaction.
- **Redaction/egress test:** criterion 6 (the live-P0 regression guard).
- **Spend-cap test:** criterion 7.
- **Erasure test:** criterion 11.
- **Storage soak:** simulate 30 days at realistic rates; assert the compactor holds budget and the degradation order (pixels → thumbnails → never text).

## PHASING (ship security-critical layer before the corpus)

Do NOT ship this as one milestone. Sequence so the holes that are open today close first and independently of the training-corpus ambition:

- **Phase A — Safety floor (ships alone, closes live P0s):** `MIGRATION.md` (D0), L0 metadata stream + `gaps`, pre-OCR privacy gate, the redaction-before-any-model-or-log stage, capture **pause + live indicator**, and cascading **erasure**. Defensible to run; fixes what's bleeding.
- **Phase B — Text:** L1 delta OCR + FTS5 + embeddings + reconstruction/property tests.
- **Phase C — Frames:** L2 thumbnails + promotion + compactor + disk-budget enforcement.
- **Phase D — Semantics/corpus:** L3 async enrichment (local-first, spend-capped) + Parquet export + supervision-driven corpus.

Phase A must not depend on B–D. The higher-fidelity pipe must never capture *more* while the leak is still open.

## EXPLICIT NON-GOALS (v1)

- No full-desktop multi-monitor stitching (foreground window only).
- No cloud sync; no remote inference in the capture path (enrichment remote is opt-in/budgeted only).
- No keylogging of content — input is counts and idle only.
- No model training in this milestone — only the corpus + supervision logging that make it possible later.

## DELIVERABLES

0. `MIGRATION.md` — per existing module (`desktop_capture.py`, `screen_extract.py`, `activity.py`, `escalate_log.py`, `vlm*.py`): replace/fold/bridge/delete, cutover order, dual-emit-during-transition plan for `desktop.screen` events, rollback. **Merged and reviewed before any capture code.**
1. `perception/` package: `l0_meta.py`, `l1_capture.py`, `privacy_gate.py`, `redaction.py`, `l2_frames.py`, `l3_workers.py`, `compactor.py`, `export_parquet.py`, `erasure.py`, shared `schemas.py` (pydantic, schema-versioned).
2. Migration scripts for the tables above (with `PRAGMA user_version`).
3. Test suite implementing the full test plan (including the redaction, spend-cap, and erasure regression guards).
4. `PERCEPTION.md`: data-flow diagram, retention table, budget math (disk + USD/day), the exact join paths from a KG claim back to its evidence at each degradation level, and the consent/pause/erasure UX contract.
```
