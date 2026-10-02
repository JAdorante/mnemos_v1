# Records layer

Sparrow produces an organization's records; it does not become the place
that holds them. Capture stays private and expiring on each node, facts
extracted from it become **claims**, and only claims a human approves become
**records** in the Org Record Service. Writing records back to CRM, Drive
and Notion is Phase 2.

| Tier | What | Where | Who sees it |
|---|---|---|---|
| 1 Capture | events, turns, audio, vectors | node SQLite + LanceDB | owner only, never crosses the wire |
| 2 Claim | structured assertion + evidence pointers | node `claims` | owner; scoped approvers once forwarded (pointers only) |
| 3 Record | approved claim, versioned, bi-temporal | Org Record Service (Postgres) | members with `read` on the scope |

## Node (`app/services/records/`)

| Module | Job |
|---|---|
| `canonical.py` | Canonical JSON, `canonical_hash`, `quote_hash`, `audit_entry_hash`. Standard library only, and imported by the service too, so both sides hash identically. |
| `claim_schemas.py` + `app/schemas/claims/*.json` | Versioned JSON Schema per kind (`decision`, `commitment`, `status`, `field_update`, `fact`). The small validator covers only the keywords those files use. |
| `claim_builder.py` | Turns accepted `fact_candidates` into `claims`. Every claim needs evidence (an event id plus the verbatim span). Handles dedup and conflicts, the personal-only gates, and scope suggestion. Optionally auto-proposes when confidence ≥ `QUILL_CLAIM_PROPOSE_MIN_CONF`. |
| `promotion.py` | Propose (mints a packet with a 7-day TTL), decide (approve, edit or reject; live session only), deliver, forward, discard, retry. Also the learning-loop verdicts and precision per kind. |
| `org_client.py` | Membership (`data/org_membership.json`, mode 0600), access token exchange, heartbeat caches (policy, scopes and grants, UI only), and the outbox drain. |
| `retention.py` | Policy (defaults, then env, then org; clamped), `expires_at` stamped at insert, and the expiry sweep with tombstones and receipts. |
| `node_store.py` | Data access for claims, packets, the hash-chained `node_audit_log`, and `records_outbox`. |
| `scheduler.py` | `records_tick` (every 10 min, only when `QUILL_RECORDS=1`) and `capture_expiry` (once per UTC day). |

The DDL lives in `Store._migrate_records`: `claims`, `promotion_packets`, `event_tombstones`, `node_audit_log` and `records_outbox`. It also adds the `events` columns `privacy_class`, `expires_at`, `hold_ids` and `consent_mode`, plus `kg_predicates.recorded_at` and `superseded_at`.

Routes are in `app/api/records_routes.py`; the review page is at `/records`.

### What counts as a live session

`promotion.decide` approves only when `source == LIVE_SESSION`. The HTTP layer decides that from the request itself, never from a body field. A request qualifies when all of these hold:

- It has no `Authorization` header. Bearer callers are scripts and agents.
- It sends the double-submit CSRF header, which only same-origin page JavaScript can produce.
- It is not cross-site, according to `Sec-Fetch-Site`.
- If an owner account exists (every hosted seat has one), it carries a valid account session cookie.

Any other source goes through `trust.source_can_authorize`, which returns False for every source (Invariant 3). Typed approval also requires the first 8 characters of the packet hash.

### Expiry

The mode comes from `QUILL_CAPTURE_EXPIRY`: `dry_run` (the default) writes receipts and deletes nothing, `enforce` deletes, and `off` skips the sweep. Deletion runs in dependency order:

1. vectors
2. audio and frame files, only inside the store's own directories
3. turns
4. `kg_evidence`; a predicate left with no evidence becomes `unsupported`
5. the `events` row, replaced by a tombstone

Claims keep their evidence pointer, marked `expired`. Rows with a non-empty `hold_ids` never expire. **Rows captured before the migration have `expires_at` NULL and never expire.** Turning enforcement on therefore cannot sweep a seat's existing history in one night. Backfilling old rows is a deliberate later decision.

Not covered yet: desktop `perception.db` rows and CAS frames. The perception erasure job owns those, and hosted seats have neither.

## Org Record Service (`org_coordinator/`)

- `repo/` is the only package that imports SQLAlchemy. `Database.tenant(org_id)` opens a transaction, runs `SET LOCAL ROLE sparrow_app`, and binds `app.org_id`. Every tenant table is under `FORCE ROW LEVEL SECURITY`, and a request that forgot to bind a tenant sees no rows.
- The app role cannot UPDATE or DELETE `audit_log`. On `record_versions` it may only stamp `superseded_at`.
- `migrations/` holds Alembic revisions as explicit SQL. Revision 0001 creates the tables, RLS, the role and its grants.
- `records/service.py` is the business logic. Permissions are `admin > approve > propose > read` and inherit down the scope tree; an org admin is admin on every scope. Packet submission checks the hash, then the TTL, then the grant at submission time, and is idempotent on `payload_hash`, including when two identical submissions race.
- Bi-temporal records: superseding a version stamps its `superseded_at`. If the old value started earlier in valid time, a `derived` row keeps believing it for the interval before the change. `GET /records?as_of=&known_at=` answers "what did we believe on date X about date Y".
- `records/audit.py` is the hash chain. The head lock is `FOR NO KEY UPDATE` on the org row; plain `FOR UPDATE` deadlocks against the FK key-share locks that every insert takes. `anchor` writes the head to `QUILL_ORG_AUDIT_ANCHOR_DIR` as a write-once file. `scripts/verify_audit_chain.py` walks the chain and checks the anchors.
- Credentials take the form `<org>.<id>.<secret>`, so the tenant can be bound before any lookup. Access tokens are 15-minute HS256 JWTs signed with `QUILL_ORG_JWT_SECRET`. Member status and credential revocation are re-checked on every request.
- The API mounts on the existing coordinator app only when `QUILL_ORG_DATABASE_URL` is set. Deploy files are in `deploy/org/`, and dependencies are in `requirements-org.txt`. Nodes never install those dependencies.

Operator CLI: `python -m org_coordinator.records migrate | bootstrap | anchor | verify`.

## Flags

| Env | Default | Effect |
|---|---|---|
| `QUILL_RECORDS` | `0` | Turns on the claim builder and the records tick. |
| `QUILL_CLAIM_PROPOSE_MIN_CONF` | `0.7` | Auto-propose threshold, applied only when a scope is suggested. |
| `QUILL_CAPTURE_EXPIRY` | `dry_run` | `off`, `dry_run` or `enforce`. |
| `QUILL_CAPTURE_TTL_DAYS` etc. | 30 / 90 / 7 / 1 | Local policy when the node is in no org. Org policy overrides it. |
| `QUILL_ORG_DATABASE_URL` | unset | Service only. Mounts the records API. |
| `QUILL_ORG_JWT_SECRET` | unset | Service only. At least 32 characters. |
| `QUILL_ORG_AUDIT_ANCHOR_DIR` | unset | Service only. Where daily anchors go. |

## Tests

`test_records_canonical` and `test_records_node` run anywhere. `test_records_org_service` and `test_records_e2e` need Postgres and skip without `QUILL_ORG_TEST_DATABASE_URL` (see `tests/records_pg.py`); they never fall back to SQLite.

## Where this departs from the spec, on purpose

- **Multi-valued predicates.** Dedup is on subject plus predicate only for single-valued predicates. For `owes` and `decided`, the value is part of the identity, both for claims and for the record's `record_key`. One person owes many things, and treating two promises as a conflict would be wrong.
- **Subject refs are org-portable names** (`entity:<normalized name>`, `person:<normalized name>`, `self` → `member:<id>`), not node-local row ids. A node's `person:42` means nothing to another node.
- **The packet carries `expires_at` inside the payload**, so the service enforces the TTL the human saw and the hash covers it.
- **Teams.** Scopes are authoritative for org teams. `team_layer` keeps personal peer groups unchanged; the scope-to-group sync lands in Phase 2.

## Not in Phase 1

- Write-back connectors.
- `decision`, `status` and `field_update` extraction. The schemas exist, but the extractor prompt doesn't ask for these kinds yet.
- Hold stamping on new captures, and hold export.
- Consent modes.
- The departure workflow. Revocation already takes effect on the next request.
- SSO, and mTLS between node and service.
- The admin manual-entry path, and redaction.
- Notifications pushed to nodes; nodes reconcile on heartbeat.
