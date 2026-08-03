# Phase 3 LoRA Architecture — Lessons Into Weights

*Phases 1–2 compensate for the local model. Phase 3 makes the model absorb the lessons.*

Phases 1–2 gave you a system that compensates for the local model: retrieval
whispers worked examples into its prompt, calibration decides when to trust it,
the bench measures it. Phase 3 makes the model itself absorb those lessons — a
LoRA fine-tune of `llama3.2` on accumulated `(prompt → verified answer)` pairs,
run as an idle-time job. After it, behaviors taught one example at a time become
the model's default: no examples needed in the prompt, no context spent, no
retrieval dependency. Few-shot remains the fast path for new lessons; LoRA is
how lessons graduate into permanence.

This is also the endgame of the core invariant: **user-specificity lives first
in data (the JSONL), then in weights (the adapter) — and never in the `.py`
files.** The code that runs `llama3.2-mnemos` is byte-identical to the code that
runs stock `llama3.2`.

---

## Where Phase 3 sits in the learning loop

| Phase | What it does | When it helps | Artifact |
|-------|--------------|---------------|----------|
| **1 · Few-shot** | Retrieve similar accepted/edited distill rows into the local system prompt | Same day a label lands | `app/services/few_shot.py` |
| **2 · Bench + calibration** | Score local replies vs human gold; tune escalate confidence | Continuously; gates every change | `scripts/bench_text.py` |
| **3 · LoRA** | Fine-tune an adapter on curated clean pairs | Periodic, after ~100–300 pairs | adapter + Ollama tag |

```
escalate → label → few-shot (same day) → LoRA (periodic)
        → fewer escalations → remaining escalations are hard cases
        → next cycle's best training data
```

---

## The pipeline, stage by stage

### 1. Curate — `scripts/distill_curate.py` (built)

Walks `escalate_distill.jsonl` and builds the training set:

| Rule | Why |
|------|-----|
| Keep `modality=text`, outcome `accepted` or `edited`, with full-fidelity `meta.system` / `meta.messages` | Pre–full-fidelity rows lack a replayable clean prompt; the training era effectively starts when fidelity landed |
| Target = human `edited` text when present, else accepted parent output | Strongest signal first |
| Train on the **clean** stored system/messages | Never the few-shot-augmented local prompt — otherwise the model learns to need the crutch |
| Drop known stub parent answers | Test harness noise (`{"tasks": ["stub"]}`, etc.) |
| Exclude the bench holdout via `bench_text.in_holdout()` | A row the gate scores on must never be trained on |
| Dedupe near-identical prompt focus (embedding similarity) | Avoid overweighting one repeated failure mode |
| Optionally upweight edited rows (2–3×) | A human correction beats a rubber-stamped Claude answer |
| Flag perishable-looking targets (dates/meetings) | Prefer form/behavior over facts that go stale in weights |

```bash
python scripts/distill_curate.py                         # readiness report
python scripts/distill_curate.py --json
python scripts/distill_curate.py --write data/lora/train.jsonl --upweight-edited 3
```

**Readiness bands** (printed by the script):

- **accumulating** — under ~100 train pairs (do not train yet)
- **critical_mass** — ≥100 (first light LoRA run is justified)
- **ready** — ≥300 (solid first training set)

### 2. Train — Unsloth under WSL2 — `scripts/lora_train_wsl.py` (built)

On this Windows machine the practical route is Unsloth under WSL2 — Windows-native
training tooling is rough; WSL2 gets the standard Linux CUDA stack while sharing
the GPU.

Ballpark config (deliberately light touch):

- Base: `llama3.2` 3B
- LoRA rank 8–16
- Small learning rate, 1–3 epochs
- Optional mix-in of a small slice of generic instruction data

**Risk at a few hundred examples:** catastrophic forgetting — sanding away the
base model's general instruction-following while teaching your patterns. Low rank
+ few epochs + optional generic mix keeps the base intact.

Training runs minutes-to-an-hour at this scale, scheduled when the machine is
idle (the GPU also serves vision capture — the job should check for activity,
same spirit as the reflection loop).

### 3. Package — GGUF + Ollama Modelfile — in `scripts/train_lora.py` (built)

The trainer exports one MERGED GGUF (not a separate adapter file — one
artifact, one `ollama create`, no llama.cpp adapter-conversion dependency),
and the Modelfile copies TEMPLATE/PARAMETERS verbatim from the base tag via
`ollama show`, so the fine-tune behaves identically to the base except for
what it learned:

```
FROM ./model-q4_k_m.gguf
TEMPLATE """…copied from base…"""
PARAMETER stop "<|im_end|>"
```

Tag with a date: `qwen2.5-mnemos-20260801`. The base tag and every prior
tag stay installed; rollback is pointing config back.

### 4. Gate — holdout bench comparison — in `scripts/train_lora.py` (built)

The challenger runs the Phase 2 bench in holdout mode:

```bash
python scripts/bench_text.py --mode holdout --model llama3.2-mnemos-20260801
```

**Promotion requires beating the incumbent on the same holdout:**

| Metric | Requirement |
|--------|-------------|
| Pass rate | ≥ incumbent |
| Mean similarity | ≥ incumbent |
| Would-escalate rate | **lower** |
| Conf-high / sim-low quadrant | **zero new** confidently-wrong rows |

No promotion without numbers, ever. Same discipline that killed a sensible-looking
calibration change in one bench run.

### 5. Deploy

```bash
# .env
QUILL_TEXT_LOCAL_MODEL=llama3.2-mnemos-20260801
```

Restart. Nothing else changes — router, few-shot, calibration, and distill logging
all keep operating on the new champion. The next cycle's training data starts
accumulating immediately.

### 6. Recalibrate

A fine-tuned model's self-reported confidence distribution shifts. After
promotion, the bench's per-row conf/sim data should sanity-check
`QUILL_TEXT_ESCALATE_MIN_CONF` — a better model may deserve a lower bar, or may
become overconfident and need a higher one. The data to decide is already in the
bench output.

---

## What has to be true first

The honest gate is **data volume**: ~100–300 curated pairs before the first
training run is worth the electricity. A useful LoRA on a 3B model genuinely
emerges around there; at a handful of rows you'd be fine-tuning on noise.

**Labels are the rate limiter.** The chat-verdict UX (`👍` / `👎` / `✏️` and
`scripts/distill_label.py`) matters more than any training code until the
curator reports critical mass. The right mix happens naturally: mostly the tasks
you actually use, because the data is your usage.

---

## Risks worth naming

1. **Stale facts in weights.** An accepted answer containing "your next meeting
   is Tuesday 9am" becomes permanently baked knowledge. Prefer pairs that teach
   *form and behavior* (phrasing, format, how to handle a note-to-self) over
   perishable facts — the curator flags likely perishable targets; labeling
   wisdom enforces the rest.
2. **Privacy.** The adapter file is personal data in weight form. It stays on
   this machine, under `data/`, and never ships — same policy as the JSONL
   (`scripts/data_audit.py` classifies the trail as personal).
3. **Eval erosion.** As the labeled set grows, the holdout must grow with it.
   Eventually a time-based split (train on past, eval on recent) beats the hash
   split, because it measures what matters: does the model handle next week's
   traffic.

---

## Deliberately out of scope

- Vision fine-tuning (`minicpm-v` LoRA tooling is immature; distill rows
  intentionally carry no image bytes)
- Cloud training (privacy plus needless complexity)
- Replacing the escalation policy with a learned model (calibration already
  covers the tractable part; the trail keeps logging if that's revisited)

---

## Concrete build order

| # | Piece | Status |
|---|-------|--------|
| 1 | `scripts/distill_curate.py` — curation + readiness report | **built** |
| 2 | `scripts/lora_train_wsl.py` — Unsloth QLoRA in WSL2, merged-GGUF export | **built** |
| 3 | Modelfile packaging → dated Ollama tag (`train_lora.py` stage) | **built** |
| 4 | Gate: holdout bench vs incumbent → promote/reject (`train_lora.py` stage) | **built** |
| 5 | One-time WSL env: `sudo apt install python3.12-venv python3-pip` in the distro, then `train_lora.py --setup` | manual step |

The full run is one command once data hits critical mass:

```bash
python scripts/train_lora.py --check     # preflight: WSL GPU, unsloth, ollama, pair count
python scripts/train_lora.py             # curate -> train -> package -> gate (refuses <100 pairs)
```

Promotion gate (all four, plus at least one strict improvement): pass_rate ≥,
mean_sim ≥, would_escalate ≤, and no growth in the confidently-wrong quadrant
(stays-local at sim < 0.4). The script never edits .env — it prints the flip
and rollback lines.

Base since 2026-07-17: `qwen2.5:7b-instruct` (championed over llama3.2 by
bench: pass 0.66 vs 0.47) — HF mapping lives in `train_lora.HF_BASES`.

Curation doubles as a data-quality report — run it while labeling to watch the
training set grow.

---

## Invariant checklist

1. **No user-specific logic in `.py`** — personality and domain live in JSONL,
   then in the adapter.
2. **Clean prompts only** — few-shot is a runtime crutch; training must not
   depend on it.
3. **Shared holdout** — `in_holdout()` is the one split train and gate both use.
4. **Numbers before promotion** — pass, similarity, escalate rate, zero new
   confident-wrong.
5. **Rollback is a config flip** — prior tags stay installed.

---

*Related: `scripts/distill_label.py` · `scripts/bench_text.py` ·
`app/services/few_shot.py` · `voice_pipeline_architecture.md` (same doc style
for the voice stack).*
