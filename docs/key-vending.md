# Onboarding the model account (WS-D)

Every tester needs Sparrow talking to a frontier model. How they get there is a
funnel decision, not a technical one, so the tiers below are ordered by how much
the operator absorbs on the tester's behalf — and the higher tiers are strictly
more operator liability, not more product.

**Shipped today: Tier 1.** Tier 2 is designed here and deliberately unbuilt.

---

## Tier 0 — shepherded install (no code)

The operator provisions a key on a founder-controlled Anthropic org during an
install call and the tester pastes it as before. Zero engineering, works to
about ten testers, and it is the fallback if everything else slips.

## Tier 1 — invite-code vending *(shipped)*

The operator pre-creates one Anthropic **workspace key per tester** — revocable
individually, budgetable individually — and mints a single-use invite code for
each. `install.bat` offers the code path first; redemption writes the vended key
into that machine's `.credentials.env`, exactly where a pasted key goes.

    tester types ABCD-EFGH-JKLM
      -> POST {QUILL_INVITE_URL} {"code": "..."}
      -> {"provider": "anthropic", "key": "sk-ant-...", "label": "Dana"}
      -> parent_model.save(provider, key)   # the same call the paste path makes

Pieces:

| what | where |
|---|---|
| client redemption + error copy | `app/services/invite.py` |
| in-app route (Setup page) | `POST /onboarding/invite` |
| installer branch | `scripts/install.ps1` (only when `QUILL_INVITE_URL` is set) |
| operator service + CLI | `scripts/invite_service.py` |
| tests | `tests/test_invite.py` |

Operator flow:

```bash
# one key per tester, created in the Anthropic console, then:
python scripts/invite_service.py mint sk-ant-... --label "Dana (Capital Connect)" --days 30
# ABCD-EFGH-JKLM      <- send this, not the key

QUILL_INVITE_DB=./invites.json uvicorn scripts.invite_service:app --port 8090

python scripts/invite_service.py list
python scripts/invite_service.py revoke ABCD-EFGH-JKLM   # one tester, not the cohort
python scripts/invite_service.py reissue ABCD-EFGH-JKLM  # tester is reinstalling
```

**Why this tier and not the next one.** The key still lives in the tester's own
`.credentials.env` and their calls go straight to Anthropic. After install the
operator's service is never contacted again, so it can be down, moved, or
deleted with no effect on any running install. That is what makes Tier 1
reversible and what keeps the local-first story true.

Known costs, stated plainly:

* The operator holds keys in a JSON file and mails codes. Fine for ~10 testers,
  not a thing to grow.
* A code is single-use, so a tester who reinstalls needs `reissue`. That is a
  phone call, and the alternative (multi-use codes) is a key leak waiting to
  happen.
* Per-tester spend is capped by the Anthropic workspace budget and by the local
  `QUILL_CLOUD_BUDGET_USD_DAY` (default **$2/day**, unchanged).

---

## Tier 2 — hosted proxy *(designed, NOT built)*

One founder-held real key behind an Anthropic-API-compatible proxy; each tester
gets a bearer token instead of a key, and per-token daily USD budgets are
enforced server-side mirroring `app/perception/spend_cap.py`.

**Build this only if the hosted-cloud testing path is confirmed.** It adds an
availability dependency that Tier 1 does not have: *proxy down = cloud tier down
for every tester simultaneously*, during a pilot whose whole purpose is
measuring whether people keep using the thing. If it is built, it ships with a
client-side fallback to a BYO key so an outage degrades one tier instead of
stopping the app.

### Client change

The `anthropic` SDK honors `base_url`, so the client side is a base-URL override
at the four construction sites (`grep -rn "anthropic.Anthropic("`):

* `app/services/extractor.py`
* the vision path (`ClaudeVLM`)
* `app/services/model_router.py`
* the agent bridge

Rather than four edits, route them through one `_ensure_client` helper that
reads `ANTHROPIC_BASE_URL`; then Tier 2 is a config change, and the audit is a
single grep that must return one construction site.

### Token / budget schema

```sql
-- operator side only; never on a tester machine
proxy_tokens(
  token_hash    TEXT PRIMARY KEY,   -- sha256; the raw token is shown once
  label         TEXT,               -- operator note, e.g. "Dana (Capital Connect)"
  created_at    REAL NOT NULL,
  revoked_at    REAL,
  budget_usd_day REAL NOT NULL DEFAULT 2.0   -- mirrors QUILL_CLOUD_BUDGET_USD_DAY
)

proxy_spend(
  token_hash  TEXT NOT NULL,
  day         TEXT NOT NULL,        -- UTC YYYY-MM-DD, same convention as usage_daily
  usd         REAL NOT NULL DEFAULT 0,
  calls       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (token_hash, day)
)
```

Semantics copied from `spend_cap.py` so the two cannot disagree: cost is
estimated per call from token counts and the model price table, the day rolls at
UTC midnight, and exhaustion is a refusal rather than a throttle. A budget-
exhausted proxy response must map to the existing `BudgetExhausted` UX — the
tester sees the same "daily cloud budget reached" message they would see with
their own key, never a stack trace or an opaque 402.

Content: the proxy sees every prompt. That is a different privacy posture from
Tiers 0 and 1 and must be stated in TESTER_SETUP before anyone is put on it —
not buried in a changelog.
