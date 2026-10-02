# Fleet federation

Each person's agent fleet publishes structured signals into their own Sparrow, and Sparrows exchange those signals only through the firm relay (`org_coordinator/`). Agents never talk across the boundary, and nothing received from a peer can reach an execution surface. Everything is off unless `QUILL_FLEET=1`.

Source plan: "Sparrow fleet federation: implementation plan", Oct 2 2026. This doc maps that plan to the code and records where the build departs from it.

```
agent ──POST /fleet/publish──▶ Sparrow A ──POST /relay/publish──▶ relay ──POST /peer/ask (kind=signal)──▶ Sparrow B ──GET /fleet/stream──▶ agent
          per-agent token        router decides          node token + HMAC      topics, barriers,         relay inbound token + HMAC       registered topics only
                                 share | offer | local                          restricted list, log
```

## The five invariants and where each is enforced

| Invariant | Enforced in |
|---|---|
| 1. Signals carry information, never instructions | `fleet/inbound.py` emits `peer.signal` events tagged `epistemic=inferred`, `never_authorizes`; `trust.source_can_authorize` is False for `fleet.`/`peer.`; source classes `fleet_agent`/`peer_signal` mint claims only; `slots._NEVER_FILL_PREFIXES` and `task_completion.detect` ignore signals; `tests/test_fleet_inbound.py` walks the static import graph (function-level imports included) from `inbound.py` and proves it never reaches `agent_planner`, `browser_agent`, or `desktop_agent` |
| 2. Sparrow decides what leaves, by rule | Agent input has no recipient field (`envelope.AGENT_FIELDS`); `fleet/router.py` is the only caller of `relay_client.send`; a paired peer can neither send nor receive `kind="signal"` (`peer_channel._RELAY_ONLY_KINDS`) |
| 3. No positions, orders, or P&L | `envelope.validate` rejects unknown fields and names order-like ones (`order_like_field`); the schema has no such properties; a routing rule whose topic names positions/orders can never be `share`; peer-channel class `trading` can never be `auto` |
| 4. No LLM rewrite in transit | Outbound goes `router → relay_client.prepare` (hop + 1, validate, HMAC) and never touches `compose_peer_claims`; the relay re-signs per link but forwards every other byte unchanged (both pinned by tests) |
| 5. Fail closed | No rule → local; malformed routes file → local; missing/malformed restricted list → refused (Sparrow) / 503 (relay); no relay registered → local; unset admin/compliance token → those endpoints refuse everyone; a node with no barrier group sees nothing |

## Files

Sparrow side, `app/services/fleet/`:

- `envelope.py` — the `Signal` dataclass, `SIGNAL_SCHEMA`, `validate`, canonical JSON, `sign`/`verify`, restricted-list parsing. Stdlib only and free of app imports: the relay imports it directly.
- `registry.py` — per-agent tokens (`register_agent`, `revoke_agent`, `list_agents`), SHA-256 only on disk.
- `ingress.py` — `POST /fleet/publish`: agent gate, topic check, per-agent rate limit, stamping, validation, emit, route.
- `feed.py` — bus subscriber, bounded ring, SSE wake-ups, catch-up from the ring and the event store.
- `router.py` — `fleet_routes.json` rules, restricted list, egress checks, offers (approval packets).
- `relay_client.py` — register/enroll with the relay, signed sends, outbox with retry.
- `inbound.py` — relay deliveries: auth, HMAC, validation, own-origin drop, dedupe, emit.
- `dedup.py` — origin-id ledgers (own and seen), persisted, TTL-bounded.
- `state.py` — instance tag and relay credentials.

HTTP: `app/api/fleet_routes.py`; `/peer/ask` in `app/api/routes.py` hands `kind="signal"` to it before pairing auth runs. Team page panel in `app/api/peer_page.py`. MCP: read-only `signals` tool in `mcp_tools.py`. Agent client: `fleet_client.py` (stdlib only; copy it into an agent).

Relay side, `org_coordinator/`: `topics.py` (topics, barrier groups, admin/compliance tokens), `relay.py` (publish, forward, retry queue, restricted list, enrollment links), `relay_log.py` (append-only hash chain), `relay_routes.py`, `verify_chain.py` (CLI).

## Setting up a pilot (two Sparrows, one relay)

1. Relay: `QUILL_RELAY_ADMIN_TOKEN=… QUILL_RELAY_COMPLIANCE_TOKEN=… python -m org_coordinator.main`.
2. Compliance sets the list: `PUT /relay/admin/restricted {"instruments": [...]}`. Each Sparrow also needs `data/restricted_list.json` (`{"instruments": [...]}`); without it nothing leaves.
3. On each Sparrow (`QUILL_FLEET=1`): `POST /fleet/agents {"name", "topics", "role"}` and hand the returned token to the agent (shown once). Then `POST /fleet/relay/register {"relay_url", "node_id", "fleet_url"}`. `fleet_url` is how the relay reaches this Sparrow, so use `internal_url` for on-box peers.
4. Admin: `PUT /relay/admin/nodes/{node_id}/group {"group": "research"}` per node, then `PUT /relay/admin/topics/{topic} {"members": [...], "groups": [...]}`.
5. Owner opts a topic in: `PUT /fleet/routes {"rules": [{"topic": "macro.rates", "action": "share"}]}`. A rule without an action is `offer`.
6. Agents publish with `FleetClient(url, token).publish({...})` and read with `.subscribe([...])`.

Compliance reads `GET /relay/compliance/feed` and `GET /relay/compliance/verify`, or runs `python -m org_coordinator.verify_chain`.

## Flags and paths

| Env | Default | Meaning |
|---|---|---|
| `QUILL_FLEET` | `0` | Master switch (Sparrow) |
| `QUILL_FLEET_MAX_HOPS` | `2` | Hop cap, both directions |
| `QUILL_FLEET_RATE` | `30` | Signals per agent per minute |
| `QUILL_FLEET_TTL_S` | `3600` | Default `expires_at` when the agent sets none (cap 7 days) |
| `QUILL_FLEET_RING` | `1000` | In-memory catch-up ring |
| `QUILL_FLEET_RELAY_URL` | — | Default relay URL (registration stores its own) |
| `QUILL_FLEET_OWNER_AUTH` | `0` | Require the API token or an unlocked session for owner routes even from loopback |
| `QUILL_FLEET_AGENTS` / `_ROUTES` / `_RESTRICTED` / `_STATE` / `_OFFERS` / `_OUTBOX` / `_ORIGINS` | `data/fleet_*.json`, `data/restricted_list.json` | Stores |
| `QUILL_RELAY_ADMIN_TOKEN` / `QUILL_RELAY_COMPLIANCE_TOKEN` | unset (refuse all) | Relay role tokens |
| `QUILL_RELAY_MAX_HOPS` | `2` | Relay's hop cap |
| `QUILL_RELAY_LOG` / `QUILL_RELAY_RESTRICTED` | `<coord data>/relay_log.jsonl`, `<coord data>/restricted_list.json` | Relay stores |

## Where the build departs from the plan, on purpose

- **HMAC keys are the SHA-256 of the bearer token, not the token.** The plan said "sign with the relay token as the HMAC key", but the relay stores node tokens hash-only (`token_sha256`) and could not verify an HMAC keyed on the plaintext. `envelope.link_key(token) = sha256(token)`: the holder of the plaintext and the holder of the hash compute the same key. The same scheme covers the relay→Sparrow leg with the inbound token. Anyone who can read the relay's `directory.json` can forge a node's signature, so treat that file as a secret.
- **The relay authenticates to Sparrow with its own inbound credential, not a peer pairing.** The plan said "reuse peer_channel auth". Making the relay a paired peer would put it in presence pings, the mailbox flush, and the Team page, and would let any paired peer push `kind="signal"`. Instead, `/peer/ask` checks `kind="signal"` first and accepts it only on the relay's inbound token; `peer_channel.handle_ask` and `ask` refuse `signal` outright.
- **Forwarding tokens are kept out of the directory.** `/directory` returns every node record to every node, so `relay.enroll` stores the inbound tokens in `relay_links.json`.
- **`/register` no longer re-mints for an existing node without its current token.** It used to rotate the token for anyone who posted the same `node_id`, which on a relay is a takeover of that node's topics and barrier group. `org_client.register` now presents its token when re-registering.
- **"New topics → offer" and "unmatched → local".** The plan's table lists both. Read here as: a rule written without an action is `offer`; a signal no rule matches is `local`.
- **"trading can never be auto for any rule that matches positions or orders".** On the peer channel, `trading` joins `personal` in `NEVER_AUTO` (checked at write and at enforcement), and questions naming positions, orders, or P&L classify as `trading` deterministically, without the model. The regex is deliberately narrow, because "open positions in engineering" is hiring. In the fleet router, a rule whose topic names positions/orders is refused as `share` on write and downgraded to `offer` on enforcement.
- **`redact.py` has no LLM rewrite.** The plan said outbound signals skip "the LLM rewrite in redact.py". The LLM egress path is `peer_retrieval` → `compose_peer_claims`; that is what outbound signals never touch, and the test patches it to raise.
- **`epistemic="inferred"`.** There is no `reported` tag in `confidence.py`. `inferred` is the closest, and it never outranks what the user observed or said.
- **Signals cannot fill slots or close tasks.** Not in the plan. A slot with `deliver_on_fill` would otherwise forward a peer's view to a teammate over the peer channel, around the relay. Found while proving invariant 1.
- **The relay logs refusals and admin changes**, not just forwards: a restricted-ticker attempt is something compliance wants to see.
- **Agents may set `signal_id`** for idempotent retries; `origin_id` is always Sparrow's.
- **Envelope adds `derived_from`** (Phase 5 mentions it; the Phase 0 field list did not).

## Known limits

- **Local trust model.** On a loopback-bound desktop the whole Sparrow API is open to local processes. Owner routes refuse requests carrying an agent token, but an agent that also holds Sparrow API access could still manage routes. Set `QUILL_FLEET_OWNER_AUTH=1` where agents share the box.
- **JSON-file stores.** These are fine for a pilot. Move the relay log, topics, and queue to SQLite or Postgres before multi-desk use (the Records layer's Postgres audit chain is a candidate home).
- **HTTP plus SSE latency.** The end-to-end test delivers in well under 2 s on one box. Whether that is fast enough for the target signals is an open question.
- **Retention.** The log is an engineering baseline. What counts as compliant retention and surveillance is for the firm's compliance team.

## Tests

`test_fleet_envelope` (schema, order-like rejection, HMAC), `test_fleet_ingress` (401/403/422/429, provenance, source policy), `test_fleet_feed` (fan-out under 1 s, SSE, catch-up without duplicates, MCP tool), `test_fleet_router` (local/offer/share, trading never auto, restricted list, offers bound to hashes, outbox), `test_relay` (barriers, signatures, log chain and tamper detection, retry queue, register guard), `test_fleet_inbound` (dedupe, own-origin drop, hop limit, no re-forward, `source_can_authorize`, import boundary), `test_fleet_team_page` (live chromium), `test_fleet_e2e` (two Sparrows and a relay as real uvicorn processes; an agent on B receives A's signal in under 2 s and nothing comes back to A). Set `QUILL_SKIP_E2E=1` to skip the subprocess test.

## Open questions (from the plan, still open)

- Who owns `restricted_list.json`, and how often is it refreshed? (Today it is two files: the relay's, set through the admin API, and each Sparrow's.)
- Which barrier groups exist at launch, and who approves topic membership?
- Is HTTP plus SSE fast enough, or does the relay sit on the firm's message bus?
- What retention period and storage does compliance require for the relay log?
- Should a later phase add an A2A adapter for non-Sparrow agents?
