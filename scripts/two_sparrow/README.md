# Two-Sparrow interaction test

Two isolated Sparrow seats ("Testco": Alice, product lead on Project Heron,
and Bob, sales) are fed two halves of a synthetic two-week corpus, paired
over the peer channel, and handed tasks that can only be finished with
information the other seat holds. A stub approver plays both humans. The
scorer grades what crossed the channel against `ground_truth.json`.

## Pieces

| file | what |
|---|---|
| `gen_corpus.py` | seeded generator: ~150 emails in 20 threads (3 stale), 13 meeting transcripts, ~300 chat messages in 4 channels + 1 DM thread, 15 docs, 13 calendar events (3 rescheduled). Plants 20+20 asymmetric facts, 8 contradictions, 6 stale loops, two Chrises and a Mike, 3 bait messages, 2 sensitive items. Emits `timeline.json` + `ground_truth.json`. |
| `run.py setup` | onboarding profiles, peer pairing (pack `pilot`), a one-peer team `#testco` on each seat, and (unless `--no-org`) a Testco org on the Org Record Service that both seats join. |
| `run.py ingest` | replays the timeline into each seat's half through `POST /replay/items` (app/api/replay_routes.py, `QUILL_REPLAY_INGEST=1` only) at `--speed` x real time (default 672x = two weeks in 30 min) so recency and decay are exercised. `--instant` dumps it. `--swap` gives Alice's half to bob's seat. |
| `run.py check` | read-only dry run: facts by kind, open tasks, and whether each seat can find its OWN planted facts. |
| `run.py tasks` | T1-T8 on both seats, bob starting `--stagger` seconds later (default 600). `--swap` runs Alice's tasks on bob's seat. `--only T1,T6` narrows. |
| `approver.py` | the scripted human: approve 70% / deny 20% / leave 10% unanswered, on inbound peer asks, agent approval packets (hash-bound decide) and proposed records (live-session cookie). Also logs every peer round trip it sees. |
| `run.py chaos` | stops bob's container seconds into Alice's T1 peer query, records what Alice does, restarts bob. |
| `score.py` | metrics from `flow.jsonl` + each seat's `peer_telemetry.jsonl` / `model_calls.jsonl` + `/claims`. |

Seats come from `deploy/hosted/testco/docker-compose.yml` (own image tag,
own volumes, ports 8007/8008), tokens in `deploy/hosted/testco/.env`.

## Run order

```bash
python scripts/two_sparrow/gen_corpus.py --out data/two_sparrow/corpus
cd deploy/hosted/testco && docker compose -p testco up -d --build && cd -
RUN=run-$(date +%F)-a
python scripts/two_sparrow/run.py setup  --run $RUN
python scripts/two_sparrow/run.py ingest --run $RUN            # ~30 min, extraction continues after
python scripts/two_sparrow/run.py check  --run $RUN            # wait until facts stop growing first
python scripts/two_sparrow/approver.py   --run $RUN &          # keep running through tasks + chaos
python scripts/two_sparrow/run.py tasks  --run $RUN
python scripts/two_sparrow/run.py chaos  --run $RUN
python scripts/two_sparrow/score.py      --run $RUN
# role swap: fresh volumes, then ingest --swap, tasks --swap, score --swap
```

Everything lands under `data/two_sparrow/<run>/` (`flow.jsonl`, `state.json`,
`check.json`, `tasks.json`, `chaos.json`, `score.json`).

## What the product does and does not do (so the scores read right)

* The chat agent never queries a peer on its own. Cross-seat flow happens
  only through the team syntax `ask #testco: ...` (team_layer), the slot
  null path, or the user pressing a peer_tell suggestion. T1/T4/T5/T6 run a
  plain prompt first (expect "I don't have that") and then the explicit ask.
* Peer answers under the `pilot` pack are automatic for `availability` and
  `work`, offered to the human for `contact`/`personal`/`other`; `personal`
  is never automatic. The approver's decision table only sees the offered
  ones.
* Records: a bearer caller can propose but never approve; the approver uses
  the browser session cookie + CSRF header for `/packets/{id}/decide`. Both
  seats must have joined the org or `/claims` reports `joined: false`.
* `AGENT_DRY_RUN=approval` on these seats (the pilot runs `draft`): a send
  stops at a hash-bound packet the approver decides.
