# Hosted (Boost Run) deployment — Web Perceive

One container per user; the browser is the only capture surface
(`QUILL_HEADLESS=1`). Everything below maps to the Web Perceive plan's
Phase 3.

## The moving parts

| Piece | Where |
|---|---|
| Browser capture page | `GET /capture` (mic + meeting-tab audio, enrollment) |
| Audio ingest | `WS /ingest/audio` — auth enforced in-handler |
| Client-side VAD (Phase 4) | `/static/vad_worker.js` + `GET /capture/vad-model` + `GET /capture/config`; onnxruntime-web baked into `/static/ort/` by the Dockerfile |
| Headless guard | `QUILL_HEADLESS=1` — local devices never open, `/capture/resume` 503s toward `/capture` |
| ASR warm-up | `QUILL_ASR_WARMUP=1` (default under headless) — model loads at boot, not on the first utterance |

## Checklist per user

1. Copy the service block in `docker-compose.yml`; give it a unique name,
   volume, host port, and `QUILL_API_TOKEN`.
2. Route a per-user subdomain (or `/u/<user>/`) to the container port at the
   reverse proxy, **with TLS** — `getUserMedia` requires a secure context.
3. First visit: `/auth` unlock with the token → `/capture` → opt in → talk.
   The "last heard" ticker doubles as the ASR sanity check.

## GPU ASR

Swap the base image for a CUDA one (see Dockerfile header), run with the
NVIDIA runtime, and set:

```
QUILL_WHISPER_DEVICE=cuda
QUILL_WHISPER_COMPUTE=float16
QUILL_WHISPER_MODEL=large-v3        # or distil-large-v3
```

Quality gates re-tune per engine id automatically (`whisper:<model>` keys the
ingest calibration; the confidence scale is unchanged, so defaults apply
until a per-model calibration exists).

## Local text tier

Point every container at one Ollama/vLLM on the box:
`QUILL_TEXT_LOCAL=1`, `QUILL_OLLAMA_URL=http://host.docker.internal:11434`.
Escalation ladder, distill trail and shadow eval work unchanged.

## Per-user LoRA adapters (personal model weights)

Each container can grow its own fine-tune of the shared base model from its
user's verdicts/edits — the same idle-trainer pipeline as desktop, retargeted
for the hosted posture:

1. **Opt in per container**: `QUILL_IDLE_TRAIN=1` plus a unique
   `QUILL_LORA_TAG_SUFFIX` (e.g. `user1`). The suffix scopes every tag this
   container publishes (`qwen2.5-mnemos-user1-YYYYMMDD`) AND its retention
   pruning — one user's cleanup can never remove another user's model.
2. **Idleness is capture-quiet**: under `QUILL_HEADLESS=1` the trainer's
   "user is idle" probe becomes *seconds since the last ingested event* and
   the AC-power check is skipped. Everything else holds: ≥150 new labeled
   pairs, ≥7 days between runs, disk headroom, failure backoff, and the
   exemplar-saturation gate (weights train only after retrieval stops
   improving).
3. **Training runs natively** (no WSL): `scripts/train_lora.py` needs the
   NVIDIA runtime + unsloth (`python scripts/train_lora.py --setup` once per
   volume, `--check` to verify). Point `OLLAMA_HOST` at the shared Ollama so
   `ollama create/show/list` land on the box every container serves from.
4. **Promotion stays an offer**: a gate win (challenger beats the
   exemplar-augmented incumbent on the user's own holdout) posts the flip
   line in that user's chat; deploying is
   `QUILL_TEXT_LOCAL_MODEL=<tag>` on that container + restart. Rollback is
   the same line with the base tag.

Trainer pairs carry the router's confidence contract (system trailer +
`CONFIDENCE: 0.NN` target line) — an adapter trained before this fix answered
without the trailer, parsed as "unsure" on every reply, and auto-escalated
itself into a regression.

**Cold-start bootstrap (automatic)**: a fresh user has far fewer than the
100-pair green light, so the idle trainer closes the gap itself: once the
profile's memory graph has substance (≥10 facts) and a quiet window opens,
it distills parent-model answers to questions grounded in that user's OWN
memory (scripts/synthetic_pairs.py, once per install), then fires the FIRST
training run on the combined volume — no manual step. Synthetic stays
quarantined: train-only, capped at 3x real pairs, never in holdout, so the
promotion gate still judges purely on human-verified rows, and every run
after the first requires organic label growth again. Opt out with
QUILL_SYNTH_BOOTSTRAP=0.

VRAM note: training a 7B QLoRA wants ~16GB free; pause that user's heavy
chat use or size the GPU for train+serve concurrency (train_lora frees
Ollama's resident models before each stage).

## Do NOT ship meeting-first mode to hosted instances

The desktop tester build pins `QUILL_PROFILE=tester` + `QUILL_FIRST_RUN_MODE=meeting`
(the mic only ingests inside calendar meeting windows until the user takes the
"keep listening between meetings" opt-in). `audio.web_mic` rides the same mic
channel, so a hosted container with that posture **silently discards all web
capture** — utterances are dropped before telemetry, and Audio Health shows
zeros with no "dropped" counts to hint why. The Dockerfile pins
`QUILL_FIRST_RUN_MODE=full` for exactly this reason; don't override it from a
copied desktop `.env`. If a hosted tier ever wants meeting-first, the
onboarding flow must put the between-meetings unlock card front and center.

## Known limits (accepted for the pilot)

- `_transcribe_loop` is per-container and blocking — fine to ~20 users on one
  GPU (bursty load); the scale path is a shared ASR sidecar.
- Tab-audio capture is Chromium-only; other browsers get mic-only mode with
  a headphones notice.
- Client-side VAD needs `/static/ort/` (baked here) or the pinned CDN; when
  neither loads the page silently streams to server-side VAD — capture never
  breaks, the page just drops the "silence stays local" badge.
