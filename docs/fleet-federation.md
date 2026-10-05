# Fleet federation

Each person's agent fleet publishes structured signals into their own Sparrow, and Sparrows exchange those signals only through the firm relay (`org_coordinator/`). Agents never talk across the boundary, and nothing received from a peer can reach an execution surface. Everything is off unless `QUILL_FLEET=1`.

The source plan ("Sparrow fleet federation: implementation plan", Oct 2 2026) used a trading fleet as its example. The build is domain-neutral: what a signal may carry is declared per **kind** in a registry, and trading is one example kind (`market_view` in [fleet-kinds.example.json](fleet-kinds.example.json)), next to `status_update`, `finding`, and `lead`.

```
agent ──POST /fleet/publish──▶ Sparrow A ──POST /relay/publish──▶ relay ──POST /peer/ask (kind=signal)──▶ Sparrow B ──GET /fleet/stream──▶ agent
          per-agent token        router decides          node token + HMAC      topics, barriers,         relay inbound token + HMAC       registered topics only
                                 share | offer | local                          kinds, blocked list, log
```

## The signal

| Field | Set by | Meaning |
|---|---|---|
| `topic` | agent | Routing key (`eng.status`, `research.ai`); agents publish only to topics they are registered for |
| `kind` | agent (default `note`) | Which registered kind this is; decides the body shape and the forbidden fields |
| `subject` | agent | What it is about (a project, account, instrument…); the kind says required, optional, or forbidden; checked against the blocked list |
| `summary` | agent | Human-readable text, at most 4,000 characters |
| `confidence` | agent, optional | 0..1 |
| `body` | agent | Structured fields declared by the kind |
| `sources[]` | agent | `{name, license, url?, as_of?}`; only `internal_ok` sources may leave this Sparrow |
| `signal_id`, `thread_id`, `derived_from`, `expires_at` | agent, optional | Idempotency, threading, re-share lineage, TTL (default 1 h, cap 7 days) |
| `origin_id`, `producer`, `ts`, `hops`, `sig` | Sparrow | Identity, timing, loop control, HMAC |

### Kinds

A kind declares the body's fields (string, number, integer, boolean, enum, or array of those), which are required, whether a subject is required, optional, or forbidden (with an optional pattern), and the fields that must never appear anywhere in that kind's payload:

```json
{"kinds": {"market_view": {
  "subject": "required", "subject_pattern": "^[A-Za-z0-9][A-Za-z0-9.:/_-]{0,31}$",
  "fields": {"direction": {"enum": ["bullish", "bearish", "neutral"]},
             "horizon":   {"enum": ["intraday", "days", "weeks", "months", "long_term"]}},
  "required": ["direction", "horizon"],
  "forbidden": ["quantity", "price", "order_id", "position", "pnl"]}}}
```

The built-in `note` kind (no body, optional subject) is always available. Every hop holds its own registry: each Sparrow (`PUT /fleet/kinds`, `data/fleet_kinds.json`) and the relay (`PUT /relay/admin/kinds`). A kind any hop does not know is refused there (`unknown_kind`), and a malformed registry falls back to `note` only. `GET /fleet/schema` publishes the envelope plus every kind's body schema.

## The five invariants and where each is enforced

| Invariant | Enforced in |
|---|---|
| 1. Signals carry information, never instructions | `fleet/inbound.py` emits `peer.signal` events tagged `epistemic=inferred`, `never_authorizes`; `trust.source_can_authorize` is False for `fleet.`/`peer.`; source classes `fleet_agent`/`peer_signal` mint claims only; `slots._NEVER_FILL_PREFIXES` and `task_completion.detect` ignore signals; `tests/test_fleet_inbound.py` walks the static import graph (function-level imports included) from `inbound.py` and proves it never reaches `agent_planner`, `browser_agent`, or `desktop_agent` |
| 2. Sparrow decides what leaves, by rule | Agent input has no recipient field (`envelope.AGENT_FIELDS`); `fleet/router.py` is the only caller of `relay_client.send`; a paired peer can neither send nor receive `kind="signal"` (`peer_channel._RELAY_ONLY_KINDS`) |
| 3. Some fields are never valid payloads | Per kind: `forbidden` fields fail `validate` anywhere in the payload (`forbidden_field`) at Sparrow ingress, at the relay, and on arrival; unknown fields always fail. `never_share` topics and kinds can never auto-share |
| 4. No LLM rewrite in transit | Outbound goes `router → relay_client.prepare` (hop + 1, validate, HMAC) and never touches `compose_peer_claims`; the relay re-signs per link but forwards every other byte unchanged (both pinned by tests) |
| 5. Fail closed | No rule → local; malformed routes file → local; unreadable `never_share` → every share becomes offer; unknown kind → refused; missing/malformed blocked list → refused (Sparrow) / 503 (relay); no relay registered → local; unset admin/compliance token → those endpoints refuse everyone; a node with no barrier group sees nothing |

## Routing

`data/fleet_routes.json` (`GET/PUT /fleet/routes`):

```json
{"rules": [{"topic": "research.*", "producer": "*", "action": "share"},
           {"topic": "sales.leads", "producer": "agent:scout", "action": "offer"}],
 "never_share": {"topics": ["hr.*"], "kinds": ["incident"]}}
```

- `share` sends at once, `offer` queues an approval packet on the Team page (and is a rule's default), and `local` never leaves. A signal that matches no rule stays local.
- The most specific rule wins; on a tie, the most restrictive action wins.
- A `share` rule on a `never_share` topic is refused when written. Any `never_share` match, by topic or by kind, is downgraded to `offer` when enforced.
- Approving an offer binds to the SHA-256 of the signal's canonical bytes. The owner may edit `summary`, `confidence`, `body`, and `expires_at`, and any edit needs a fresh approval. Every egress gate runs again at approval time.

The blocked list (`GET/PUT /fleet/blocked`, `data/fleet_blocked.json`; relay: `PUT /relay/admin/blocked`) names subjects that may never leave. Matching is exact (case- and space-insensitive) or by glob pattern: `{"subjects": ["Project Falcon"], "patterns": ["acme*"]}`.

## Files

Sparrow side, `app/services/fleet/`:

- `envelope.py` — the `Signal` dataclass, `SIGNAL_SCHEMA`, kind definitions and `load_kinds`, `validate`, canonical JSON, `sign`/`verify`, the blocklist. Stdlib only and free of app imports: the relay imports it directly.
- `kinds.py` — this Sparrow's registry and blocked list.
- `registry.py` — per-agent tokens, SHA-256 only on disk.
- `ingress.py` — `POST /fleet/publish`.
- `feed.py` — bus subscriber, bounded ring, SSE wake-ups, catch-up from the ring and the event store.
- `router.py` — rules, `never_share`, egress checks, offers.
- `relay_client.py` — register/enroll, signed sends, outbox.
- `inbound.py` — relay deliveries.
- `dedup.py` — origin-id ledgers.
- `state.py` — instance tag and relay credentials.

HTTP: `app/api/fleet_routes.py`; `/peer/ask` in `routes.py` hands `kind="signal"` to it before pairing auth runs. Team page panel in `app/api/peer_page.py`. MCP: read-only `signals` tool. Agent client: `fleet_client.py` (stdlib only).

Relay side, `org_coordinator/`: `topics.py` (topics, barrier groups, admin/compliance tokens), `relay.py` (publish, forward, retry queue, blocked list, kinds, enrollment links), `relay_log.py` (append-only hash chain), `relay_routes.py`, `verify_chain.py` (CLI).

## Setting up a pilot (two Sparrows, one relay)

1. Relay: `QUILL_RELAY_ADMIN_TOKEN=… QUILL_RELAY_COMPLIANCE_TOKEN=… python -m org_coordinator.main`.
2. Admin loads kinds and the blocked list: `PUT /relay/admin/kinds` (for example, the body of `docs/fleet-kinds.example.json`) and `PUT /relay/admin/blocked {"subjects": []}`.
3. On each Sparrow (`QUILL_FLEET=1`): `PUT /fleet/kinds` with the same kinds, `PUT /fleet/blocked`, then `POST /fleet/agents {"name", "topics", "role"}` (the token is shown once), then `POST /fleet/relay/register {"relay_url", "node_id", "fleet_url"}`.
4. Admin: `PUT /relay/admin/nodes/{node_id}/group {"group": "…"}` per node, then `PUT /relay/admin/topics/{topic} {"members": [...], "groups": [...]}`.
5. Owner opts topics in with `PUT /fleet/routes`.
6. Agents publish with `FleetClient(url, token).publish({...})` and read with `.subscribe([...])`.

Compliance reads `GET /relay/compliance/feed` and `GET /relay/compliance/verify`, or runs `python -m org_coordinator.verify_chain`.

## Flags and paths

| Env | Default | Meaning |
|---|---|---|
| `QUILL_FLEET` | `0` | Master switch (Sparrow) |
| `QUILL_FLEET_MAX_HOPS` | `2` | Hop cap, both directions |
| `QUILL_FLEET_RATE` | `30` | Signals per agent per minute |
| `QUILL_FLEET_TTL_S` | `3600` | Default `expires_at` (cap 7 days) |
| `QUILL_FLEET_RING` | `1000` | In-memory catch-up ring |
| `QUILL_FLEET_RELAY_URL` | — | Default relay URL |
| `QUILL_FLEET_OWNER_AUTH` | `0` | Require the API token or an unlocked session for owner routes even from loopback |
| `QUILL_FLEET_KINDS` / `_BLOCKED` / `_AGENTS` / `_ROUTES` / `_STATE` / `_OFFERS` / `_OUTBOX` / `_ORIGINS` | `data/fleet_*.json` | Stores |
| `QUILL_RELAY_ADMIN_TOKEN` / `QUILL_RELAY_COMPLIANCE_TOKEN` | unset (refuse all) | Relay role tokens |
| `QUILL_RELAY_MAX_HOPS` | `2` | Relay's hop cap |
| `QUILL_RELAY_LOG` / `_BLOCKED` / `_KINDS` | `<coord data>/relay_log.jsonl`, `blocked.json`, `fleet_kinds.json` | Relay stores |

## Where the build departs from the plan, on purpose

- **Domain-neutral envelope.** The plan's fields (`instrument`, `direction`, `horizon`, `thesis`) and its "no positions/orders/P&L" rule are now one example kind (`market_view`). The core is `kind` / `subject` / `summary` / `body`. The plan's `restricted_list.json` became a blocked-subjects list, and its trading-class rule became `never_share`. No `trading` class was added to the peer channel's disclosure classes.
- **HMAC keys are the SHA-256 of the bearer token, not the token.** The relay stores node tokens hash-only, so it could not verify an HMAC keyed on the plaintext. `envelope.link_key(token) = sha256(token)`. Anyone who can read the relay's `directory.json` can forge a node's signature, so treat that file as a secret.
- **The relay authenticates to Sparrow with its own inbound credential, not a peer pairing.** `/peer/ask` checks `kind="signal"` first and accepts it only on the relay's inbound token. `peer_channel.handle_ask` and `ask` refuse `signal` outright.
- **Forwarding tokens are kept out of the directory** (`relay_links.json`), because `/directory` returns every node record to every node.
- **`/register` no longer re-mints for an existing node without its current token.** It was a takeover of that node's topics and barrier group. `org_client.register` now presents its token.
- **"New topics → offer", "unmatched → local".** Read as: a rule without an action is `offer`; no matching rule means `local`.
- **`redact.py` has no LLM rewrite.** The LLM egress path is `compose_peer_claims`; outbound signals never touch it.
- **`epistemic="inferred"`**: there is no `reported` tag; `inferred` never outranks what the user observed.
- **Signals cannot fill slots or close tasks.** A `deliver_on_fill` slot would otherwise forward a peer's signal to a teammate around the relay.
- **The relay logs refusals and admin changes** (topics, groups, kinds, blocked list), not just forwards.
- **Envelope adds `derived_from`** (Phase 5 mentions it; Phase 0 did not).

## Known limits

- **Local trust model.** On a loopback-bound desktop, owner routes refuse agent tokens, but an agent that also holds Sparrow API access could manage routes. Set `QUILL_FLEET_OWNER_AUTH=1` where agents share the box.
- **Kinds are copied, not synced.** Each hop holds its own registry. A kind changed on one Sparrow and not at the relay is refused at the relay, which is the safe failure, but it is still a failure. A later phase could have Sparrows pull kinds from the relay.
- **JSON-file stores.** These are fine for a pilot. Move the relay log, topics, and queue to SQLite or Postgres before multi-team use.
- **Retention.** The log is an engineering baseline; what counts as compliant retention is for the organization's compliance team.

## Tests

`test_fleet_envelope` (core, kinds, forbidden fields, registry validation, HMAC, blocklist), `test_fleet_ingress` (401/403/422/429, provenance, kinds and blocked-list APIs, source policy), `test_fleet_feed` (fan-out under 1 s, SSE, catch-up without duplicates, MCP tool), `test_fleet_router` (local/offer/share, `never_share`, blocked subjects, unknown kinds, offers bound to hashes, outbox), `test_relay` (barriers, signatures, kinds and forbidden fields at the relay, log chain and tamper detection, retry queue, register guard), `test_fleet_inbound` (dedupe, own-origin drop, hop limit, no re-forward, `source_can_authorize`, import boundary), `test_fleet_team_page` (live chromium), `test_fleet_e2e` (two Sparrows and a relay as real uvicorn processes). Set `QUILL_SKIP_E2E=1` to skip the subprocess test.

## Open questions

- Who owns the kinds registry and the blocked list, and how are they kept in step across hops?
- Which barrier groups exist at launch, and who approves topic membership?
- Is HTTP plus SSE fast enough, or does the relay sit on an existing message bus?
- What retention period and storage does compliance require for the relay log?
- Should a later phase add an A2A adapter for non-Sparrow agents?
