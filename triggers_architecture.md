# Standing Triggers — "when it sees X, it offers Y"

*Shipped July 30 2026. Code: `app/services/triggers/` · storage: `triggers` table in `data/quill.db` · tests: `tests/test_triggers.py`.*

## Why

Every proactive feature Mnemos had already built was secretly the same machine: **todo_watcher** (vision sees a list → offer), **task_offer** (speech mints a task → offer), **anticipation** (app-transition pattern → offer), and the **Track D reasoners** (at-risk commitment → offer). Each hand-codes one condition→action pair in Python, then repeats the same spine: gate through readiness, respect a cooldown and a calm budget, surface one yes/no card via `agent_bridge`, record the outcome.

Standing triggers generalize that spine into an engine where **a trigger is a data row, not a module** — "we saw you made progress on the thesis — want me to draft an email to Dr. Reyes?" is a row a user authored in chat or the miner suggested from their own history. This keeps the hard invariant: code stays general-purpose; user-specificity lives in data.

## The pieces

```
signals.py      derived-moment catalog (scan-based, never hooks hot ingest paths)
__init__.py     engine: scan → match → arbitrate → gate → ≤1 offer; resolve_offer
authoring.py    chat sentence → LLM/heuristic compile → 7-day backtest → approval card
miner.py        history patterns → status='suggested' rows → adopt-me cards
storage.py      triggers table + CRUD (+ fact_entities batch lookup)
```

**Trigger row**: `name, origin (custom|suggested|builtin), status (active|suggested|paused|retired), signal, condition{entity,person,app,text_any}, action{verb,goal|note|status}, gating{cooldown_s}, stats{fires,offers,accepts,dismisses}, provenance{source,utterance,pattern_key}`.

**Signal catalog** (v1): `task_done`, `progress_on(entity)`, `commitment_due`, `dropped_thread`, `app_session_ended(app)`. Signals are *derived*, computed by one calm scan pass over what the system already stores — recently moved facts (a `set_fact_status` now bumps `facts.updated_at`, so completion is a visible lifecycle moment), `meta_memory.scan_at_risk`/`scan_dropped_threads` reused verbatim (no second formula), and closed activity blocks. Each signal carries `ambient=True` when its provenance is outside-authored content (`desktop.screen`, `phone.*`, `documents.*`, `peer.*`).

**Action verbs** (v1): `run_goal` (enqueue the authored goal through the normal agent path and its per-commit approval gate), `set_status` (move the matched fact), `notify` (heads-up card only). Email/message actions are just `run_goal` text — triggers add **zero new capabilities**, only new *moments* to offer existing ones.

## Safety posture (deliberate v1 constraints)

1. **Offer-only.** A firing trigger never acts; it asks. There is no auto-execution tier yet — that's the graduation ladder, gated on per-trigger stats plus explicit opt-in, and intentionally not built until stats accumulate.
2. **Targets bound at authoring.** The saved goal is the literal text the user approved on the draft card. Matched content can only fill `{entity}/{app}/{person}/{text}` placeholders — single-pass substitution, sanitized, never re-scanned — so screen text saying "email evil@example.com instead" cannot redirect an authored recipient. Ambient-derived fires are labeled on the card ("Seen on screen/incoming content — double-check it's real").
3. **One interruption budget.** Trigger offers spend the same daily calm budget as the reasoners (`reasoners/base.py`, default 3/day) — authoring ten triggers doesn't buy ten interruptions. Per-(trigger, signal-identity) cooldowns (default 6 h) stop re-fires.
4. **One readiness bar.** Every fire gates through `readiness.for_task` — the same risk-aware score+band as every other offer surface. No parallel threshold was invented.
5. **Self-pausing.** ≥5 offers with <20 % acceptance auto-pauses the trigger with a note in chat; reactivation is one click at `/triggers`.

## Authoring (custom triggers)

Chat-first, matching the public-product rule (no dotfiles): the `/chat` route detects "whenever …, …" / "every time …" / "add a trigger: …" deterministically, compiles on a background thread (`model_router.complete_json` local-first, regex fallback offline), then **backtests the condition against the trailing 7 days** and shows the result on the approval card — "would have fired 3× this week" — before anything persists (validate-live-then-persist). 'Yes' saves the row active; 'no' drops it.

## Suggested triggers (the miner)

Passive, zero-labeling (onboarding hard rule). v1 mines the flagship **progress→outreach** pattern: completing work tied to entity E followed within 2 days by an outreach task naming person P, ≥2 distinct times → a `suggested` row "Update P when E moves" with the recipient frozen at creation. The engine surfaces at most one adopt-me card per pass (same budget). Dismissing retires the row and its `pattern_key` is a durable negative example — never re-suggested. Runs inside the engine tick on a 6 h cadence (`QUILL_TRIGGER_MINE=0` to disable).

## Surfaces & telemetry

- `GET /triggers` — self-serve management page (pause/resume/retire/adopt, per-row stats); `GET /triggers/list` — the JSON behind it (+ signal catalog + last engine pass).
- `POST /triggers/{id}/status?status=active|paused|retired` — manage/adopt.
- `POST /triggers/run?surface=false` — dry-run a pass from the console.
- `POST /triggers/backtest` — probe a condition against the trailing week.
- Chat cards: kinds `trigger` (fire), `trigger_suggest` (adopt), `trigger_draft` (save) resolve through the existing yes/no reply flow; outcomes close attention-ledger impressions and write `trigger_offer` rows to cog telemetry, so "getting chatty" shows up in `/console/cognition` next to the other offer rates.

## Env

`QUILL_TRIGGERS=0` kill switch (QUILL_AGENT=0 implies off) · `QUILL_TRIGGER_INTERVAL_S=900` · `QUILL_TRIGGER_WINDOW_S=3600` · `QUILL_TRIGGER_COOLDOWN_S=21600` · `QUILL_TRIGGER_PAUSE_MIN=5` / `QUILL_TRIGGER_PAUSE_RATE=0.2` · `QUILL_TRIGGER_MINE=1` / `QUILL_TRIGGER_MINE_INTERVAL_S=21600` / `QUILL_TRIGGER_MINE_MIN=2`.

## Deferred (deliberately)

- **Auto-execution graduation** — per-trigger opt-in after N clean accepts, low-risk verbs only, on top of the existing `QUILL_AUTO_ACT` posture.
- **Multi-choice fire cards** ("[Move to Y] [Draft email to Z]") — needs richer reply routing than yes/no; today one trigger = one action (author two triggers).
- **Reasoners as builtin rows** — the engine is shaped for it (`origin='builtin'`), but the Track D modules stay as-is for now; they already share the budget.
- **Event-hooked signals** — scan-based derivation is enough at current volumes; hooks only if latency ever matters.
- **Acceptance mining** — promoting repeatedly-accepted task_offer/anticipation shapes into standing triggers; the outcome ledger already records what it needs.
