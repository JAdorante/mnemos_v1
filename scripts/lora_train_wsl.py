"""Phase 3 LoRA trainer — the Linux half (runs INSIDE WSL2, not on Windows).

Invoked by scripts/train_lora.py via `wsl -d <distro> ...`; can also be run by
hand inside any Linux box with a CUDA GPU. Fine-tunes a LoRA adapter on the
curated (clean prompt -> human-verified answer) pairs distill_curate.py wrote,
then exports ONE merged GGUF ready for `ollama create`.

    python3 lora_train_wsl.py --train /mnt/c/.../data/lora/train.jsonl \
        --out /mnt/c/.../data/lora/runs/<tag> --base-hf unsloth/Qwen2.5-7B-Instruct

Design choices (see phase3_lora_architecture.md):
  * QLoRA (4-bit base) — a 7B fits comfortably in 16GB VRAM.
  * Low rank / few epochs — at a few hundred examples the risk is sanding away
    general instruction-following, not underfitting.
  * Completions-only loss where the chat family is known (the model learns to
    ANSWER, not to reproduce prompts); whole-text loss as the safe fallback.
  * Merged GGUF export (not an adapter file): one artifact, one `ollama create`,
    no llama.cpp adapter-conversion dependency.

Generic code: every training pair comes from this install's own curated trail.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

# --- pure helpers (importable on Windows for tests; no heavy deps) -----------

# Chat-family -> (instruction marker, response marker) for completions-only
# training. Matched against the HF base name, case-insensitive.
_FAMILY_MARKERS = {
    "qwen": ("<|im_start|>user\n", "<|im_start|>assistant\n"),
    "llama-3": ("<|start_header_id|>user<|end_header_id|>\n\n",
                "<|start_header_id|>assistant<|end_header_id|>\n\n"),
}


def response_markers_for(base_hf: str) -> tuple[str, str] | None:
    """Markers that delimit user/assistant turns in the base's chat template,
    or None (-> whole-text loss) for an unrecognized family."""
    low = (base_hf or "").lower().replace("_", "-").replace("llama-3.2", "llama-3")
    for family, markers in _FAMILY_MARKERS.items():
        if family in low:
            return markers
    return None


def load_rows(path: Path) -> list[dict]:
    """Curated training records (distill_curate.to_example shape)."""
    rows = []
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
        if not ln.strip():
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def to_conversation(ex: dict) -> list[dict] | None:
    """One training record -> chat messages ending with the verified answer.
    None when the record can't teach (no messages or no target)."""
    target = str(ex.get("target") or "").strip()
    messages = [m for m in (ex.get("messages") or [])
                if str(m.get("content") or "").strip()]
    if not target or not messages:
        return None
    conv = []
    system = str(ex.get("system") or "").strip()
    if system:
        conv.append({"role": "system", "content": system})
    conv.extend({"role": m.get("role", "user"), "content": m["content"]}
                for m in messages)
    conv.append({"role": "assistant", "content": target})
    return conv


# --- training (heavy imports stay inside; Linux/CUDA only) -------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--train", type=Path, default=None,
                    help="curated JSONL (required for --stage train/all)")
    ap.add_argument("--out", required=True, type=Path,
                    help="output dir for the merged GGUF (+ checkpoints)")
    ap.add_argument("--base-hf", required=True,
                    help="HF base, e.g. unsloth/Qwen2.5-7B-Instruct")
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--max-seq", type=int, default=2048)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--quant", default="q4_k_m",
                    help="GGUF quantization (q4_k_m matches Ollama defaults)")
    ap.add_argument("--seed", type=int, default=3407)
    # Two-stage on purpose: GGUF merge (4-bit -> 16-bit) inside the process
    # that just trained is VRAM-fragile (observed: cudaErrorUnknown at save).
    # `train` writes <out>/adapter; `export` merges in a FRESH process.
    ap.add_argument("--stage", choices=["train", "export", "all"], default="all")
    args = ap.parse_args()

    if args.stage in ("train", "all"):
        if args.train is None:
            raise SystemExit("--train is required for --stage train/all.")
        do_train(args)
    if args.stage in ("export", "all"):
        do_export(args)
    # Skip CUDA context teardown entirely. On WSL2 it crashed one run
    # (cudaErrorUnknown at interpreter exit) and HUNG the next for 14 hours —
    # both AFTER every artifact was safely on disk. There is nothing left to
    # clean up that a process exit doesn't; leave without looking back.
    import os
    import sys
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def do_train(args) -> None:
    rows = load_rows(args.train)
    convs = [c for c in (to_conversation(r) for r in rows) if c]
    if len(convs) < 10:
        raise SystemExit(f"only {len(convs)} usable pairs in {args.train} — "
                         "run distill_curate.py --write first (need >=10).")
    random.Random(args.seed).shuffle(convs)
    print(f"[train] {len(convs)} pairs  base={args.base_hf}  rank={args.rank}  "
          f"epochs={args.epochs}  lr={args.lr}")

    from unsloth import FastLanguageModel                       # noqa: E402
    from datasets import Dataset                                # noqa: E402
    from trl import SFTConfig, SFTTrainer                       # noqa: E402

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.base_hf,
        max_seq_length=args.max_seq,
        load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=args.rank,
        lora_alpha=args.rank,
        lora_dropout=0.0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth",
        random_state=args.seed,
    )

    texts = [tokenizer.apply_chat_template(c, tokenize=False,
                                           add_generation_prompt=False)
             for c in convs]
    # Drop over-length examples EXPLICITLY: silent truncation cuts off the
    # assistant answer, leaving all-masked labels the trainer then discards
    # anyway (observed live: 56/75 vanished at max_seq 2048). Saying so beats
    # a mystery shrink — and says exactly what --max-seq would recover.
    lens = [len(ids) for ids in tokenizer(texts)["input_ids"]]
    keep = [i for i, n in enumerate(lens) if n <= args.max_seq]
    if len(keep) < len(texts):
        print(f"[train] WARNING: dropping {len(texts) - len(keep)}/{len(texts)} "
              f"examples over --max-seq {args.max_seq} tokens "
              f"(longest={max(lens)}) — raise --max-seq to keep them.")
        texts = [texts[i] for i in keep]
    if len(texts) < 10:
        raise SystemExit(f"only {len(texts)} examples fit --max-seq "
                         f"{args.max_seq} — raise it (longest={max(lens)}).")
    print(f"[train] {len(texts)} examples within {args.max_seq} tokens "
          f"(median≈{sorted(lens)[len(lens) // 2]}).")
    dataset = Dataset.from_dict({"text": texts})

    # SFTConfig, not TrainingArguments: trl >= 0.20 reads max_length /
    # dataset_text_field from the CONFIG. The old kwargs were silently ignored
    # (observed live: the dataset tokenized at the 1024 default, every longer
    # example lost its truncated-off answer, and only the 19 shortest trained).
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        args=SFTConfig(
            output_dir=str(args.out / "checkpoints"),
            dataset_text_field="text",
            max_length=args.max_seq,
            per_device_train_batch_size=args.batch,
            gradient_accumulation_steps=args.grad_accum,
            num_train_epochs=args.epochs,
            learning_rate=args.lr,
            lr_scheduler_type="linear",
            warmup_steps=2,
            logging_steps=5,
            optim="adamw_8bit",
            weight_decay=0.01,
            seed=args.seed,
            save_strategy="no",
            report_to="none",
        ),
    )

    markers = response_markers_for(args.base_hf)
    if markers:
        from unsloth.chat_templates import train_on_responses_only  # noqa: E402
        trainer = train_on_responses_only(
            trainer, instruction_part=markers[0], response_part=markers[1])
        print(f"[train] completions-only loss (markers for {args.base_hf}).")
    else:
        print("[train] WARNING: unknown chat family — whole-text loss fallback.")

    stats = trainer.train()
    print(f"[train] done: loss={stats.training_loss:.4f}")

    adapter_dir = args.out / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    print(f"[train] adapter -> {adapter_dir}")


# A real 7B Q4 GGUF is ~4.7GB; anything under this is a partial/failed write.
_MIN_GGUF_BYTES = 1_000_000_000


def _find_ggufs(out: Path) -> list[Path]:
    """GGUFs for an output dir — unsloth may write to `<out>` OR a sibling
    `<out>_gguf` dir depending on version. Newest last."""
    hits = list(out.glob("*.gguf"))
    hits += list((out.parent / (out.name + "_gguf")).glob("*.gguf"))
    return sorted(hits, key=lambda p: p.stat().st_mtime)


def do_export(args) -> None:
    """Merge the trained adapter into the base and quantize to ONE GGUF.

    Runs in its own process (fresh CUDA context for the 4-bit -> 16-bit
    merge), and converts on the WSL-NATIVE filesystem first: streaming a
    multi-GB GGUF straight through /mnt/c silently truncated (observed: a
    "successful" 66MB file). The finished file is then copied to the Windows
    out dir with a size check."""
    import shutil
    adapter_dir = args.out / "adapter"
    if not (adapter_dir / "adapter_config.json").is_file():
        raise SystemExit(f"no trained adapter at {adapter_dir} — "
                         "run --stage train first.")
    scratch = Path.home() / ".mnemos-runs" / args.out.name
    scratch.mkdir(parents=True, exist_ok=True)

    from unsloth import FastLanguageModel                       # noqa: E402
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=str(adapter_dir),          # base + adapter resume
        max_seq_length=args.max_seq,
        load_in_4bit=True,
    )
    model.save_pretrained_gguf(str(scratch), tokenizer,
                               quantization_method=args.quant)

    good = [p for p in _find_ggufs(scratch)
            if p.stat().st_size >= _MIN_GGUF_BYTES]
    if not good:
        raise SystemExit("export produced no full-size GGUF in WSL scratch "
                         f"({scratch}) — check the llama.cpp output above.")
    src = good[-1]
    dest = args.out / src.name
    print(f"[export] copying {src.stat().st_size / 1e9:.2f}GB GGUF to "
          f"{dest} …")
    shutil.copyfile(src, dest)
    if dest.stat().st_size != src.stat().st_size:
        raise SystemExit(f"[export] copy size mismatch: {dest} — /mnt/c "
                         "write failed; GGUF remains in WSL at "
                         f"{src} (copy it manually).")
    print(f"[export] GGUF -> {dest}")


if __name__ == "__main__":
    main()
