"""Train, export and package one Sparrow 1.0 candidate.

QLoRA on the HF base (default unsloth/Qwen2.5-14B-Instruct, the weights
behind the pilot's qwen2.5:14b-instruct) using the repo's own trainer,
scripts/lora_train_wsl.py, in its own venv (~/.mnemos-lora — unsloth pins a
torch/transformers stack the app must not share). Each candidate trains from
the BASE on the full accumulated set, not on top of the last candidate, so a
bad round cannot compound.

Export leaves the merged 16-bit model in ~/.mnemos-runs/<tag>/ — that
directory IS the 1.0 base a per-user live layer later fine-tunes from, so it
is kept. The GGUF is packaged into Ollama with the base tag's chat template
copied verbatim (train_lora.build_modelfile), so the app talks to it exactly
as it talks to the base.

The GPU is shared with every hosted seat on this box. A file lock stops two
Sparrow 1.0 trainings overlapping; evicting the seats' loaded models is opt-in
(--evict-ollama) because it stalls their chat.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

LORA_PY = Path.home() / ".mnemos-lora" / "bin" / "python"
TRAINER = ROOT / "scripts" / "lora_train_wsl.py"
RUNS = ROOT / "data" / "sparrow1" / "runs"
MERGED_ROOT = Path.home() / ".mnemos-runs"
LOCK = ROOT / "data" / "sparrow1" / "gpu.lock"

BASE_HF = os.environ.get("SPARROW1_BASE_HF", "unsloth/Qwen2.5-14B-Instruct")
BASE_TAG = os.environ.get("SPARROW1_BASE_TAG", "qwen2.5:14b-instruct")


@contextlib.contextmanager
def gpu_lock(path: Path = LOCK):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"[train] another Sparrow 1.0 training holds {path}")
        f.write(f"{os.getpid()} {time.time():.0f}\n")
        f.flush()
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _trainer_cmd(train_jsonl: Path, out: Path, stage: str, hp: dict) -> list[str]:
    return [str(LORA_PY), str(TRAINER), "--train", str(train_jsonl),
            "--out", str(out), "--base-hf", hp["base_hf"],
            "--epochs", str(hp["epochs"]), "--lr", str(hp["lr"]),
            "--rank", str(hp["rank"]), "--max-seq", str(hp["max_seq"]),
            "--batch", str(hp["batch"]), "--grad-accum", str(hp["grad_accum"]),
            "--quant", hp["quant"], "--stage", stage]


def package(gguf: Path, tag: str, *, template_from: str = BASE_TAG) -> None:
    import train_lora as tl
    template = tl._capture(["ollama", "show", "--template", template_from])
    params = tl._capture(["ollama", "show", "--parameters", template_from])
    if not template.strip():
        raise SystemExit(f"[package] could not read the chat template of "
                         f"{template_from} — is Ollama up?")
    mf = gguf.parent / "Modelfile"
    mf.write_text(tl.build_modelfile(gguf.name, template, params),
                  encoding="utf-8")
    subprocess.run(["ollama", "create", tag, "-f", str(mf)],
                   cwd=str(gguf.parent), check=True)


def train(train_jsonl: Path, tag: str, *, base_hf: str = BASE_HF,
          template_from: str = BASE_TAG, epochs: float = 2.0,
          lr: float = 1e-4, rank: int = 32, max_seq: int = 4096,
          batch: int = 1, grad_accum: int = 8, quant: str = "q4_k_m",
          evict_ollama: bool = False, log=print) -> dict:
    if not LORA_PY.exists():
        raise SystemExit(f"[train] no training venv at {LORA_PY} — see "
                         "scripts/sparrow1/README.md")
    out = RUNS / tag
    out.mkdir(parents=True, exist_ok=True)
    hp = {"base_hf": base_hf, "epochs": epochs, "lr": lr, "rank": rank,
          "max_seq": max_seq, "batch": batch, "grad_accum": grad_accum,
          "quant": quant}
    env = {**os.environ, "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}
    t0 = time.time()
    with gpu_lock():
        for stage in ("train", "export"):
            if stage == "train" and (out / "adapter" / "adapter_config.json").is_file():
                log(f"[train] {tag}: adapter exists, skipping to export")
                continue
            if evict_ollama:
                import train_lora as tl
                tl.free_gpu_from_ollama()
            log(f"[train] {tag}: stage {stage} ...")
            with (out / f"{stage}.log").open("w", encoding="utf-8") as lf:
                proc = subprocess.run(_trainer_cmd(train_jsonl, out, stage, hp),
                                      cwd=str(ROOT), env=env, stdout=lf,
                                      stderr=subprocess.STDOUT)
            if proc.returncode != 0:
                tail = (out / f"{stage}.log").read_text(encoding="utf-8")[-2000:]
                raise SystemExit(f"[train] {stage} failed (exit "
                                 f"{proc.returncode}):\n{tail}")
        ggufs = sorted(out.glob("*.gguf"), key=lambda p: p.stat().st_mtime)
        if not ggufs:
            raise SystemExit(f"[train] no GGUF in {out}")
        log(f"[train] {tag}: packaging {ggufs[-1].name} into Ollama")
        package(ggufs[-1], tag, template_from=template_from)
    # unsloth's GGUF scratch (~9 GB) duplicates the copy in the run dir, which
    # is the shippable artifact; the merged 16-bit dir is kept (the base).
    scratch = MERGED_ROOT / f"{out.name}_gguf"
    if scratch.is_dir():
        shutil.rmtree(scratch, ignore_errors=True)
    merged = MERGED_ROOT / out.name
    info = {"tag": tag, "gguf": str(ggufs[-1]), "adapter": str(out / "adapter"),
            "merged_dir": str(merged) if merged.is_dir() else None,
            "hp": hp, "train_rows": str(train_jsonl),
            "minutes": round((time.time() - t0) / 60, 1)}
    (out / "candidate.json").write_text(json.dumps(info, indent=1))
    log(f"[train] {tag} ready in {info['minutes']} min")
    return info
