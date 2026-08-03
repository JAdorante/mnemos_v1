# PERCEPTION.md — Desktop Perception Subsystem

Layered desktop capture for grounded recall with provenance and a
training-ready corpus. **Phase A (safety floor) ships always-on.**
**Phase B (L1 text) ships behind `QUILL_PERCEPTION_L1` (default off).**
**Phase C (L2 frames) ships with L1 when `QUILL_PERCEPTION_L2=1` (default on).**
**Phase D (L3 semantics) ships behind `QUILL_PERCEPTION_L3` (default off).**
See `MIGRATION.md` for cutover.

## Data flow (A–C live; D behind flag)

```
                    ┌─────────────────────────────────────────┐
  Win32 / idle ───► │ L0  meta stream (1 Hz, debounce 500 ms) │
  (counts only)     │     → meta_events + gaps (perception.db)│
                    └───────────────┬─────────────────────────┘
                                    │ gates L1
                    ┌───────────────▼─────────────────────────┐
  foreground grab ─►│ privacy_gate (pre-pixel)                │
                    │   match → captures(kind=excluded) STOP  │
                    │   miss  → pixels may leave RAM (OCR only)│
                    └───────────────┬─────────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
     L1 OCR deltas (B)     L2 thumbnails (C)     VLM desktop_capture
     FTS5 + ocr_blocks     CAS WebP / H.264      (producer when L1=0)
              │                     │                     │
              └──────────┬──────────┘                     │
                         ▼                                ▼
              redaction (secrets always;          Event(source=desktop.*)
              PII on log/egress)                   + meta.capture_id (L1)
                         │                                ▼
                         └──────────► L3 jobs (D, flag) ──► KG via source_event_id
                                      l3_extract / segment / export
                                      spend_cap gates cloud; screen_extract OFF
```

**Hard rules:** no network in the capture path; enrichment defaults local;
remote is opt-in, redacted, and USD/day capped. Input is **counts + idle
only** — never key/mouse contents. **Exactly one** `desktop.screen`
producer: L1 *or* the legacy VLM loop — never both.

## Retention (by layer)

| Layer | What | Retention today | Planned (C) |
|---|---|---|---|
| L0 `meta_events` / `gaps` | window metadata, labeled holes | indefinite | indefinite |
| L1 `ocr_lines` / FTS / `ocr_blocks` | text + vectors | indefinite (when L1 on) | indefinite |
| L2 full frames | CAS WebP `data/frames/<hh>/<sha>.webp` | 72 h unless pinned | same |
| L2 thumbnails | CAS WebP (960px q55) | 30 d | same |
| L2 promoted H.264 | 2–4 fps segments | deferred | until budget forces drop |
| L3 `extractions` / `activity_blocks` / `salience_scores` | typed candidates | writers when `QUILL_PERCEPTION_L3=1` | indefinite |
| Supervision | query/confirm/erase signals | append-only from A | indefinite |
| Distill log | `escalate_distill.jsonl` | append-only, redacted at write | purge via erasure |

Disk budget (Phase C): default **15 GB / user-year**. Compactor drops
**pixels → thumbnails → never text/metadata/supervision**.

## Budget math (USD/day)

| Knob | Default | Meaning |
|---|---|---|
| `QUILL_CLOUD_BUDGET_USD_DAY` | **2.0** | Hard ceiling on ambient cloud spend (UTC day) |
| `0` (or negative) | uncapped | Explicit escape hatch — never the shipped default |
| `QUILL_BUDGET_AMBIENT_TASKS` | `vision,extract,reflect,activity,screen_extract,consolidate` | Tasks that draw from the ledger |

Enforcement seams (before the call, fail closed):

- `VLMRouter.describe` — all three cloud-escalation sites
- `ModelRouter._complete_claude` — ambient text tasks only

Recording: `model_log.log_call` for cloud + ambient → `spend_ledger`.
User-initiated `chat` / `plan` / browser agent are **not** drawn from this
budget. Exhausted ⇒ keep local / re-queue — never unmetered cloud.

## Evidence join (KG claim → text/pixels, with degradation)

Phase B+C make the **text** and **pixel** joins live via `meta.capture_id`
and CAS SHAs on L1 events. Phase D adds `extractions` + `activity_blocks`
as first-class L3 rows (still joined through the same capture id):

```
KG fact / extraction
  └─ source_event_id ──► events (quill.db)          # existing path
  └─ events.meta.capture_id / extractions.capture_id
        └─ captures (perception.db)
              ├─ meta_event_id ──► meta_events       # L0 context
              ├─ frame_line_map → ocr_lines          # full text (byte-identical)
              ├─ ocr_blocks (LanceDB)                # embedded merged blocks
              ├─ thumb_sha256 → data/frames/<hh>/…   # live in C
              └─ frame_sha256 → same CAS layout      # live in C (72h / pinned)
activity_blocks.capture_ids[] ──► same captures rows
```

When `QUILL_PERCEPTION_L3=1`, `l3_extract` is enqueued per new L1
`desktop.screen` event carrying `meta.capture_id`; legacy `screen_extract`
is not scheduled. Chat/console still ground on `activities` until a
follow-up flip.

Degradation levels a citation may report:

1. **full** — `frame_sha256` present
2. **thumb** — only `thumb_sha256`
3. **text** — OCR reconstructable via `frame_line_map`
4. **meta** — L0 window record only (exclusion / erased pixels)

Disk budget default **15 GB** on the frames tree; compactor drops full →
thumb and never text/metadata/supervision. Pin via `POST /perception/pin`.

## Consent / pause / erasure UX contract

| Surface | Behavior |
|---|---|
| First run | Capture sources **off** until Privacy consent (`capture_consent`) |
| Live indicator | `GET /perception/status` → `capturing` / `paused` + L0 + spend + 24 h coverage |
| Recent view | `GET /perception/recent?minutes=N` → meta_events, captures (incl. exclusions), gaps |
| Pause screen | `POST /capture/pause {source:screen}` stops pixels **and** L0; opens `gap(reason=user_pause)` |
| Resume screen | `POST /capture/resume` closes the pause gap and restarts L0 |
| Blocklist | `GET/POST/DELETE /perception/blocklist` — titles / apps / domains (+ builtins) |
| Erase | `POST /perception/erase {ts_start_ms,ts_end_ms}` — cascading wipe (≤ 90 days) |

Erasure cascade (nothing survives on disk):

1. `perception.db` rows in window  
2. `quill.db` `desktop.*` events + derived facts/relations (+ VACUUM)  
3. LanceDB vectors for those ids  
4. Frame files (`desktop_frames` + future CAS `frames/`)  
5. Matching `escalate_distill.jsonl` rows (atomic rewrite, **no** `.bak`)  
6. Overlapping `export/<table>/date=YYYY-MM-DD` Parquet partitions  

Then: `gap(reason=privacy_excluded)` spanning the window + a supervision
`erasure` row (counts only — never erased content).

## Redaction tiers

| Tier | Where | Masks |
|---|---|---|
| `secrets` | everywhere (stores, logs, egress) | API keys, tokens, private keys, cards, SSNs, credential assignments |
| `log` / `egress` | distill/telemetry + anything leaving the machine | secrets **+** email + phone |

**Product decision (criterion 6 deviation, signed):** contact emails/phones
are kept in local stores and embeddings — contact memory is the product —
and masked only in logs / cloud payloads. Secrets (`sk-ant-…`, keys, etc.)
are still stripped before store, embed, log, and egress. A strict reading of
criterion 6 ("no embedding" of email/phone) would require dropping contact
memory; we deliberately do not.

Secret-shaped OCR ⇒ prefer **skip the model call** over redact-then-send.

## Known limitations / open gaps

| Item | Status |
|---|---|
| Criterion 9 (CPU ≤3% / RSS ≤400 MB / OCR p95 ≤1.5 s) with audio running | Measure with `python scripts/bench_perception_overhead.py --pid <app> --seconds 120` (live) or `--synthetic --audio-load` (contention approx). **Required before flipping `QUILL_PERCEPTION_L1=1` for real.** |
| `browser_url` | Always `url_unavailable` until UIA URL read + registrable-domain validation lands — banking-domain gate and `dominant_domain` segmentation stay inert |
| Windows.Media.Ocr confidence | Hardcoded `1.0` (API does not expose it) — `ocr_mean_conf` / `dropped_low_conf` are decorative with the default engine |
| Test-plan harnesses | Synthetic-session ground-truth triggers, kill-mid-enrichment chaos, and 30-day storage soak scripts are **not** shipped; spirit covered piecewise by unit tests |
| CAS shared SHA | Compactor + erasure use `sha_refcount` before unlink; CAS tree is never mtime-swept on erase |

## Package map (`app/perception/`)

| Module | Role | Phase |
|---|---|---|
| `schemas.py` | Pydantic records, ULID, `schema_version` | A |
| `store.py` | `perception.db`, WAL, migrations, OCR write/reconstruct | A/B |
| `redaction.py` | Two-tier redactor over `services/redact.py` | A |
| `privacy_gate.py` | Pre-pixel rules + user blocklist + excluded captures | A |
| `spend_cap.py` | USD/day ledger + allow/check/record | A |
| `l0_meta.py` | 1 Hz monitor, gaps (sleep/pause/process_down) | A |
| `erasure.py` | Cascading erase job (incl. `ocr_blocks`) | A/B |
| `dhash.py` | 64-bit perceptual hash | B |
| `ocr.py` | Windows.Media.Ocr engine interface | B |
| `ocr_blocks.py` | LanceDB table keyed by `capture_id` | B |
| `l1_capture.py` | Change-triggered OCR loop + Event emit | B |
| `l2_frames.py` | CAS WebP put/path/unlink | C |
| `compactor.py` | Age + disk-budget compaction | C |
| `l3_workers.py` | L3 segment / extract / VLM fallback / cutover helpers | D |
| `export_parquet.py` | Incremental Parquet export + watermarks | D |

## API summary

| Method | Path | Purpose |
|---|---|---|
| GET | `/perception/status` | Live indicator + spend + coverage |
| GET | `/perception/recent` | Last N minutes of capture |
| POST | `/perception/erase` | Cascading erasure |
| GET/POST/DELETE | `/perception/blocklist` | Privacy rules |
| POST | `/perception/pin` | Promote capture (keep full frame) |
| POST | `/perception/unpin` | Clear promotion |
| POST | `/perception/compact` | Run L2 age/budget compactor now |

Screen consent/pause remain on `/capture/*`; L0 rides that plumbing.

## Tests

- `tests/test_perception_phase_a.py` — gap coverage, pre-pixel privacy,
  `sk-ant-…` log regression, spend-cap, erasure, crash/WAL, endpoints
- `tests/test_perception_phase_b.py` — L1 OCR, dHash, reconstruct, cutover
- `tests/test_perception_phase_c.py` — CAS put, L1+L2 wire, privacy no
  pixels, age/budget compact order, pin exemption
- `tests/test_perception_phase_d.py` — extract idempotency, segment blocks,
  L3⇔screen_extract cutover, Parquet incremental, VLM skip mocks
- `scripts/bench_perception_overhead.py` — criterion 9 CPU/RSS/OCR latency
  (measure with audio contention before enabling L1)

## Config knobs

| Env | Default | Notes |
|---|---|---|
| `QUILL_PERCEPTION` | `1` | Master enable for L0 |
| `QUILL_PERCEPTION_L1` | `0` | Flip to `1` to make L1 the screen producer |
| `QUILL_PERCEPTION_L2` | `1` | CAS frame writes on L1 captures (kill switch) |
| `QUILL_PERCEPTION_L3` | `0` | Flip to `1` for L3 jobs; disables `screen_extract` |
| `QUILL_PERCEPTION_L3_IDLE_S` | `300` | Idle gap ending an activity block |
| `QUILL_PERCEPTION_L3_SWITCH_S` | `180` | App-switch gap ending a block |
| `QUILL_PERCEPTION_L3_VLM_CHARS` | `40` | OCR length below which VLM fallback may run |
| `QUILL_PERCEPTION_EXPORT_DIR` | `export` | Parquet root (`export/<table>/date=…`) |
| `QUILL_PERCEPTION_POLL_S` | `1.0` | L0 poll cadence |
| `QUILL_PERCEPTION_DEBOUNCE_MS` | `500` | State-change debounce |
| `QUILL_PERCEPTION_HEARTBEAT_S` | `60` | Liveness emit |
| `QUILL_PERCEPTION_BATCH_S` | `2.0` | WAL commit batch |
| `QUILL_PERCEPTION_IDLE_S` | `60` | Idle flag threshold |
| `QUILL_PERCEPTION_GAP_S` | `5.0` | Wall-clock jump → sleep gap |
| `QUILL_PERCEPTION_L1_SETTLE_MS` | `700` | Post-change settle before OCR |
| `QUILL_PERCEPTION_L1_DHASH_S` | `5` | Perceptual-hash poll |
| `QUILL_PERCEPTION_L1_HAMMING` | `10` | dHash trigger threshold |
| `QUILL_PERCEPTION_L1_MAX_S` | `120` | Max interval fallback |
| `QUILL_PERCEPTION_L1_MIN_CONF` | `0.55` | Drop low-confidence OCR lines |
| `QUILL_PERCEPTION_THUMB_PX` | `960` | Thumbnail long edge |
| `QUILL_PERCEPTION_THUMB_Q` | `55` | Thumbnail WebP quality |
| `QUILL_PERCEPTION_FULL_TTL_H` | `72` | Unpromoted full-frame TTL |
| `QUILL_PERCEPTION_THUMB_TTL_D` | `30` | Thumbnail TTL |
| `QUILL_PERCEPTION_DISK_GB_YEAR` | `15` | Frames-tree disk budget |
| `QUILL_CLOUD_BUDGET_USD_DAY` | `2.0` | Ambient cloud ceiling |
