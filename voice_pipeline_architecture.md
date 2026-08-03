# Voice Pipeline Architecture — Audio → Trusted Evidence

*Roadmap item #13 — the recommended architecture, synthesizing items 1–12.*
*Visual companion: a rendered Artifact of this document exists (signal-chain diagram).*

The microphone used to produce **text**. It now produces **evidence**: every
utterance arrives carrying its capture quality, confidence, speaker, type, and a
full provenance chain — so the system knows not just *what* was said, but how much
to trust it and what to do about it. All 12 code items are shipped and validated on
real + synthetic audio.

---

## The signal chain — one utterance, mic to fact

Between the microphone and a stored, actionable fact, an utterance passes through
twelve stages. Each attaches a piece of evidence or makes a routing decision — and
none throws away what it can't yet trust.

| # | Stage | Module | What it adds | Flag / status |
|---|-------|--------|--------------|---------------|
| — | **Capture** | `audio.py` + Silero VAD | Segments the stream into `(audio, speech_start, speech_end)` | always |
| 01 | Quality score | `audio_quality.py` | Scores SNR / clipping / speech ratio *before* Whisper — tells "bad audio" from "Whisper failed". Recalibrated on 1,400 clips: loudness alone is not a defect. | `QUILL_AQ_*` · observe (skip_bad opt-in) |
| 02 | Denoise routing | `denoise.py` | Enhances only `noisy` audio; keeps the raw clip. **Finding: DSP denoise raises SNR but hurts Whisper**, so the built-in gate stays off the ASR path by default. | `QUILL_DENOISE_*` · eval-gated |
| 03 | Biased transcription | `vocabulary.py` + `audio.py` | Whisper with a session-aware `initial_prompt` from the KG (known names/projects + recent turns). Fixes "Abby Nagle" → "Abby Nengel" at the source. | `QUILL_ASR_BIAS` · on · eval-gated |
| 04 | Ingest verdict | `ingest_filter.py` | Structured verdict, not a boolean: `keep · keep_low · needs_review · store_audio_only · drop_hallucination`. Nothing real is silently lost. | always |
| 05 | Speaker attribution | `speakers.py` | Environment-adaptive cosine thresholds — a degraded profile lowers the accept bar but demands a bigger margin. | `QUILL_SPEAKER_ADAPTIVE` |
| 06 | Utterance type | `utterance_router.py` | `command · dictation · conversation · noise`, precision-first — conversation is the safe default. | `QUILL_UTTERANCE_ROUTER` · observe |
| 07 | Confidence contract | `confidence.py` | Separates *capture quality* from *model confidence* — facets a readiness score can weigh independently. | always |
| 08 | Provenance chain | `provenance.py` | Raw + enhanced audio + transcript + ASR prompt + an append-only correction log. | `QUILL_PROVENANCE` · on · `/console/provenance` |
| — | **Event persisted** | bus → `storage.py` | The utterance, now an evidence record, is published and stored; WAV clips linked. | — |
| 09 | Settled-turn consolidation | `consolidation.py` | Merges adjacent utterances into turns; `settled_turns()` is the one shared definition of "final". | always |
| 10 | Intent filter → extraction | `intent.py` + `extractor.py` | Skips zero-signal turns; extracts tasks/commitments/claims/entities/relations with a verbatim `source_span` (faithfulness-scored). | `QUILL_INTENT_ROUTER` |
| 11 | Readiness + two-signal offer | `readiness.py` + `task_offer.py` | One risk-aware score banded `auto/offer/review/hold`; an offer needs readiness **and** corroborating intent. | `QUILL_OFFER_TWO_SIGNAL` |
| — | **Action + approval → correction** | agent / `phone_link.py` + `provenance.py` | Grounding & drafting reuse the vocabulary; a send pauses for approval; human edits append back onto the source utterance's chain. | human gate |

**Cross-cutting.** `audio_telemetry` (capture: drop reasons, SNR, latency) and
`cog_telemetry` (judgement: faithfulness, source-grounding, offer surfaced/accept
rate) feed the Audio Health and cognition consoles. `scripts/eval_voice.py` gates
every change against a golden set — WER, entity recall, hallucination rate.

---

## What every transcript now carries

**Before:** `Event(text="text abby the demo tomorrow")` — just words.

**After — the evidence record:**

```
transcript      "text abby the demo tomorrow"
audio_quality   good · 14 dB SNR · 2% clip
confidence      capture 0.82 · model 0.71 · tier=extracted
ingest          keep_low_confidence
speaker         Abby? · unknown · profile=quiet_room
type            conversation
provenance      raw.wav + asr_prompt("Names: Abby Nengel…")
                ↳ 1 correction: Abby → Abby Nengel
```

---

## The decision layer — from evidence to action

All the evidence converges on one risk-aware **readiness** score → one **band**.
Risk raises the bar, not just the score.

| Band | Floor | What to do |
|------|-------|------------|
| `auto` | ≥ 0.85 (low-risk) | Do it without asking. Opt-in (`QUILL_AUTO_ACT`); ask-first by default. |
| `offer` | ≥ 0.60 (0.75 high-risk) | Surface a yes/no — the default for a solid task. |
| `review` | ≥ 0.30 | Keep as a reviewable item; don't nag. |
| `hold` | < 0.30 / blocked | Record for provenance; never surface. |

**The two-signal offer gate (#10).** Readiness alone let ambient talk become false
offers. An offer now needs two independent lines of evidence:

- **Signal 1 · Readiness** — "Are we sure enough about *what was said*?" (capture × model × risk)
- **Signal 2 · Intent** — "Is this actually a thing to *do*?" (a concrete transactional verb tied to a real object)

Result on the labeled A/B: **false-offer rate 1.00 → 0.00 at zero recall cost.**
"…I should tell Kristi it's fine" stays on the board; "text Abby the deck" surfaces.

---

## Five rules the architecture keeps

1. **Observe before you act** — every behavior-changing stage ships observational and
   flagged first, opt-in once trusted (the path #1/#3/#6 took).
2. **Never silently lose what's real** — a drop keeps an audio-only record; the ingest
   verdict flags shaky text rather than discarding. The only hard drop is a confident hallucination.
3. **General code, tailored by data** — no real names in `.py` logic; personalization flows
   through the knowledge graph at runtime (the same vocabulary biases ASR, extraction, grounding, approval).
4. **Prove it on real audio** — denoise-hurts-Whisper, quiet-speech false-drops, the false-offer
   class were each *caught by testing*, not guessed. The eval harness makes changes re-checkable.
5. **One definition, shared** — `settled_turns`, `risk_of`, `span_is_faithful`: a single source
   of truth each, so the pipeline can't disagree with itself.

*(And: confidence is a contract, not a number — capture/model/faithfulness/review are
carried as separate facets so a weak signal never masquerades as tacit approval.)*

---

## Proven, not assumed

| Result | Metric | Source |
|--------|--------|--------|
| 0.50 → 0.97 | entity recall from ASR bias, no hallucination induced | #3 · eval_voice A/B |
| 1.00 → 0.00 | false task-offer rate under the two-signal gate, 0 recall cost | #10 · labeled A/B |
| 702 → 0 | past transcripts replayed through the ingest verdict — zero new hard-drops | #7 · regression replay |
| 99% | real clips classified `good` after recalibrating on 1,400 WAVs | #1 · real-data calibration |
| 24 / 24 | utterance-router labels, every class P = R = 1.00 | #6 · classification eval |
| 14 / 14 | provenance chain checks against a live store | #12 · end-to-end |

---

## The recommendation — where it goes next

The twelve stages give the pipeline trustworthy inputs and a disciplined way to act.
The architecture's open edge is the **output** side — turning captured evidence into learning.

1. **Close the feedback loop (priority).** Provenance (#12) now captures every human
   correction on the exact utterance it fixes. Feed those corrections back into the
   models — ASR vocabulary weighting, extractor negatives, speaker profiles — so the
   system learns from its own edits. Provenance closed the *capture* side; conditioning
   closes it. Honors the invariant: learning lives in data/models, never in code.
2. **A learned denoiser on the ASR path (when installed).** The DSP gate hurts Whisper;
   DeepFilterNet may not. Wire it, then let the eval harness (#8) decide — per environment,
   per backend — whether it earns a place on the transcription path.
3. **Promote routing from observe to act.** The utterance router (#6) already stamps every
   transcript. Once trusted in production, promote it: dictation skips extraction and is
   stored verbatim; commands take a fast path to the agent.

---

*Env flags default to their safe / observational setting. Behavior changes are opt-in:*
`QUILL_AQ_SKIP_BAD` · `QUILL_UTTERANCE_ROUTE` · `QUILL_AUTO_ACT` · `QUILL_EXTRACT_VOCAB`.
