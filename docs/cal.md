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
| `app/services/context/surfaces.py` | **title segments → entity/person**, bind-only. Never mints. |
| `app/services/context/frames.py` | **segmentation** — a pure function over an ordered stream. No clock reads, no DB. |
| `app/services/context/episodes.py` | root frame + everything nested under it. |
| `app/services/context/replay.py` | stream builder, timeline, CLI. |

Tables: `kg_node_keys` (extended into a binding table), `context_frames`,
`episodes`, `episode_events`.

## How to measure

Replay a captured day. The segmenter is pure, so this is the same code path as
live capture, fed differently — and a day becomes a re-runnable fixture.

    python -m app.services.context.replay --day 2026-08-26
    python -m app.services.context.replay --day 2026-08-26 --persist

`--persist` writes under a `run_id`, so the same day can be re-segmented under
different tuning and compared instead of clobbered.

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

## Known-broken, not ours to fix silently

- **The self node on the dev box.** `user_identity()` returns `'User 2'` here
  (onboarding profile never filled), so `self_profile.self_person_id` resolves a
  placeholder while the real human sits in the graph as an ordinary contact.
  Everything keyed on self points at the wrong node. All three active pilot
  seats have identity set correctly — this is a dev-box data problem.
- **`test_capture_dock`'s web-state assertion** fails when run after the other
  capture modules (with or without the CAL changes). `web_ingest`'s WS
  connection state is module-global and something before it leaves it set.
- **Gmail `thread:` keys.** `exhaust_ingest` now carries Gmail's `threadId`, but
  there is nothing to attach a thread key to: that module deliberately writes
  one provenance event per source class, not per message.

## Open threads

1. **Anchor supply is the binding constraint**, not segmentation. On the pilot
   5% of events carry any anchor; episodes get named on coherence 0.04.
2. **Unused signal:** `vlm.describe` returns a per-frame `title` (`vlm.py:165`);
   `desktop_capture.py:344` uses it only to build a summary string and nothing
   downstream sees it. 102 of 230 events on the measured pilot day had VLM
   output. It is model output — medium strength at most, and it must never mint
   a binding, or the layer-separation rule that prevents self-confirmation drift
   is broken.
3. **The timeline is not in the product.** It is a CLI. Wiring the segmenter to
   live capture and surfacing a page is the retention artifact.
4. **Connectors are off for the pilot.** `GOOGLE_OAUTH_CLIENT_ID`/`SECRET` are
   unset in `deploy/hosted/gb10/.env`, and quick tunnels give ephemeral redirect
   URIs. So `thread:`/`calendar_uid:`/`fileId:` do not arrive for free.
5. **No labelled eval set.** Nothing here can be tuned on measurement until one
   real day is hand-labelled with project and boundary.
