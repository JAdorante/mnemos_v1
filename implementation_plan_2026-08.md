# Mnemos v1 — Implementation Plan (August 2026)

Source: the repository-grounded Functional Improvement Plan (PDF, audited @ `3ca0f57`),
re-verified against the working tree on 2026-08-03 and merged with the July 23/28
security audits. Every task cites the file it touches; anything the PDF got wrong or
that has shipped since is corrected here.

---

## 0. Verification results — what changed since the PDF

**Confirmed defects (all still present):**

| Defect | Where |
|---|---|
| Claim text/span conflation (`source_span or text`) | `app/storage.py:3014` (`add_claim`) |
| Speaker never reaches extraction (`_extract_text(text)`) | `app/services/extractor.py:221` |
| Approval = free-text regex + autonomous bypass | `browser_agent/orchestrator.py:307,658,696` |
| No `payload_hash` / `expires_at` on packets | `app/storage.py:391` (`action_packets`) |
| Commitments have `status` only — no state machine | `app/storage.py:229` |
| Jobs: `attempts` but no max/backoff/dead-letter | `app/storage.py:248` |
| No `fact_candidates`, no `correlation_id`, no `privacy_class` | absent repo-wide |
| No golden datasets (`tests/fixtures/goldens/` doesn't exist) | absent |
| Raw LAN token stored in the session cookie (30 days) | `app/api/routes.py:3937–3944` |
| Live secrets on disk in `.env` (Anthropic key, Google key, LAN token) — gitignored, NOT in git history, but ships in any zip/folder copy; the Anthropic key already leaked once (July 28 audit) and is still unrotated | `.env`, `.gitignore:4` |

**PDF claims that are stale — do NOT re-do these:**

1. **"Dark subsystems" is inverted on this machine.** `.env` sets `QUILL_PLANNER=1`,
   `QUILL_PEOPLE_V2=1`, `QUILL_TEXT_LOCAL=1`, `QUILL_FIELD_V2=1`, `QUILL_IDLE_TRAIN=1`
   while the *code* defaults stay `0`. The graduation problem is real but reversed:
   uncalibrated systems (People v2 thresholds are literally commented "calibrate later
   on bench", `people_pipeline.py:31`) are already live here, while a fresh install
   gets the off-defaults. Consequence: **golden sets move earlier** (they now gate
   keeping things on, not turning them on), and `.env` itself is a shipping artifact
   to fix.
2. **`decompose()` is no longer stubbed** — it routes through the MultiTask splitter
   (`agent_planner.py:637`). PDF task 30's "un-stub decompose" is done; only the
   planner *eval + code-default flip* remains. `QUILL_PLANNER_CORE=1` is already the
   code default for core workflows.
3. **Ambient spend cap exists** — `app/perception/spend_cap.py` ($2/day default,
   enforced at VLMRouter + ModelRouter seams). PDF/audit "no spend cap" is fixed.
4. **Secret egress redaction exists** — `app/services/redact.py` (3 layers: capture
   title-deny, VLM escalation skip, distill-log scrub). What does NOT exist is a
   general `privacy_class` gate at the `model_router` text seam — that item stands.
5. **Action-readiness score exists** (`app/services/readiness.py`) — risk-aware
   auto/offer/review/hold bands. Approval binding should consume it, not duplicate it.
6. `REQUIRE_APPROVAL = True` is the shipped default (`browser_agent/config.py:129`) —
   the gate holds in default posture; the fix needed is *enforceability* (binding),
   not the default.

---

## Phase 0 — Stop incorrect behavior + secure the tree (week 1)

The claim-corruption fix, the approval-binding mechanism, and the committed secrets.
All additive columns/flags; rollback = flag off.

| # | Task | Files | Acceptance criterion | Size |
|---|---|---|---|---|
| 0.1 | **Rotate all three secrets** (Anthropic key — leaked July 28 and never rotated — Google key, LAN token); ship `.env.example` with safe-default posture and exclude `.env` + `data/` from any zip/packaging path (`.env` is already gitignored — history is clean; the risk is folder-copy distribution). Delete the pre-purge `data/*.bak` files that still hold old secrets | `.env`, packaging, `data/` | Rotated keys; no live secret in any distributable artifact | S |
| 0.2 | **Claim text/span fix** — add nullable `facts.text` (guarded ALTER, `entities.hidden` pattern); stop `source_span or text` substitution; empty span ⇒ gate **drop** with reason; backfill `text` from span | `app/storage.py:3006`, `app/services/extractor.py`, `fact_gate.py` | Span always verbatim; `facts.text` populated; drop-with-reason on empty span | S |
| 0.3 | **Packet hash + expiry columns** — `payload_hash`, `expires_at` (default 15 min), `approved_at`, `approved_via` (`button\|typed`), `executed_hash`; hash = sha256 of canonicalized executable args (`fields_json`) at `record_packet` time | `app/storage.py:391`, `app/services/agent_log.py` | Every new packet has hash + expiry | S |
| 0.4 | **Enforce at browser commit gate** — immediately before the commit action (`_looks_irreversible` branch), re-canonicalize about-to-execute args; require `hash == payload_hash` and `now < expires_at`; mismatch ⇒ hard stop + re-ask with diff. Ship behind `QUILL_APPROVAL_BIND` in shadow-log mode for 1 week, then default-on | `browser_agent/orchestrator.py:652` | Drift test fails closed; shadow week shows zero false blocks | M |
| 0.5 | **Enforce at desktop mutating gate** — same check | `desktop_agent/driver.py` | Same | S |
| 0.6 | **Approve/Cancel/Edit buttons** POSTing `{packet_id, payload_hash}`; typed "approve" resolves to the *pending packet id*; negation regex still wins; `edit` mints a new packet (new hash) | `approval_partial.py`, `app/api/routes.py` (`POST /approval/{packet_id}/decide`) | Free text alone can no longer authorize a stale/drifted packet | M |
| 0.7 | **Autonomous mode bypasses the ask, never the policy** — route the desktop guard through `RISK_TABLE` so there is one policy source; delete/remove blocked everywhere incl. autonomous | `desktop_agent/driver.py`, `app/services/agent_planner.py` | One policy source; blocked classes blocked in all modes | S |
| 0.8 | **Duplicate-send refusal** — before any send-class commit, refuse if a verified same-`executed_hash` send exists in the last hour | `browser_agent/orchestrator.py` | Same-hash send within 1h refused | S |
| 0.9 | **Extract retry cap** — 3 failures ⇒ turn parked `failed`; no nudge spin | `app/services/extractor.py:595`, `app/storage.py` | Poisoned turn can't loop | S |
| 0.10 | **Jobs dead-letter** — `max_attempts=5`, backoff, `dead` status + console view | `app/services/worker.py`, `app/storage.py:248` | Poisoned job visible, not looping | S |
| 0.11 | **`tests/test_approval_binding.py`** — adversarial suite: recipient/price/attachment drift post-approval, expiry, duplicate send, autonomous-mode policy block | `tests/` | Green; drift cases enumerated; runs in CI | M |

## Phase 1 — Evidence & replayability (weeks 2–3)

| # | Task | Files | AC | Size |
|---|---|---|---|---|
| 1.1 | **`fact_candidates` table + writer** (schema per PDF §7: `turn_hash`, `kind`, `payload_json`, `source_span`, `speaker`, `assertion`, `confidence`, `model`, `prompt_version`, `schema_version`, `status`, `verdict_reason`) | `app/storage.py`, `app/services/extractor.py` (`_persist`) | Every LLM output row lands as a candidate with `prompt_version` | M |
| 1.2 | **Materialize accepted candidates → facts** (no behavior change); dedupe on `turn_hash+kind+payload` | `extractor.py` | Fact counts identical on a replay corpus | M |
| 1.3 | **Assertion classes** — `assertion` enum in the extraction JSON schema (`stated_by_user\|stated_by_other\|inferred\|quoted\|hypothetical`); quoted/hypothetical are never auto-accepted (gate → review); adversarial fixtures | `extractor.py` (`_SCHEMA`, `_SYSTEM`), `fact_gate.py`, `tests/` | Quoted/hypothetical fixtures never auto-accept | M |
| 1.4 | **`validators.py`** — deterministic price/email/phone/URL/temporal validation (reuse `person_details._EMAIL`, `_phone_ok`); failure = dropped-with-reason, never an exception | new `app/services/validators.py`, `fact_gate.py` | Bad email/phone/price/temporal ⇒ dropped with reason | S |
| 1.5 | **`correlation_id`** stamped at capture (`Event.meta`), propagated into `fact_candidates`, `agent_runs` | `app/events.py`, `extractor.py`, `agent_log.py` | Trace query returns the full capture→verify chain | M |
| 1.6 | **`/console/trace/{id}`** page | `app/api/routes.py` | Chain rendered end-to-end | M |

## Phase 2 — Calibrate what's already live (weeks 3–5)

Reordered vs the PDF: People v2 and the planner are already ON via `.env`, so the
goldens gate *keeping* them on. Store goldens as `tests/fixtures/goldens/*.jsonl`
(ranking-goldens pattern); wire a `make eval` CI target with thresholds — any
prompt/`EXTRACTOR_MODEL`/schema change fails CI below threshold.

| # | Task | Files | AC | Size |
|---|---|---|---|---|
| 2.1 | **Speaker-labeled extraction** — `_extract_text(turn)`; render `[<speaker or 'unknown speaker'>]: <text>`; `owner='me'` valid only when speaker is the enrolled user; ownership resolved relative to the labeled speaker | `extractor.py:221`, `consolidation.py` | Ownership eval ≥ target on 2-speaker fixtures | M |
| 2.2 | **Golden set #1: commitments/ownership** — 150 turns incl. quoted/negated/hypothetical/two-speaker; P/R/F1 + ownership accuracy; precision ≥ 0.9 keeps auto-insert; 2-speaker ownership errors < 5% before trusting `from='me'` | `scripts/`, fixtures, CI | Thresholds wired into `make eval` | L |
| 2.3 | **Golden set #2: entity resolution** + threshold sweep for `_AUTO_RESOLVE/_AUTO_MARGIN/_CREATE_NEW` (`people_pipeline.py:31–34`). **Decision gate:** if merge-error ≉ 0 at current thresholds, set `QUILL_PEOPLE_V2=0` in `.env` until calibrated; when green, flip the *code* default to 1 and keep legacy resolver documented as fallback for one release | `scripts/`, `people_pipeline.py` | Merge-error ≈ 0 at chosen thresholds; code default flipped only on green | L |
| 2.4 | **Contact-attribution fixtures** (5 mandate sentences + 50) + attribution write-path decision; assert `source_policy` mint-deny for "the article mentioned…" | `tests/test_people_pipeline.py`, `people_pipeline.py` | Misattribution ≈ 0; above it, route to review not auto-write | M |
| 2.5 | **Structured claims → `kg_beliefs`** — optional `subject/predicate/object/speaker_is_source` on claims; parseable claims become beliefs with speaker-attributed evidence + `source_class`; unparseable stay flat facts; simultaneous money/date conflicts ⇒ `kg_adjudications kind='conflict_flag'` review card, never silent overwrite | `extractor.py` schema, `kg_beliefs.py` | "David said $49" queryable by speaker; conflict card on $49 vs $55 | L |
| 2.6 | **KG v2 read cutover** after 7 clean parity reports (`kg_parity.py` gate exists) | `kg_parity.py`, graph readers, `grounding.py` | Constellation + grounding read beliefs | M |

## Phase 3 — Retrieval usefulness (weeks 5–6)

| # | Task | Files | AC | Size |
|---|---|---|---|---|
| 3.1 | **Working-context param** — `compose(question, ctx=working_memory.current())` boosting active-project/person facts | `grounding.py`, `working_memory.py` | Active-person queries boosted; session_context tests extended | M |
| 3.2 | **Deterministic answer-check** — every name/date/price token in the answer must appear in the context block; LLM entailment only for money/date/commitment answers; failure downgrades to "here's what I found, with the evidence"; expose `confirmed/likely/conflicting/missing` sections | new module + `response_compiler` | Fabricated-token test downgrades the answer | M |
| 3.3 | **Query-type routes** — "what did X tell me" → belief store filtered by evidence speaker; "what changed since last week" → `field_history`/reflections diff; regex-first, LLM only on no-match (`model_router` task `query_route`, local-eligible) | `grounding.py` | Golden #4 grounding rate ↑ | M |
| 3.4 | **Evidence playback (F2)** — fact card → "play the moment": fact→event→WAV + span highlight; repo already has `audio_paths`, enhanced clips, `/graph/constellation/evidence` | `routes.py` + memory page JS | Any surfaced memory can play its evidence | M |

## Phase 4 — Commitments dependable (weeks 6–8)

| # | Task | Files | AC | Size |
|---|---|---|---|---|
| 4.1 | **State machine** — additive `commitments.state` (`detected…superseded`), `completion_evidence_json`, `last_surfaced`, `counterparty_expects` + `commitment_transitions` table; keep `status` as a derived compat view so `list_facts(status='open')` callers (`grounding.py:165`, `horizon.py:114`, `meta_memory.py:33`, `reflector.py:162`) are untouched | `app/storage.py`, `routes.py` | Transition-legality tests; status view back-compat | M |
| 4.2 | **Completion candidates** — (a) user statement: `resolves_commitment` hint on "sent/done" + cosine-near open commitment ⇒ offer, never auto-complete; (b) agent: verified send step whose packet `source_fact_ids` include the commitment ⇒ `completed` with `evidence_event_id`; (c) screen Sent-toast/folder OCR ⇒ candidate. A generated plan must never complete anything — transitions require cited evidence | `extractor.py`, `orchestrator.py` hook | Plan-only never completes; verified send does | L |
| 4.3 | **Open-loop engine** — new `app/services/open_loops.py`, deterministic detectors first: my overdue commitments (waiting-on-me), others' overdue to me (waiting-on-them), unanswered questions (extraction gains a `questions` array), pending agent asks; surface via `horizon.refresh()` + Today page; snooze via `last_surfaced` + existing offer-defer machinery | new service, `horizon.py` | Waiting-on-them loop appears with evidence; snooze respected; metric = user dismiss rate (precision-first) | L |

## Phase 5 — Agents trustworthy (weeks 8–10)

| # | Task | Files | AC | Size |
|---|---|---|---|---|
| 5.1 | **Evidence-anchored verification registry** — per-tool read-back: email via Sent-folder/mail query, calendar via event-id read-back, file via `os.stat`; add `outcome_uncertain` to `agent_steps.status` and make it the default when only the LLM judge ran | `orchestrator.py`, `driver.py`, `storage.py` | Send verified via Sent folder, not LLM opinion | L |
| 5.2 | **Planner graduation** — `decompose()` already live via MultiTask; remaining: assert multi-step goal compiles ≥2 packets each pre-persisted, planner eval, then flip `QUILL_PLANNER` *code* default once approval binding (0.4) is default-on | `agent_planner.py`, `agent_bridge.py`, tests | Eval green; code default flipped after binding | M |

## Phase 6 — Privacy egress + web hardening (S/M items, interleave from week 2)

| # | Task | Files | AC | Size |
|---|---|---|---|---|
| 6.1 | **`privacy_class` on events** (public/internal/personal/sensitive/never-send), stamped deterministically (excluded apps, `_looks_like_secret` patterns, health/finance keywords), **enforced in `model_router` before any remote call** (redact or refuse) — complements the existing 3-layer `redact.py` | `app/events.py`, `model_router.py` | No `sensitive`+ content reaches a cloud call unredacted | M |
| 6.2 | **`model_log.privacy_max`** — per-external-call record of highest class sent; console "what left the machine" view | `model_log.py`, console | Auditable egress inventory | S |
| 6.3 | **Session cookie hardening** — store a derived session token (HMAC of the API token + salt), not the raw token; keep HttpOnly/SameSite | `app/api/routes.py:3937`, `api_auth.py` | Cookie theft no longer yields the LAN token | S |
| 6.4 | **`exec_webapp` CSRF protection** — origin check / CSRF token on state-changing endpoints (July 28 audit P1) | `routes.py`, `api_auth.py` | Cross-origin POST rejected | M |
| 6.5 | **Prompt-injection fixtures** — hidden instructions in pages/documents into `tests/test_ghost_browser.py` + planner-input fixtures; assert the hash gate (0.4) stops argument drift — the binding IS the defense | `tests/` | Injection cases green | M |
| 6.6 | **`vector_gc`** — periodic job dropping Lance rows whose store row is dismissed/superseded >30d; assert erasing an event marks facts `evidence_removed`; add periodic Lance `optimize()` (pending from the 107 GB incident) | `memory.py`, `vectorstore.py`, `tests/test_memory_economy.py` | Deletion propagates; version bloat bounded | S |

---

## Sequencing rationale

1. **Phase 0 first, nothing before it** — the claim conflation corrupts data daily,
   the approval gap undermines every agent feature, and the committed keys are a
   standing exposure. All items are S/M and independently revertible.
2. **Candidates (Phase 1) before goldens (Phase 2)** — replay through
   `fact_candidates` is what makes golden-set iteration cheap (diff candidates, not
   memory).
3. **Calibration before any further default flips** — People v2 runs uncalibrated on
   this machine today; golden #2 either validates the live thresholds or turns the
   flag back off. No new flag flips (planner code default, KG cutover, Field v2)
   until their gates are green.
4. **Commitments/open-loops (Phase 4) after speaker labeling (2.1)** — ownership must
   be trustworthy before loop detection is built on it.
5. **Phase 6 interleaves** — items are small, independent, and several close July-28
   audit P1s.

## Explicitly NOT in scope (per PDF §23.23 + standing invariants)

No rewrite; no Postgres; no LoRA-by-default (gate on `predictor_bench` beating
prompted-local); no new capture modalities; no peer expansion beyond the current
`peer_channel` (TLS required before LAN anyway); code stays general-purpose —
user-specificity lives in data/model conditioning, never in `.py` logic.

## Definition of done (trust criteria)

- Ownership accuracy ≥ 95% on two-speaker goldens; merge-error ≈ 0; contact
  misattribution ≈ 0; grounding rate at target on golden #4.
- `test_approval_binding.py` green in CI; unauthorized-action rate 0; drift fails
  closed; duplicate sends refused.
- Completion never fires from a plan; every completion transition cites evidence.
- Every surfaced memory can play its evidence (F2).
- No live secret in the tree; keys rotated; cookie carries a derived token; ambient
  cloud spend capped (already true) and egress auditable via `privacy_max`.
