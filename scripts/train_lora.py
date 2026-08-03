"""Phase 3 LoRA pipeline — curate -> train (WSL2) -> package -> gate.

One command turns the labeled distill trail into a candidate local model and
tells you — with numbers — whether it beats the incumbent:

    python scripts/train_lora.py --check         # preflight (env + data readiness)
    python scripts/train_lora.py --setup         # install unsloth in the WSL venv
    python scripts/train_lora.py                 # full run (refuses under --min-pairs)
    python scripts/train_lora.py --force         # train anyway (small-data experiment)
    python scripts/train_lora.py --skip-train    # re-package/gate an existing run

Stages (phase3_lora_architecture.md):
  1. CURATE   distill_curate writes data/lora/train.jsonl — holdout excluded,
              stubs dropped, near-dupes deduped, edited rows upweighted.
  2. TRAIN    scripts/lora_train_wsl.py under `wsl -d <distro>` (Unsloth QLoRA,
              merged GGUF export). GPU is shared with Windows via WSL2.
  3. PACKAGE  Modelfile = FROM <gguf> + TEMPLATE/PARAMETERS copied verbatim from
              the base tag (`ollama show`), then `ollama create <tag>`.
  4. GATE     bench_text --mode holdout for incumbent AND challenger; promotion
              requires: pass_rate >=, mean_sim >=, would_escalate <=, no new
              confidently-wrong rows, and at least one strict improvement.

The script NEVER edits .env — it prints the flip line and the rollback line.
Generic code: model names come from config/args; all training data is this
install's own curated trail.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

TRAIN_JSONL = ROOT / "data" / "lora" / "train.jsonl"
RUNS_DIR = ROOT / "data" / "lora" / "runs"
WSL_VENV = "$HOME/.mnemos-lora"          # venv inside the distro (--setup)

# A row that stayed local with sim below this is "confidently wrong" — the
# quadrant a fine-tune must not grow (promotion gate condition #4).
CONF_WRONG_SIM = 0.4

# Ollama tag -> HF base for training. Extend as bases are adopted; unknown
# tags need an explicit --base-hf.
HF_BASES = {
    "qwen2.5:7b-instruct": "unsloth/Qwen2.5-7B-Instruct",
    "qwen2.5:7b": "unsloth/Qwen2.5-7B-Instruct",
    "llama3.2": "unsloth/Llama-3.2-3B-Instruct",
    "llama3.2:3b": "unsloth/Llama-3.2-3B-Instruct",
    "llama3.2:1b": "unsloth/Llama-3.2-1B-Instruct",
}


# --- pure helpers (unit-tested in tests/test_train_lora.py) ------------------
def wsl_path(win: str | Path) -> str:
    """C:\\Users\\x\\repo -> /mnt/c/Users/x/repo (how WSL sees Windows disks)."""
    p = Path(win).resolve()
    drive = p.drive.rstrip(":").lower()
    return "/mnt/" + "/".join([drive, *p.parts[1:]])


def hf_base_for(tag: str) -> str | None:
    return HF_BASES.get((tag or "").strip().lower())


def default_tag(base: str, date: str | None = None) -> str:
    """qwen2.5:7b-instruct -> qwen2.5-mnemos-YYYYMMDD (dated; prior tags stay
    installed, so rollback is pointing config back)."""
    stem = (base or "model").split(":")[0].replace("/", "-")
    return f"{stem}-mnemos-{date or time.strftime('%Y%m%d')}"


def build_modelfile(gguf_name: str, template: str, params_text: str) -> str:
    """Modelfile for a merged GGUF that behaves exactly like the base tag:
    template and parameters are copied verbatim from `ollama show`."""
    lines = [f"FROM ./{gguf_name}"]
    if template.strip():
        lines += ["", 'TEMPLATE """' + template.rstrip("\n") + '\n"""']
    for ln in (params_text or "").splitlines():
        ln = ln.strip()
        if ln:
            lines.append("PARAMETER " + re.sub(r"\s+", " ", ln, count=1))
    return "\n".join(lines) + "\n"


def merged_artifacts(out_dir: Path) -> list[Path]:
    """The merged full-model intermediates the export stage leaves behind
    (~14GB/run at 7B). They exist only to feed GGUF quantization and are
    regenerable from the saved adapter with --skip-train, so after a
    successful `ollama create` they are dead weight. The adapter (the real
    trained artifact) and the GGUF (resume shortcut) are NOT included."""
    pats = ("model-*.safetensors", "pytorch_model-*.bin",
            "consolidated*.safetensors")
    out: list[Path] = []
    for p in pats:
        out += out_dir.glob(p)
    return sorted(out)


def cleanup_merged(out_dir: Path) -> int:
    """Delete merged-model intermediates; return bytes freed. Best-effort."""
    freed = 0
    for p in merged_artifacts(out_dir):
        try:
            size = p.stat().st_size
            p.unlink()
            freed += size
        except OSError as exc:
            print(f"[package] could not remove {p.name}: {exc}")
    return freed


_MNEMOS_TAG_RE = re.compile(r"-mnemos-\d{8}")


def parse_ollama_list(text: str) -> list[str]:
    """Tag names from `ollama list` output (first column, header skipped)."""
    out = []
    for ln in (text or "").splitlines()[1:]:
        cols = ln.split()
        if cols:
            out.append(cols[0])
    return out


def tags_to_prune(tags: list[str], live: str, keep: int = 2) -> list[str]:
    """Which -mnemos- tags to `ollama rm`. Each fine-tune tag is a ~4.7GB blob
    in Ollama's store, so scheduled retraining accumulates them without bound
    unless pruned. Keep the `keep` newest (by date suffix) plus whatever tag is
    live in config; everything else matching -mnemos-YYYYMMDD goes. Non-mnemos
    tags (the base, vision models…) are never touched. Pure — testable."""
    norm = lambda t: (t or "").removesuffix(":latest")
    mnemos = [t for t in tags if _MNEMOS_TAG_RE.search(norm(t))]
    newest = sorted(mnemos, key=norm, reverse=True)[:max(0, keep)]
    kept = {norm(t) for t in newest} | {norm(live)}
    return [t for t in mnemos if norm(t) not in kept]


def prune_ollama_tags(live: str, keep: int = 2) -> list[str]:
    """Remove superseded fine-tune tags from Ollama's blob store. Best-effort."""
    tags = parse_ollama_list(_capture(["ollama", "list"]))
    doomed = tags_to_prune(tags, live, keep=keep)
    removed = []
    for t in doomed:
        r = subprocess.run(["ollama", "rm", t], capture_output=True)
        if r.returncode == 0:
            removed.append(t)
            print(f"[prune] ollama rm {t} (superseded fine-tune, ~4.7GB)")
        else:
            print(f"[prune] could not rm {t} — skipped")
    return removed


def prune_run_dirs(runs_dir: Path, kept_tags: set[str]) -> list[str]:
    """Delete run dirs of pruned tags (each holds an adapter, ~0.35GB). A run
    dir survives only while its tag does — same retention, one policy."""
    import shutil

    norm = lambda t: (t or "").removesuffix(":latest")
    kept = {norm(t) for t in kept_tags}
    removed = []
    for d in sorted(runs_dir.iterdir()) if runs_dir.is_dir() else []:
        if not d.is_dir() or not _MNEMOS_TAG_RE.search(d.name):
            continue
        if d.name in kept:
            continue
        try:
            shutil.rmtree(d)
            removed.append(d.name)
            print(f"[prune] removed run dir {d.name}")
        except OSError as exc:
            print(f"[prune] could not remove {d.name}: {exc}")
    return removed


def confidently_wrong(rows: list[dict]) -> int:
    return sum(1 for r in rows
               if not r.get("would_escalate")
               and float(r.get("sim") or 0.0) < CONF_WRONG_SIM)


def gate_decision(champ: dict, chall: dict) -> tuple[bool, list[str]]:
    """Promotion verdict from two bench results ({overall, rows}).

    All four conditions must hold AND at least one must be a strict
    improvement — a challenger that merely ties the incumbent everywhere is
    not worth a model switch."""
    co, no = champ["overall"], chall["overall"]
    c_cw, n_cw = confidently_wrong(champ.get("rows") or []), \
        confidently_wrong(chall.get("rows") or [])
    checks = [
        ("pass_rate", no["pass_rate"], co["pass_rate"],
         no["pass_rate"] >= co["pass_rate"], no["pass_rate"] > co["pass_rate"]),
        ("mean_sim", no["mean_sim"], co["mean_sim"],
         no["mean_sim"] >= co["mean_sim"], no["mean_sim"] > co["mean_sim"]),
        ("would_escalate", no["would_escalate_rate"], co["would_escalate_rate"],
         no["would_escalate_rate"] <= co["would_escalate_rate"],
         no["would_escalate_rate"] < co["would_escalate_rate"]),
        ("confidently_wrong", n_cw, c_cw, n_cw <= c_cw, n_cw < c_cw),
    ]
    reasons = [f"{name}: {new} vs {old} "
               f"{'OK' if ok else 'FAIL'}{' (improved)' if strict else ''}"
               for name, new, old, ok, strict in checks]
    promote = all(c[3] for c in checks) and any(c[4] for c in checks)
    if not any(c[4] for c in checks):
        reasons.append("no strict improvement anywhere — tie is not promotion")
    return promote, reasons


# --- subprocess plumbing -----------------------------------------------------
def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(cmd))
    return subprocess.run(cmd, **kw)


def _capture(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True)
    out = r.stdout or b""
    try:
        text = out.decode("utf-8")
    except UnicodeDecodeError:
        text = out.decode("utf-16-le", errors="replace")   # wsl.exe quirk
    return text if r.returncode == 0 else ""


def pick_distro(preferred: str | None) -> str | None:
    """First WSL distro that can see the GPU (nvidia-smi)."""
    candidates = [preferred] if preferred else ["Ubuntu", "NVIDIA-Workbench"]
    for d in candidates:
        if not d:
            continue
        probe = _capture(["wsl", "-d", d, "--", "sh", "-c",
                          "command -v nvidia-smi >/dev/null && nvidia-smi -L"])
        if "GPU" in probe:
            return d
    return None


def wsl_python(distro: str) -> str:
    """The venv python from --setup when present, else system python3."""
    have = _capture(["wsl", "-d", distro, "--", "sh", "-c",
                     f"test -x {WSL_VENV}/bin/python && echo yes"])
    return f"{WSL_VENV}/bin/python" if "yes" in have else "python3"


def run_bench(model: str, *, pct: int) -> dict | None:
    """bench_text --mode holdout --json for one model; parsed summary+rows."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench_text.py"),
         "--model", model, "--mode", "holdout", "--pct", str(pct), "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(r.stderr[-2000:] if r.stderr else "(no stderr)")
        return None
    try:
        # stdout carries loader chatter ([embed] …) before the JSON block —
        # parse from the first brace to the last.
        text = r.stdout or ""
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start:end + 1])
    except Exception:
        print(f"[gate] could not parse bench output for {model}.")
        return None


# --- stages ------------------------------------------------------------------
def stage_curate(args) -> int:
    import distill_curate as dc
    from app.config import settings
    rows = dc.load_all_text(Path(settings.escalate_log.path))
    stats = dc.curate(rows, holdout_pct=args.holdout_pct,
                      dedupe_sim=args.dedupe_sim,
                      upweight_edited=args.upweight_edited)
    n = stats["train_pairs"]
    print(f"[curate] {n} train pairs (holdout {stats['holdout_n']} excluded, "
          f"readiness: {stats['readiness']})")
    if n < args.min_pairs and not args.force:
        raise SystemExit(
            f"[curate] {n} < --min-pairs {args.min_pairs}: keep labeling "
            "(every chat verdict adds a pair) or rerun with --force for a "
            "small-data experiment.")
    TRAIN_JSONL.parent.mkdir(parents=True, exist_ok=True)
    dc.write_jsonl(TRAIN_JSONL, stats["weighted"])
    print(f"[curate] wrote {len(stats['weighted'])} examples -> {TRAIN_JSONL}")
    return n


def free_gpu_from_ollama() -> None:
    """Unload whatever Ollama has resident before training. WSL shares the GPU
    with Windows, and a loaded 7B chat model starves the trainer (observed:
    step-0 'no GPU memory for fused cross entropy'). Non-destructive — models
    reload on their next use; best to pause heavy app use during training."""
    out = _capture(["ollama", "ps"])
    names = [ln.split()[0] for ln in out.splitlines()[1:] if ln.split()]
    for name in names:
        print(f"[train] freeing GPU: ollama stop {name}")
        subprocess.run(["ollama", "stop", name], capture_output=True)
    if names:
        time.sleep(3)          # give the driver a beat to release VRAM


def stage_train(args, distro: str, out_dir: Path, *,
                skip_train: bool = False) -> Path:
    """Train (unless resuming) and export — SEPARATE WSL processes: the
    4-bit -> 16-bit merge needs a fresh CUDA context (in-process merge after
    training died with cudaErrorUnknown). GPU is re-freed before each stage —
    the app may have reloaded Ollama models in between. With `skip_train`,
    resume from whatever exists: a GGUF skips everything; a saved adapter
    skips straight to export."""
    def base_cmd() -> list[str]:
        py = wsl_python(distro)
        return ["wsl", "-d", distro, "--",
                "env", "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True", py,
                wsl_path(ROOT / "scripts" / "lora_train_wsl.py"),
                "--train", wsl_path(TRAIN_JSONL),
                "--out", wsl_path(out_dir),
                "--base-hf", args.base_hf,
                "--epochs", str(args.epochs), "--lr", str(args.lr),
                "--rank", str(args.rank), "--max-seq", str(args.max_seq),
                "--batch", str(args.batch),
                "--grad-accum", str(args.grad_accum),
                "--quant", args.quant]

    def ggufs() -> list[Path]:
        # Also check unsloth's sibling "<out>_gguf" dir, and ignore partial
        # writes (a real 7B Q4 is ~4.7GB; /mnt/c once left a 66MB corpse).
        hits = list(out_dir.glob("*.gguf"))
        hits += list((out_dir.parent / (out_dir.name + "_gguf")).glob("*.gguf"))
        return sorted((p for p in hits if p.stat().st_size > 1_000_000_000),
                      key=lambda p: p.stat().st_mtime)

    if skip_train and ggufs():
        print(f"[train] resuming — GGUF already present: {ggufs()[-1].name}")
        return ggufs()[-1]

    adapter_done = (out_dir / "adapter" / "adapter_config.json").is_file()
    if skip_train and adapter_done:
        print("[train] resuming from saved adapter — export only.")
    else:
        free_gpu_from_ollama()
        if _run(base_cmd() + ["--stage", "train"]).returncode != 0:
            raise SystemExit("[train] WSL training failed — see output above "
                             "(missing deps? run --setup first).")
    free_gpu_from_ollama()
    if _run(base_cmd() + ["--stage", "export"]).returncode != 0:
        raise SystemExit("[train] GGUF export failed — see output above. "
                         "The trained adapter is saved; retry with "
                         "--skip-train after fixing.")
    if not ggufs():
        raise SystemExit(f"[train] no GGUF in {out_dir}.")
    return ggufs()[-1]


def stage_package(args, gguf: Path, tag: str) -> None:
    template = _capture(["ollama", "show", "--template", args.base])
    params = _capture(["ollama", "show", "--parameters", args.base])
    if not template.strip():
        raise SystemExit(f"[package] `ollama show --template {args.base}` gave "
                         "nothing — is the base tag installed?")
    modelfile = gguf.parent / "Modelfile"
    modelfile.write_text(build_modelfile(gguf.name, template, params),
                         encoding="utf-8")
    print(f"[package] Modelfile -> {modelfile}")
    r = _run(["ollama", "create", tag, "-f", str(modelfile)], cwd=str(gguf.parent))
    if r.returncode != 0:
        raise SystemExit("[package] ollama create failed — see output above.")
    print(f"[package] created tag {tag}")


def stage_gate(args, tag: str) -> tuple[bool, list[str]]:
    print(f"[gate] holdout bench: incumbent {args.base} vs challenger {tag}")
    champ = run_bench(args.base, pct=args.holdout_pct)
    chall = run_bench(tag, pct=args.holdout_pct)
    if not champ or not chall:
        raise SystemExit("[gate] bench failed — challenger NOT promoted.")
    promote, reasons = gate_decision(champ, chall)
    print("\n[gate] " + ("PROMOTE" if promote else "KEEP INCUMBENT"))
    for line in reasons:
        print("  " + line)
    if promote:
        print("\nTo deploy (then restart the server):")
        print(f"  .env: QUILL_TEXT_LOCAL_MODEL={tag}")
        print(f"Rollback: QUILL_TEXT_LOCAL_MODEL={args.base}")
        print("After a day of use, sanity-check QUILL_TEXT_ESCALATE_MIN_CONF "
              "against the new model's conf/sim rows (bench --json).")
    else:
        print(f"\nChallenger tag {tag} stays installed for inspection; "
              "config is unchanged.")
    return promote, reasons


# --- preflight / setup -------------------------------------------------------
def cmd_check(args) -> None:
    distro = pick_distro(args.distro)
    print(f"WSL GPU distro : {distro or 'NONE FOUND (install/repair WSL2 + '}"
          f"{'' if distro else 'NVIDIA driver)'}")
    if distro:
        py = wsl_python(distro)
        print(f"WSL python     : {py}")
        uns = _capture(["wsl", "-d", distro, "--", "sh", "-c",
                        f"{py} -c 'import unsloth' 2>/dev/null && echo yes"])
        print(f"unsloth        : {'installed' if 'yes' in uns else 'MISSING — run --setup'}")
    ollama = _capture(["ollama", "--version"])
    print(f"ollama         : {ollama.strip() or 'MISSING'}")
    base = args.base
    print(f"base tag       : {base} "
          f"(hf: {args.base_hf or hf_base_for(base) or 'UNKNOWN — pass --base-hf'})")
    import distill_curate as dc
    from app.config import settings
    rows = dc.load_all_text(Path(settings.escalate_log.path))
    stats = dc.curate(rows, holdout_pct=args.holdout_pct,
                      dedupe_sim=1.0)          # exact dedupe: no embedder load
    print(f"train pairs    : {stats['train_pairs']} "
          f"(readiness: {stats['readiness']}, need >= {args.min_pairs})")


def cmd_setup(args) -> None:
    distro = pick_distro(args.distro)
    if not distro:
        raise SystemExit("no GPU-visible WSL distro found.")
    print(f"[setup] creating venv + installing unsloth in {distro} "
          "(first run downloads several GB)…")
    r = _run(["wsl", "-d", distro, "--", "sh", "-c",
              f"python3 -m venv {WSL_VENV} && "
              f"{WSL_VENV}/bin/pip install -U pip && "
              f"{WSL_VENV}/bin/pip install unsloth"])
    if r.returncode != 0:
        raise SystemExit(
            "[setup] failed. Inside the distro, install the venv module first "
            "(`sudo apt-get update && sudo apt-get install -y python3-venv "
            "python3-pip`), then rerun --setup.")
    print("[setup] done — run --check to verify.")


# --- main --------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="preflight only")
    ap.add_argument("--setup", action="store_true",
                    help="install unsloth into the WSL venv")
    ap.add_argument("--base", default=None,
                    help="incumbent Ollama tag (default: configured local model)")
    ap.add_argument("--base-hf", default=None,
                    help="HF model for training (default: mapped from --base)")
    ap.add_argument("--tag", default=None,
                    help="challenger tag (default: <base>-mnemos-<date>)")
    ap.add_argument("--distro", default=None, help="WSL distro override")
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    # 4096: chat rows carry fat grounding contexts — at 2048, 56/75 examples
    # lost their (truncated-off) answer on the first live run. batch 1 keeps
    # the longer window inside a 16GB shared GPU.
    ap.add_argument("--max-seq", type=int, default=4096)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--quant", default="q4_k_m")
    ap.add_argument("--holdout-pct", type=int, default=34,
                    help="same deterministic split as bench/curate")
    ap.add_argument("--upweight-edited", type=int, default=2)
    ap.add_argument("--dedupe-sim", type=float, default=0.95)
    ap.add_argument("--min-pairs", type=int, default=100)
    ap.add_argument("--force", action="store_true",
                    help="train below --min-pairs (experiment)")
    ap.add_argument("--skip-train", action="store_true",
                    help="reuse the newest GGUF in the run dir (repackage/regate)")
    ap.add_argument("--keep-merged", action="store_true",
                    help="keep the merged 16-bit shards after packaging "
                         "(default: deleted — ~14GB/run, regenerable from "
                         "the adapter)")
    ap.add_argument("--keep-gguf", action="store_true",
                    help="keep the source GGUF after `ollama create` imports "
                         "it (default: deleted — Ollama serves its own copy)")
    ap.add_argument("--keep-tags", type=int, default=2,
                    help="fine-tune tags to retain in Ollama after the gate "
                         "(newest N + the live model; older ones are removed)")
    ap.add_argument("--no-prune", action="store_true",
                    help="skip tag/run-dir retention pruning after the gate")
    ap.add_argument("--no-gate", action="store_true")
    args = ap.parse_args()

    if args.base is None:
        from app.config import settings
        args.base = settings.text_local.local_model
    if args.base_hf is None:
        args.base_hf = hf_base_for(args.base)

    if args.check:
        cmd_check(args)
        return
    if args.setup:
        cmd_setup(args)
        return

    if not args.base_hf:
        raise SystemExit(f"no HF base known for '{args.base}' — pass --base-hf "
                         "(e.g. unsloth/Qwen2.5-7B-Instruct).")
    tag = args.tag or default_tag(args.base)
    out_dir = RUNS_DIR / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    stage_curate(args)

    distro = pick_distro(args.distro)
    if not distro:
        raise SystemExit("no GPU-visible WSL distro (run --check).")
    gguf = stage_train(args, distro, out_dir, skip_train=args.skip_train)

    stage_package(args, gguf, tag)

    # The tag is created (Ollama holds its own copy of the GGUF), so the
    # merged 16-bit shards are now pure disk bloat — observed live: two runs
    # left 28GB of them behind. Adapter stays: it is the trained artifact.
    if not args.keep_merged:
        freed = cleanup_merged(out_dir)
        if freed:
            print(f"[package] freed {freed / 1e9:.1f} GB of merged-model "
                  "intermediates (adapter kept; re-export anytime with "
                  "--skip-train)")
    # Same story for the source GGUF (~4.5GB): `ollama create` copied it into
    # Ollama's blob store, so the serving path never reads this file again.
    if not args.keep_gguf:
        try:
            size = gguf.stat().st_size
            gguf.unlink()
            print(f"[package] freed {size / 1e9:.1f} GB — removed source GGUF "
                  "(Ollama serves its own imported copy)")
        except OSError as exc:
            print(f"[package] could not remove {gguf.name}: {exc}")

    if args.no_gate:
        print(f"[gate] skipped — try it manually: python scripts\\bench_text.py "
              f"--model {tag} --mode holdout")
        return
    promote, reasons = stage_gate(args, tag)

    # Machine-readable verdict for the idle trainer (and anything else that
    # wants "what happened last run" without parsing stdout).
    (RUNS_DIR / "last_gate.json").write_text(json.dumps({
        "tag": tag, "base": args.base, "promote": promote,
        "reasons": reasons, "ts": time.time(),
    }, indent=2), encoding="utf-8")

    # Retention: storage must be O(1) in the number of retrains, not O(n).
    # Keep the newest `--keep-tags` fine-tune tags + whatever is live; prune
    # the rest from Ollama's blob store and mirror that in the run dirs.
    if not args.no_prune:
        from app.config import settings
        live = settings.text_local.local_model
        removed = prune_ollama_tags(live, keep=args.keep_tags)
        surviving = set(parse_ollama_list(_capture(["ollama", "list"])))
        prune_run_dirs(RUNS_DIR, surviving)
        if removed:
            print(f"[prune] retention: kept {args.keep_tags} newest + live; "
                  f"removed {len(removed)} superseded tag(s)")


if __name__ == "__main__":
    main()
