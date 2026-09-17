# Tasks that close themselves, slots that wait for data

Built from the "Connector Capture & Task Fulfillment" spec (2026-09-17). This
page is the engineer's map: where each piece lives, what the flags do, and
the invariants the tests pin. The spec itself is the product reference.

## Vocabulary

| Term | In code |
|---|---|
| Task | a `commitments` row (the task model); `tasks` rows stay for legacy vision to-dos |
| Status | `commitments.status`: `open · awaiting_data · uncertain · done · declined · cancelled` (`commitment_state.STATUSES`) |
| Open slot | a task in `awaiting_data` with `slot_json` — `services/slots.py` |
| Fill | an event that scored above the slot's threshold; a row in `slot_candidates` |
| Completion evidence | an `outcome_verify.Evidence`; `verified` closes, anything weaker asks |
| Null result | `peer_channel.null_result(reason)` — `no_memory · policy_denied · offline` |
| Team fan-out | `team_layer.fanout_ask` + `merge_fanout` (30 s deadline) |
| Source navigation | Option B — `slots.navigate_to_source` → browser agent → `agent.fetch` event |

## The one seam

Every persisted event passes through `Store.insert`, whichever producer made
it. `task_completion.attach()` registers a post-insert hook
(`storage.add_insert_hook`) and, for each event:

1. **Completion detector** — matches open (and `uncertain`) tasks by
   counterparty and text overlap, classifies `completes | progresses |
   unrelated` with deterministic rules first, then applies the verdict through
   `outcome_verify.status_from_evidence`.
2. **Slot watcher** — `slots.evaluate_event` scores the event against every
   open slot; above threshold → `slot_candidates(offered)` + a Deliver / Not
   it offer.
3. **Salience gate** — `salience.evaluate` for connector-sourced events.

Sync mode for tests: `QUILL_TASK_COMPLETION_SYNC=1` runs the hook inline.
Otherwise a daemon thread drains a queue so capture never waits.

## Flags

| Flag | Default | Off |
|---|---|---|
| `QUILL_TASK_AUTOCOMPLETE` | on | `0` — no evidence-based closes |
| `QUILL_TASK_COMPLETION_LLM` | off | `1` — local-model tie-break for text-only overlap |
| `QUILL_SLOTS` | on | `0` — no slots, no null-path options |
| `QUILL_CONNECTOR_SYNC` | on | `0` — no background connector polling |
| `QUILL_SALIENCE` | on | `0` — silent persist for every connector item |
| `QUILL_SALIENCE_LLM` | off | `1` — tie-break just under threshold |
| `QUILL_SLOT_MATCH_THRESHOLD` | 0.78 | |
| `QUILL_SLOT_UNDO_S` | 10 | undo window on pre-approved delivery |
| `QUILL_TEAM_FANOUT_DEADLINE_S` | 30 | read at call time |
| `QUILL_PEER_MAX_HOP` | 1 | no transitive asks |

## Invariants the tests pin

- `completed` requires a cite from every actor; the user's own close carries
  `user_mark_done` / `user_confirm`. `fulfillment.summarize` counts `done`
  only when the closing transition has an `evidence_id` or a user actor.
- `declined` is terminal for capture: `thread_key` blocks re-proposal from the
  same source thread; a new mention elsewhere mints a new task with
  `linked_declined_id`.
- A fill is an offer, never a silent completion; `deliver_on_fill` is
  auto-approved with an undo, not silent. Not-it halves the score and the
  pair is never offered again.
- Only the first fill delivers; the second is logged (`superseded`).
- A miss and a denial are typed apart on the wire and in chat. `declined:
  true` on the old answer shape still means `policy_denied`.
- `/peer/update` and `/peer/slot-resolved` accept a peer token only for asks
  we sent / slots we hold (`test_peer_isolation` pins the route set).
- A sensitive or never-send fill closes the slot locally and is never
  delivered; the requester gets `policy_denied`.
- Connector sync is idempotent: `items_landed` stays flat across re-syncs of
  the same items; an edited item (same id, new content) lands once more.
- No test writes into the process-default LanceDB: `slots.index_if_bound`
  indexes only when the memory engine is bound to that store, and
  `QUILL_SLOT_SIM=overlap` keeps the embedder unloaded.

## Scenario harness

`tests/test_scenarios_boston.py` runs three in-process instances. Each has
its own `Store` and peer files (the peer channel reads its paths from the
environment at call time) and a `FakeWorker` that records offers and
notices. `Harness.post_json` replaces HTTP: it switches into the target
instance's context and calls the handler. Fan-out is sequential there
(`QUILL_TEAM_FANOUT_SEQUENTIAL=1`) because the context switch is
process-global.

## Where things are

- `app/services/commitment_state.py` — states, statuses, legality
- `app/storage.py` — `_migrate_task_slots`, `list_tasks`, slot candidates,
  `connector_sync`, insert hooks
- `app/services/task_completion.py` — detector, questions, user-created tasks
- `app/services/slots.py` — slots, matcher, delivery, Option A/B, erasure
- `app/services/salience.py` — the gate
- `app/services/connectors/scheduler.py` — background sync
- `app/services/peer_channel.py` — null results, options, `deliver_fill`,
  `handle_update`, `handle_slot_resolved`
- `app/services/team_layer.py` — fan-out deadline/merge, slot requests, policy
- `app/api/routes.py` — `/tasks…`, `/teams…`, `/peer/update`,
  `/peer/slot-resolved`, chat intents
- `app/api/adoption.py` — `/connectors/{id}` status, `/background`, `/sync-all`
