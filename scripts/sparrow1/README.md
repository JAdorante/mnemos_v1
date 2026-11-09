# Sparrow 1.0 base model: the recursive training loop

Every user's Sparrow starts from one base model, and each user's live layer
(`app/services/idle_trainer.py`) later fine-tunes it on their own data. This
directory trains that base. It teaches the model Sparrow's job before it has
seen anyone's data:

- file what lands (extraction, project routing, people)
- find it again
- answer from it honestly

It uses synthetic worlds that Claude writes and grades.

## One round

| step | what | file |
|---|---|---|
| worlds | Claude writes fresh fake orgs: people, projects, ~30 emails/chats/meetings/docs/calendar items, notes to file, and ~16 questions with answer keys. Scenario skills are re-weighted toward last round's failures. | `world.py` |
| rollout | Each world runs in its own subprocess with an isolated `QUILL_DATA_DIR`, through the real pipeline, with the current model as the local model. A wrapper on `OllamaText.complete` records every local call exactly as sent. | `rollout.py` |
| grade | Claude grades each call against the world's key and writes the right output for that exact input. Project routing is graded by code. | `grade.py` |
| rows | Corrections plus on-policy rehearsal rows, in the inference contract of `ollama_text.py`. | `pairs.py` |
| train | QLoRA from the base on every round's rows, packaged as `sparrow-1.0-rN` in Ollama. | `train.py` |
| eval / promote | The candidate and the incumbent run on frozen holdout worlds that never feed training. Promotion needs at least +0.02 overall and no task falling more than 0.05. | `loop.py` |

How the rollout runs the real pipeline:

- Items land via `scheduler.land_unseen` and are mined by `extractor.run_text_once`.
- Notes go through `projects.classify`.
- Questions go through the chat lane's own functions: `planner.compile`, then `agent_bridge._make_memory_provider`, then `LLM.direct_answer`.

## Run it

```bash
python scripts/sparrow1/loop.py holdout --n 3          # once: frozen eval worlds
python scripts/sparrow1/loop.py eval                   # baseline the incumbent (qwen2.5:14b-instruct)
python scripts/sparrow1/loop.py run --rounds 3 --worlds 6
python scripts/sparrow1/loop.py status
```

`run` repeats this loop: round, then train, then eval, then promote. Each step is also its own command (`round`, `train`, `eval --model T`, `promote --model T`).

State lives in `data/sparrow1/state.json`. A half-finished round reuses any world that is already graded.

## Rules the loop keeps

- **Training rows are recorded, not rebuilt.** A row's system prompt and messages are what the app sent. The target follows `training_contract`: a `CONFIDENCE: 0.90` trailer on text, and JSON keys in schema order with an injected `confidence` last.
- **Corrections answer the input the model saw.** When retrieval missed the evidence, the right target is "I don't have that", not a fact taken from the answer key. Chat scores are split into `chat_with_evidence` (a model problem) and `chat_retrieval_missed` (a pipeline problem).
- **Clock per item.** During extraction, `clock.now_local` is pinned to each item's arrival time, so "by Friday" in a week-old email resolves the way live capture would.
- **No cloud from rollouts.** `ModelRouter._call_parent` raises, so the router keeps the student's answer, and `ANTHROPIC_BASE_URL` points at a dead local port. The repo `.env` is not loaded. The child runs code defaults plus the hosted seats' settings.
- **Each candidate trains from the base** on the full accumulated set, so a bad round cannot compound.

## Setup (done once on the GB10, 2026-11-08)

- **Training venv:** `~/.mnemos-lora`. It runs on a uv-managed Python 3.12, because the system Python has no `Python.h` and Triton needs it. It has unsloth 2026.11.3 and torch 2.14.1+cu130. It is kept apart from `.venv` because unsloth pins its own torch and transformers.
- **Weights:** `unsloth/Qwen2.5-14B-Instruct` (16-bit, needed for the merge) and `-bnb-4bit` are in `~/.cache/huggingface`.
- **Output:** the merged 16-bit model of each candidate stays in `~/.mnemos-runs/<tag>/`. That directory is the HF base the per-user live layer should fine-tune from. To adopt it there:
  - add the tag to `HF_BASES` in `scripts/train_lora.py`; the string must contain "qwen", or loss falls back to whole-text
  - set `QUILL_TEXT_LOCAL_MODEL` on the seat
  - mount the directory into the container

## Shared GPU

The six hosted seats share this GPU through one Ollama:

- `train.py` takes a file lock so two 1.0 trainings never overlap.
- It does not stop user1's idle trainer. That trainer runs in a container and is gated to rarely run.
- `--evict-ollama` unloads the seats' models before each stage. It is off by default because it stalls their chat. Without it, training and seat inference share the GPU and both slow down.

## Cost

The teacher is `claude-opus-5-5` (`SPARROW1_TEACHER_MODEL` overrides it), with the server-side refusal fallback on.

- A world costs about $0.6 to write.
- Grading costs about $0.03 per local call. A 30-item world makes about 60 calls.
- Every call is logged in `data/sparrow1/teacher_calls.jsonl`, and `status` shows the total.
