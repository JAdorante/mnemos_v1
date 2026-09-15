# Context Association Layer (CAL) — handoff

What exists, how to measure it, and what will mislead you. Written 2026-09-11.

CAL binds raw events to graph nodes so a timeline can say what you were working
on. The design's economic claim is that **the LLM's job is to mint bindings,
not to use them**: cost scales with the number of novel identifiers, not events.
Nothing in what has shipped calls a model.

## Read this before you measure anything

**This dev box and the hosted pilot are different capture surfaces.** A number
measured on one does not transfer to the other, and most of the first day of
this work was spent tuning against a surface no tester has.

| | dev box (X11 desktop capture) | hosted pilot (browser share) |
|---|---|---|
| window titles | real, dozens distinct | **1 distinct, constant** |
| click stream | yes | none |
| `app_name` | present | absent |
| `url_domain` | 0% (WS2d dark) | absent |
| vision | webcam feed | real `desktop.screen` OCR |
| anchors | surfaces 36% / keys 0.2% | surfaces **0%** / keys 5% |

The pilot's `meta["window"]` was the display's name from `getDisplayMedia`
(`"Primary Monitor"`, 145 identical values in one day) until the fix in
`desktop_capture._web_share_win`. It is now left empty on a monitor share,
because an honest blank beats a field that is always the same lie. **Titles on
the pilot are now honest, not informative** — getting real ones needs a window
or tab share, which is a product decision nobody has made.

## Who owns what

Do not add a second extractor, stoplist, or public-suffix table. Reconciling
the first pair of those was a day's work.

| module | owns |
|---|---|
| `app/perception/identifiers.py` | **extraction** — mining surface strings from OCR text and titles. Pilot-hardened stoplists; its comments cite live misfires. |
| `app/services/context/keys.py` | **binding grammar** over that output (`from_identifiers`) — what is an identity, what it is worth, whether it may bind or name. |
| `app/services/context/surfaces.py` | **title segments → entity/person**, bind-only. Never mints. Also `from_heading` for bare VLM page titles (no trailing app segment). |
| `app/services/context/frames.py` | **segmentation** — a pure function over an ordered stream. No clock reads, no DB. |
| `app/services/context/episodes.py` | root frame + everything nested under it. |
| `app/services/context/replay.py` | stream builder, timeline, CLI. Falls back to `vlm_title` / `vision.title` when `window` is blank. |
| `app/services/context/bindings.py` | **cached binding lookup** — LRU + TTL. A stale MISS costs an escalation; a stale HIT is wrong and invisible, so minting punches the cache. |
| `app/services/context/resolver.py` | **scoring and the decision bands** — hand-weighted log-linear, 0.30 supporting clamp. Pure: no DB, no model. |
| `app/services/context/escalate.py` | **constrained-choice escalation and the ratchet** — the model returns an index or null; a resolved escalation mints a binding. |
| `app/services/context/propagate.py` | **one damped hop**, asserted/derived only. Takes candidates, never produces them. |
| `app/services/context/compounding.py` | key spread/IDF demotion, evidence independence, new-project detection. |
| `app/services/context/evaluate.py` | boundary F1, attribution, confusions, the compounding metric, and the §14 loop: `sheet` writes a day to label, `score` grades a replay against it. |
| `app/services/context/pipeline.py` | **`associate()`** — the entry point. Model-free, shadow by default; an excluded event is never attributed at all. |
| `app/services/context/naming.py` | **the model reader, offline** — feeds `escalate.choose` the episodes the cheap path left blank: candidates are entities the stretch's own text mentions (no tools/places/ideas, no single-sighting mints), asked in both orders, agreement required. Names in memory only; never mints. |

Tables: `kg_node_keys` (extended into a binding table), `context_frames`,
`episodes`, `episode_events`, `event_context`, `context_decisions`.

**Nameable is not attributable.** `keys.SignalKey.nameable` answers "may this key
TITLE a stretch of work" — OCR-derived path/repo/domain/url keys may not, because
vision invents plausible identifiers. It must NOT gate whether a key may
ATTRIBUTE an event: an invented identifier resolves to nothing anyway, since the
binding table is the filter and a bound key was minted by a trusted source, a
confirmation, or the ratchet.

## How to measure

Replay a captured day. The segmenter is pure, so this is the same code path as
live capture, fed differently — and a day becomes a re-runnable fixture.

    python -m app.services.context.replay --day 2026-08-26
    python -m app.services.context.replay --day 2026-08-26 --persist

Without `--persist`, the store opens read-only (so a `:ro` pilot volume works).
`--persist` needs a writable data dir.

Label a day, then score it (§14). The sheet carries the system's own claim
beside two blank columns; `=` accepts the prediction, otherwise type the
project (case-insensitive) and `1` where a new stretch of work really starts.
A blank project means "no project", so an unfinished sheet scores as if its
blanks were unattributable — the report says how many are blank.

    python -m app.services.context.evaluate sheet --day 2026-08-26          # -> data/cal_eval/2026-08-26.csv
    python -m app.services.context.evaluate sheet --day 2026-08-26 --blind  # no predictions shown
    python -m app.services.context.evaluate score --labels data/cal_eval/2026-08-26.csv [--json]
    python -m app.services.context.evaluate score --labels data/cal_eval/2026-08-26.csv --escalate   # + the local model over blank episodes
    python -m app.services.context.evaluate fill  --labels data/cal_eval/2026-08-26.csv --from 10:31 --to 12:02 --project Boostrun
    python -m app.services.context.evaluate fill  --labels data/cal_eval/2026-08-26.csv --from 12:46 --to 13:20 --accept

`fill` labels a clock-time stretch in place and marks its first row as a
boundary (`--no-boundary` if it continues the previous stretch). A day is a
dozen stretches, so this is faster than a spreadsheet.

`data/` is gitignored, and the sheet holds real window titles and screen
text — keep it there. Showing the prediction biases the labeller toward it;
that is the design's trade (an afternoon, not a week), and `--blind` is the
control if the numbers look too good.

Hosted pilot seat (example — user3, densest day):

    docker run --rm --entrypoint python3 \
      -v gb10_sparrow-user3-data:/srv/sparrow/data:ro \
      -v "$PWD":/app:ro -w /app \
      -e QUILL_DATA_DIR=/srv/sparrow/data \
      sparrow-hosted -m app.services.context.replay --day 2026-09-06

Inspect a hosted pilot seat read-only (counts only, no content):

    docker run --rm --entrypoint python3 \
      -v gb10_sparrow-userN-data:/srv/sparrow/data:ro sparrow-hosted -c \
      'import json,sqlite3; d="/srv/sparrow/data"
       print(json.load(open(d+"/onboarding_profile.json")).get("identity"))
       c=sqlite3.connect("file:"+d+"/quill.db?mode=ro",uri=True)
       print(c.execute("SELECT COUNT(*) FROM entities").fetchone())'

## Testing

Golden streams in `tests/test_context_frames.py` are where the segmenter is
actually exercised: on one real 11-hour day its rules fire 0 times (forced
split), once (switch) and twice (freeze-on-idle). **Real capture cannot test
this state machine**; it can only say whether the thresholds feel right. Seed
synthetic stream shapes from measured statistics (p50 gap 4 s, median title run
one event) — inventing a tidy day of 40-minute blocks builds a segmenter that
works on a day nobody has.

Two bugs the real day exposed that synthetic had not: evidence accumulated
additively (an incumbent became arithmetically undisplaceable), and a promoted
excursion started before its parent closed (overlapping episodes). Both have
regression tests now. Keep both halves.

## Stages 3–5 — what is built, and what is deliberately not

**Stage 1's spine is built.** `bindings.py` (cached lookup) and
`pipeline.associate()` (normalize → extract → look up → score → write), plus
`event_context` and `context_decisions` so an attribution stores the reasoning
that produced it. `associate()` NEVER calls a model: an ambiguous event is
written `pending` and `escalate_pending()` runs elsewhere, because a slow local
model must not become a capture stall. Attribution is **shadow by default** —
measured, not surfaced. Warm deterministic path: 0.10 ms/event against a 25 ms
budget; cached key lookup 0.43 µs.

**Stage 3 is built and unexercised.** The resolver scores, bands and explains;
escalation asks a model for an index and ratchets the answer into a binding. But
nothing wires it into the capture path yet, and the reason is upstream: on the
pilot surface 5% of events carry any anchor, so the resolver would be handed
almost no candidates to choose between. **Wire it after anchor supply improves,
not before** — measuring a resolver on an empty candidate set teaches nothing.

**Escalation has a feed now, offline.** `naming.name_unbound` runs after a
replay over the episodes with no name, builds candidates from the text, and
calls `escalate.choose` in both orders. It is the model reader the design
describes, run as a measurement pass; wiring it live is the same call from the
pending-decision path once shadow numbers justify the spend.

**`resolver.train()` raises on purpose.** Fitting weights against a corpus the
system labelled itself learns its own prior back. `WEIGHTS` is an explicit,
readable prior until the hand-labelled day exists; `evaluate.labelling_sheet()`
produces that day's sheet.

**Two design corrections made while building:**

- *Belief is absolute, margin is relative.* §5.2 bands on `P(top1)` from a
  softmax, but with one candidate a softmax share is always 1.0 however thin the
  evidence — that binds a lone supporting-only guess at confidence 1.00. Bands
  now read `strength` = σ(Σ w·f) for "am I sure" and use `p` only for "is
  anything competing".
- *The §8.1 saturation formula is inert as written.* `min(1.0, 1 + 0.2·ln(n))`
  is exactly 1.0 for every n ≥ 1, flattening all evidence to its base weight.
  Implemented as logarithmic growth under a ceiling, which is the evident
  intent.

**Stage 5 is mostly satisfied or deferred, not skipped.** Multi-project
disjointness shipped in the resolver (`scored_multi`). Org-level propagation is
a caller pattern over `propagate` — person → org → project is two hops, which it
refuses by design. Cross-episode causal chains and learned segmentation both
need the labelled corpus; the design gates learned segmentation on "boundary F1
having a stable baseline to beat", so `evaluate.py` is that prerequisite rather
than a guess at the research.

## Known-broken, not ours to fix silently

- **The self node on the dev box — fixed 2026-09-14.** `user_identity()`
  returned `'User 2'` here (onboarding profile never filled), so
  `self_profile.self_person_id` resolved a placeholder while the real human sat
  in the graph as an ordinary contact. Setting `identity.name` in
  `data/onboarding_profile.json` (backup beside it) made `self_person_id` 2 and
  moved the labelled day from P 0.49 to P 1.00 at the same recall. All three
  active pilot seats had identity set from the start.
- **`test_capture_dock`'s web-state assertion** fails when run after the other
  capture modules (with or without the CAL changes). `web_ingest`'s WS
  connection state is module-global and something before it leaves it set.
- **Gmail `thread:` keys.** `exhaust_ingest` now carries Gmail's `threadId`, but
  there is nothing to attach a thread key to: that module deliberately writes
  one provenance event per source class, not per message.

## Open threads

1. **Anchor supply is the binding constraint**, not segmentation. On the pilot
   5% of events carry any anchor; episodes get named on coherence 0.04.
2. **VLM per-frame title is on the anchor path**, with guards: questions,
   share-chrome, and junk names (`Your team`, `Project X`, …) do not bind.
   OCR path/repo keys without `browser_url` vote but do not name. On
   user3's 2026-09-06 day that should collapse attributable time toward
   honest blanks — re-measure after any further tuning.
3. **The timeline is in the product.** Memory Console → Filters → Episodes
   (`GET /console/episodes`). Same segmenter as the CLI; blanks stay blank.
   Rebuild persists under `console:<day>`.
4. **Connectors are off for the pilot.** `GOOGLE_OAUTH_CLIENT_ID`/`SECRET` are
   unset in `deploy/hosted/gb10/.env`, and quick tunnels give ephemeral redirect
   URIs. So `thread:`/`calendar_uid:`/`fileId:` do not arrive for free.
5. **The first labelled day (2026-08-26, dev box, labelled 2026-09-14).**
   287 rows, 267 attributable, labelled by stretch with `evaluate fill`.
   Baseline: attribution P 0.49 / R 0.49, unbound 7%; boundary F1 1.00 — but
   the boundaries were labelled from the same stretch list the segmenter
   produced, so that number is agreement with itself, not a measurement.
   Every wrong attribution is one error class: the user's own name off
   Outlook titles (135 events), the self-node bug from Known-broken. With the
   self name excluded in memory only, P 1.00 / R 0.49, unbound 54%: the fix
   converts wrong labels into honest blanks and adds no recall. Recall on the
   three mail stretches (mnemos, Boostrun, Venture Pulse) needs more than
   titles. The sheet lives in `data/cal_eval/`, gitignored; regenerate with
   `--blind` for an unbiased boundary label.
6. **The model reader, measured (`score --escalate`, same day).** Two local
   calls over three blank episodes, 7 per 1000 events. Two stretches carried
   no screen text at all (this box captured no frames on Aug 26; clicks and an
   Outlook title only), so no reader can name them — that is capture, not
   CAL. The third held the pitch PDF. First run: candidates by mention count
   were `company`, `MVP`, `Mnemos Labs` and the 7B model returned null —
   correctly — and showed a position bias (null with `MVP` first, `Mnemos
   Labs` with `Mnemos Labs` first, deterministic). After excluding ideas and
   single-sighting mints and asking in both orders: it chose `Mnemos Labs`
   one way and `mnemos_v1` the other, so null. **Both are the labelled
   project.** The graph holds mnemos as three nodes (`mnemos` hidden,
   `Mnemos Labs`, `mnemos_v1`); the reader cannot pick between synonyms and
   should not. Merge or alias them and that stretch names itself.
