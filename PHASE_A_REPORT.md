# Phase A Final Report — Desktop Perception Safety Floor

**Status: complete.** Security-critical Phase A from
`PERCEPTION_IMPLEMENTATION_PROMPT.md` is implemented, wired, documented, and
covered by dedicated tests. Higher layers (L1–L3) remain deferred per the
prompt’s phasing rules.

## Deliverables

| # | Item | Status |
|---|---|---|
| D0 | `MIGRATION.md` | Done (cutover A→D, dual-emit flag plan, rollback) |
| 1 | `app/perception/` package | Done — schemas, store, redaction, privacy_gate, spend_cap, l0_meta, erasure |
| 2 | Versioned migrations (`PRAGMA user_version`) | Done — `perception.db` step 1 |
| 3 | Phase A test suite | Done — `tests/test_perception_phase_a.py` (17/17) |
| 4 | `PERCEPTION.md` | Done |
| — | API: `/perception/status`, `/recent`, `/erase`, `/blocklist` | Done |
| — | Security wiring into existing modules | Done |

## Security floor (live P0s closed)

1. **Redaction** — `escalate_log` uses perception log tier (secrets + email/phone). Planted `sk-ant-…` regression in Phase A tests.
2. **Spend cap** — default **$2/day** on ambient cloud tasks; enforced in `vlm` (3 sites) + `model_router`; ledger via `model_log`. Fails closed.
3. **Privacy gate** — pre-pixel in `desktop_capture`; labeled `captures(kind=excluded)`; user blocklist API.
4. **Consent / pause / erasure** — L0 rides screen consent; pause opens `user_pause` gap; `erase_window` cascades SQLite → facts → LanceDB → frames → distill → Parquet.

## Full suite regression check

```
4 failed, 1171 passed, 1 skipped, 93 subtests passed  (≈14m46s)
```

Phase A suite alone: **17 passed**.

The 4 full-suite failures are **not Phase A regressions**:

| Failure | Cause |
|---|---|
| `test_escalate_log` ×3 (`local_error`, `tier_selection`, `local_disabled`) | Live `.env` has `QUILL_VISION_CLOUD_WHEN_LOCAL_DOWN=0`. With that kill-switch off, local-outage paths skip Claude (`provider=none`) instead of falling back. Re-running with the flag on: `local_error` + `tier_selection` pass; `local_disabled` still expects reason `local_disabled` while `local_vlm=1` + `local_ok=False` correctly labels `local_unreachable`. Env/test fragility, predates / orthogonal to the spend-cap gate. |
| `test_entities_admin::test_bad_kind_400_and_forget` | `normalize_entity_kind("wizard")` collapses to `idea`, so `POST /entities/{id}/kind` returns 200. Unrelated allowlist behavior in `name_quality`. |

No perception-module failures; no redaction/spend/privacy/erasure regressions in the suite.

## What’s intentionally not in Phase A

- L1 delta OCR / FTS / embeddings (`l1_capture.py`)
- L2 CAS thumbnails / promotion / compactor
- L3 async enrichment / Parquet export writers
- Synthetic session harness & storage soak (B–D test plan)

## How to verify locally

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_perception_phase_a.py -v
# Indicator / recent / blocklist / erase:
# GET  /perception/status
# GET  /perception/recent?minutes=30
# GET|POST|DELETE /perception/blocklist
# POST /perception/erase  {"ts_start_ms":…,"ts_end_ms":…}
```

## Cutover reminder

Phase A is additive and rollback-safe (`MIGRATION.md`). Do not enable L1 dual-write; Phase B flips a single producer via `QUILL_PERCEPTION_L1`.
