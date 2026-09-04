# ASR eval fixtures

The acceptance gate for the perception upgrade. Every engine swap — Whisper →
Parakeet, and anything after it — is decided by `scripts/eval_asr.py` run against
this directory, on the Windows reference machine.

The reason this exists rather than a leaderboard number: published ASR WER is
measured on read speech recorded close to a good microphone. Sparrow hears a
laptop mic in a room with a fan, two people talking over each other, and a
meeting coming back through WASAPI loopback. An engine can win LibriSpeech and
lose here. **If Parakeet loses on these fixtures, we keep Whisper** — and this
harness is still the most useful thing the project got out of the attempt.

## Running it

```bash
python scripts/eval_asr.py bootstrap                    # synthetic probes only
python scripts/eval_asr.py check                        # validate before a long run
python scripts/eval_asr.py run --tag whisper-baseline --speakers
python scripts/eval_asr.py run --engine parakeet-onnx --tag parakeet --speakers
python scripts/eval_asr.py compare data/eval/asr/report_whisper-baseline.json \
                                   data/eval/asr/report_parakeet.json --gate
```

`bootstrap` writes silence / hiss / keystroke / tone probes plus (on Windows) a
few SAPI-TTS lines, so the harness is runnable before any real audio exists. The
probes are genuinely useful — they are exactly the audio Whisper hallucinates
"Thank you." over. The TTS clips are **plumbing smoke tests, not domain WER**;
they are read speech, which is the thing this fixture set exists to not be.

## What to record

Target ~60 minutes of speech plus ~10 minutes of no-speech probes.

| Category | What | Roughly |
|---|---|---|
| `close_mic` | headset / close laptop mic, one speaker, dictation and commands | 10 min |
| `laptop_meeting` | 2–4 people around a laptop, cross-talk, real interruptions | 20 min |
| `far_field` | speaker 2–4 m from the mic, room reverb | 10 min |
| `fan_noise` | any of the above with HVAC / fan / street noise | 10 min |
| `loopback` | meeting audio captured through WASAPI loopback, not the mic | 10 min |
| `no_speech` | silence, music, typing, notification sounds, a TV in the next room | 10 min |

`loopback` is first-class, not an afterthought: the loopback pipeline is a second
`AudioPipeline` instance with different timing and a different audio
distribution, and a Windows audio-stack surprise there is a known risk.

Audio must be **16 kHz mono WAV** — the sample rate the live path runs at.
Resample once when you add the clip rather than in the harness, so what is
scored is what the pipeline would have received.

```bash
ffmpeg -i raw.m4a -ac 1 -ar 16000 tests/fixtures/asr_eval/clips/meeting_01.wav
```

## Consent and scrubbing

Recordings come from consenting team members and design partners, and get
scrubbed before they land here: no customer names, no credentials, no personal
details that would matter if this repo leaked. If a clip is only usable with the
sensitive part cut, cut it and shorten the reference transcript to match. A
fixture set nobody is nervous about is one people will actually keep extending.

## Manifest schema

`manifest.jsonl`, one JSON object per line. Paths are relative to this directory.

```json
{
  "id": "meeting_01",
  "audio": "clips/meeting_01.wav",
  "category": "laptop_meeting",
  "channel": "mic",
  "expect_speech": true,
  "reference": "the full ground-truth transcript of the clip",
  "entities": ["Abby", "Villanova"],
  "utterances": [
    {"start": 1.20, "end": 4.85, "speaker": "Justin", "text": "..."},
    {"start": 5.10, "end": 7.40, "speaker": "Abby",   "text": "..."}
  ],
  "notes": "free text — recording conditions, anything odd"
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | unique; names the row in every report |
| `audio` | yes | 16 kHz mono WAV, relative path |
| `category` | yes | one of the categories above |
| `channel` | no | `mic` (default) or `loopback`; picks the speaker-ID space |
| `expect_speech` | no | defaults to `category != "no_speech"` |
| `reference` | if speech | full transcript; **must be empty** for no-speech clips |
| `entities` | no | names that must survive ASR (entity recall) |
| `utterances` | no | per-utterance ground truth; unlocks boundary + attribution |
| `notes` | no | free text |

`utterances` is where the Phase C work gets its evidence. Without it a clip
scores WER and hallucination only; with `start`/`end` it scores segmentation
(fused / split / boundary error), and with `speaker` as well it scores
attribution error under `--speakers`. Timings need to be good to roughly ±100 ms
— that is the overlap tolerance the scorer uses — so label them in an editor,
not by ear.

## What the numbers mean

- **`wer`** — the headline, but read `by_category` first. A win on `close_mic`
  that loses on `laptop_meeting` is not a win for this product.
- **`raw_hallucination_rate` vs `post_filter_hallucination_rate`** — what the
  engine emitted on no-speech audio, and what survived `ingest_filter` to become
  a memory. The gap is how much work the filter is doing. An engine that stops
  hallucinating at the source lets us loosen a filter that currently discards
  some real speech to catch ghosts.
- **`rtf`** — real-time factor over the whole set. Below 1.0 means the engine
  keeps up with a live stream on this machine.
- **`offline_utterance_ms_p90`** — quality + denoise + ASR + filter + speaker ID.
  It is a **floor** for the §1 "utterance-end → TranscriptEvent ≤ 400 ms p90"
  target, not a measurement of it: queue wait and publish only exist on the live
  path. Those come from `audio_telemetry` / `latency_spans.jsonl` on a running
  system.
- **`fused_rate`** — share of VAD segments spanning two ground-truth speakers.
  A fused segment yields one embedding for two people and cannot be attributed
  correctly however good the speaker stack is. If this is near zero and
  attribution is still wrong, segmentation was not the problem.

## Ground rules

- Clips are **not** committed to git (see `.gitignore` here) — they are audio of
  real people, and the repo is not the right place for them. Keep the shared set
  where the team already keeps recordings; the manifest is what is versioned.
- Never regenerate a reference transcript from an engine's own output. A
  reference derived from Whisper measures agreement with Whisper, not accuracy.
- Add clips, don't swap them. Baselines only compare across time if the set is
  append-only; when a clip must be retired, retire it and rerun the baseline.
