# Brains: the memory constellation, by subject

Built 2026-11-10 against the "Sparrow Memory Constellation: Functional Spec"
(Nov 10, 2026). A **brain** is one subject (a deal, a person, an org) and
everything Sparrow knows about it, grouped into a fixed set of typed
sections, with a packet an agent can use in one click. The 2D constellation
is untouched and stays at `/memory?mode=constellation`.

## Where things live

| Piece | Path |
|---|---|
| Section schemas, one per brain type | `app/schemas/brains/{deal,person,org,portco}.json` |
| Tables `brain_items`, `brain_gaps`, `brain_overrides` | `app/storage.py` (CREATE TABLE IF NOT EXISTS, beside `graph_dirty`) |
| Compiler, layout, packet | `app/services/brains.py` |
| HTTP routes | `app/api/brain_routes.py` (included from `app/main.py`) |
| MCP tool `brain_context` | `app/services/mcp_tools.py` |
| Egress path `brain_packet` | `app/services/egress.py`, `app/services/model_log.py` |
| 3D map | `app/static/js/sparrow-brains.js`, Three.js r170 vendored in `app/static/js/vendor/` |
| Mode entry | `/memory?mode=brains` (`app/api/console_page.py`), Inspect index (`app/api/shell_routes.py`) |
| Local section classifier (step 5) | `app/services/brain_classifier.py`, `POST /brains/classify` |
| Benchmark against the budgets | `scripts/bench_brains.py` |
| Agent-quality harness | `scripts/brains_quality.py` |
| Tests | `tests/test_brains.py` |

## Data flow

Existing tables (facts, claims, commitments, tasks, relations,
kg_predicates, entity/person attrs) feed the compiler, which writes only
`brain_items` (derived, safe to drop) and system rows in `brain_gaps`.
`brain_overrides` holds the user's word (move, hide, set_type) and is
written only by the override routes. The map and the packet read the same
compiled rows; only the packet path crosses the egress gate.

### Which subjects are brains

Every entity and person the constellation would show (not hidden, not
merged away) whose type is known and that has at least 3 assigned items
after rules run. Type: `entities.kind` `project` maps to `deal`; `org`,
`company`, `organisation` map to `org`; people are `person`; `portco` only
through a user `set_type` override. Entities of other kinds (tool, place,
idea) have no brain in v1 unless retyped.

### Section assignment

First match wins:

1. a `move` or `hide` row in `brain_overrides`;
2. the structural rules a section names in its schema `sources`
   (`people_links`, `affiliations`, `person_attrs`, `person_links`,
   `status_claims`, `status_attr`, `next_task`, `open_commitments`,
   `commitment_claims`, `open_tasks`, `decision_claims`, `competes_with`,
   `question_facts`);
3. `keywords:<bucket>` rules (thesis, risks, competitors, performance), a
   local regex classifier, no model call;
4. `hint:model`: the fact's `section_hint` (see below), accepted by the
   sections whose schema `hints` list it, stored as `assigned_by='model'`;
5. `remaining_facts` (the person type's Notes section);
6. anything left is out of the brain and counted in `unassigned`.

### Section hints (step 5)

`facts.section_hint` holds one of `thesis, status, commitment, risk,
competitor, decision, question, role, relationship, performance, other`.
Two writers:

* the extractor, when `QUILL_EXTRACT_SECTION_HINT=1` adds the field to the
  claims schema (off by default, like every other prompt change here,
  until `eval_extraction` shows no precision cost);
* `brain_classifier.backlog_pass()` (`POST /brains/classify?limit=`),
  which sends only the facts no rule placed to the LOCAL text model
  (`OllamaText` directly, never the router, so it can never escalate to a
  cloud model), writes the hints, and recompiles the brains it touched.
  When the local model is down it reports `unavailable` and writes nothing.

A user move or hide always outranks a hint; every deterministic rule runs
before it.

Gaps: one system gap per empty `required` section (question text from the
schema's `gap` template), one per unresolved `claims.conflict_with` pair.
A system gap resolves itself when the section fills or the conflict
settles; user gaps stay until the user removes them.

### Review state, rank, activity

* `approved`: a claim with `record_ref` or status `approved`/`recorded`; a
  fact whose inbox review is `approved` or `edited`; a user-set attr; a
  KG belief that is protected or user-layer. Everything else live is
  `pending`. Dismissed facts and rejected/discarded/expired claims are not
  items at all. Extractor-asserted edges (works_at) are therefore pending
  until a human confirms them.
* `rank = confidence × temporal_salience(age) × (1.0 approved | 0.6 pending)`,
  plus 0.25 when the fact is pinned in the constellation.
* `activity = max(0.15, 2·sigmoid(n/4) − 1)` where n is the number of items
  observed in the last 30 days.

### Layout

Pure functions of ids: section anchors on a Fibonacci sphere by schema
order; leaves in a cone around their section's direction, angle and
distance hashed from `item_type:item_id`; brains in the overview on a
hashed direction with distance falling as activity rises, then 30
deterministic relaxation steps in id order. No position columns.

### Rebuild

`brains.rebuild(store, scope="full"|"dirty")`. Reads happen through
`ensure_compiled()`: a full rebuild the first time or when any schema
version changes, otherwise an incremental pass when `memory_version()`
moved. The incremental pass recompiles `graph_dirty` nodes plus the
subjects of facts, claims, edges, attrs, overrides and gaps that changed
since the last compile. A no-op recompile writes nothing; rank drift does
not bump `updated_at`. `memory_version()` now includes `brain_items`, so a
recompile that changes a section moves the token the map polls. A first
or schema-change rebuild on a store above `FULL_REBUILD_INLINE_FACTS`
(3,000 facts) runs on a daemon thread: the list answers at once with
`building: true`, the map says so and keeps polling until the version
token moves.

## API

All JSON, same auth middleware as `/graph`. Brain ids are `entity:<id>` or
`person:<id>`.

| Route | Purpose |
|---|---|
| `GET /brains?type&limit&since` | Overview. `since=<version token>` returns only brains whose `updated_at` is later than that compile. |
| `GET /brains/{id}` | One brain: sections with `label`, `dir`, `required`, items with `ref`, `text`, `review_state`, `source_label`, `observed_at`, `evidence_id`, `source_event_id`, `pos`, `size`, `rank`. |
| `POST /brains/{id}/packet` | `{scope, section, include_pending, budget_tokens, destination}` → packet object. 403 with `verdict: "deny"` when the rung closes the path; 404 unknown brain/section; 422 bad scope/budget; 429 above 30 a minute. |
| `POST /brains/{id}/gaps` | `{section, question}` adds a user gap. |
| `POST /brains/{id}/items/move` | `{item_type, item_id, section}` writes a move override, logs a `brain_move` adjudication. |
| `POST /brains/{id}/items/hide` | `{item_type, item_id}` writes a hide override. |
| `POST /brains/{id}/type` | `{brain_type}` sets the type (an entity never becomes a person, or the reverse). |
| `POST /brains/rebuild?scope=full` | Recompile, same shape as `/graph/rebuild`. |

## Packet

`brains.compile_packet()` serves the Copy button and the MCP tool;
identical arguments give identical text. Plain markdown: a three-line
header, `## Section` headers in schema order, `- text [Source · Mon D]`
lines in rank order, `(unreviewed)` prefix on pending items when
`include_pending` is on, `UNKNOWN:` lines for gaps always. Item text is
collapsed to one line with markdown headings and code fences stripped, so
an item cannot fake a section. Verbatim quotes and spans never appear.
Tokens are `ceil(chars / 4)`. Over budget, the lowest-ranked item of the
fullest section is dropped one at a time, headers stay, each trimmed
section ends with `(n more omitted)`, and the count lands in
`withheld.budget`.

Egress: `egress.decide()` runs per item on path `brain_packet` with
`policy="allow"`, `data_class="work"`, the item's stored privacy class,
and `approved=True` only for a clipboard copy (the click is the human
approval). Under `perimeter` only claims with a `record_ref` pass and
`include_pending` is ignored; under `sealed` items above `internal` are
withheld; over `mcp` the click shortcut does not apply, so sealed and
perimeter deny the whole packet. Claims with `personal_only=1` render on
the map and never leave on any rung (`withheld.personal`). One
`record_send()` row per packet, allowed or denied, visible at
`/privacy/egress` as `egress.brain_packet`.

## Front end

`window.SparrowBrains.mount(el, opts)` in `sparrow-brains.js`, loaded on
demand the first time the Brains chip is clicked. Three states (overview,
brain focused, section selected), orbit/zoom with pitch clamped to ±70°,
600 ms fly-to (an instant cut under reduced motion), a 4 px drag threshold
so a drag never selects, HTML labels capped at 40, depth cue to 25%
opacity, render on demand only, camera and focus persisted under
`sparrow.brains.v1` with non-finite values reset, URL
`?mode=brains&brain=…&section=…` kept with `replaceState`. The card is a
right-hand panel (a bottom sheet under 720 px) with chips, item list, Copy
brain / Copy this section / Include pending, withheld counts after each
copy, a selectable fallback box when the clipboard refuses, and an item
menu (move, hide, add a gap). List view is a sortable table and the
screen-reader path. Without WebGL the same scene draws on a 2D canvas
through the same camera math. Live updates poll `/graph/version` every
5 s while visible and patch with `?since=`. Hub and edge colours come from
the page's theme tokens and follow the theme toggle live.

## Decisions that differ from the spec, on purpose

* Routes live in `app/api/brain_routes.py`, not inside `routes.py`, the
  same split the inbox and team-search routes use.
* The "local classifier" in v1 is keyword rules (`keywords:*` sources), so
  step 1 ships with no model call. Items it places carry
  `assigned_by='rule'`. The `section_hint` extractor field and the backlog
  model pass (step 5) are not built.
* `withheld` has a fourth key, `personal`, so a personal-only claim is not
  mistaken for a privacy-class refusal.
* User moves and hides are logged into `kg_adjudications` (kinds
  `brain_move`, `brain_hide`) rather than a new table.
* `?since=` compares against the time the server first saw that version
  token, so an unknown token returns every brain.
* The rate limit is keyed by client host plus session cookie, in process.
* Status keeps the newest three claims/attrs plus one next step; other
  sections cap at the schema's `max_items`. Items over a cap count as
  unassigned.
* The 3-item threshold counts assigned non-gap items.

## Performance (step 6)

`python3 scripts/bench_brains.py --brains 500 --items 40` seeds 500 deal
subjects with 17,000 facts and ~18,800 compiled items (the spec's scene
size) and measured, on the GB10, 2026-11-11:

| Measure | Budget | Measured |
|---|---|---|
| Full rebuild | < 30 s per 100k facts | 4.5 s for 17k facts (26 s / 100k) |
| `GET /brains` 560 brains | < 150 ms p95, < 300 KB gzipped | 136 ms cold (once per change), 7 ms warm; 11 KB gzipped |
| `GET /brains/{id}` 36 items | < 100 ms | 7 ms |
| `POST /brains/{id}/packet` | < 200 ms | 20 ms |
| Incremental recompile | < 50 ms per dirty brain | 5 ms per brain, plus a fixed ~290 ms to load the index |

What the pass changed: the read path uses a light index (subjects only, no
facts); a dirty pass links facts to a subject by a per-subject substring
scan instead of building the whole name index; `graph_dirty` marks count
only when stamped since the last compile (the extraction queue is cleared
on graph.rebuild's schedule, not ours); system gaps no longer look like
changes; overview relaxation runs vectorised over KD-tree pairs. The fixed
index load on a dirty pass is the remaining cost: `list_facts` over every
row plus claims, edges and attrs. Loading only the rows a dirty subject
touches would remove it.

## Accessibility (step 6)

Canvases are `aria-hidden`; the stage is a `role=application` with a
description of the keys; the message strip is a live status region; the
card is a labelled live region; chips and sort headers carry
`aria-pressed` / `aria-sort`; list rows mark the open brain with
`aria-current`; every control is at least 44 px on coarse pointers; arrow
keys orbit, `+`/`-` zoom, Escape steps back, Enter on a list row moves
focus into the card; reduced motion cuts instead of flying and disables
auto-rotate. No automated audit (axe) ran; this was a review of the
markup and a keyboard pass in Playwright.

## Agent-quality check

`scripts/brains_quality.py run tasks.json --out quality/` runs each task
three ways (no context, raw memory search, brain packet) at the same token
budget through this seat's router, writes the answers shuffled and
unlabeled for blind 1-5 scoring, and `score` reports means per condition
and how many tasks the packet won. The spec's bar: if the packet does not
clearly beat search (here: 70% of tasks), fix the section schema first.
Not run yet: it needs ten real tasks against a real seat's store.

## Not done

* Running the agent-quality check on real tasks and acting on it.
* Loading only touched rows on a dirty pass (the ~290 ms fixed cost).
* Opening a teammate's brain over the peer channel.

## Open questions the spec left, and what the code assumes

* Approved means `record_ref` set or claim status approved/recorded, or
  inbox review approved/edited.
* Portco is user-set only (`POST /brains/{id}/type`).
* Every `project` entity is a deal until retyped.
* Copy counts as approval under Sealed (no second confirm).
* The wheel zooms only while the pointer is over the map.
* The user's own person row gets a brain like anyone else; its claims
  with `subject_ref='self'` join it.
* Brains ship on the current `entities` table; a merge shows up as the
  absorbed row disappearing from the overview on the next compile.
