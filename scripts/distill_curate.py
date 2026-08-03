"""Phase 3 LoRA curation — build the training set and report data readiness.

Walks escalate_distill.jsonl and applies the same eligibility rules the
Phase 2 bench uses (accepted/edited + full-fidelity system/messages), then
drops stubs, excludes the shared holdout split, and optionally dedupes
near-identical prompts. Doubles as a data-quality report so you can watch
the training set grow toward the ~100–300 pair green light.

Usage:
    python scripts/distill_curate.py                         # human report
    python scripts/distill_curate.py --json                  # machine-readable
    python scripts/distill_curate.py --write data/lora/train.jsonl
    python scripts/distill_curate.py --holdout-pct 34 --dedupe-sim 0.95
    python scripts/distill_curate.py --upweight-edited 3     # repeat edited 3×

Training records always use the CLEAN stored system/messages (never the
few-shot-augmented local prompt). Target = edited text when present, else
the accepted parent output. Holdout uses bench_text.in_holdout — a row the
gate scores on must never appear in the written train file.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bench_text as bt  # noqa: E402

# Green-light band from the Phase 3 plan: useful LoRA emerges around here.
CRITICAL_MIN = 100
CRITICAL_READY = 300

# Parent answers that are test harness noise, not teaching signal.
_STUB_ANSWERS = frozenset({
    '{"tasks": ["stub"]}',
    "parent (Claude) answer",
    "rescued by parent",
})

# Soft perishable-fact markers — flagged in the report, not dropped by default.
# Curation prefers form/behavior pairs; stale calendar facts bake into weights.
_PERISHABLE_MARKERS = (
    "tomorrow", "today", "tonight", "yesterday",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
    " next meeting", "am ", "pm ",
)


def load_all_text(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
        except Exception:
            continue
        if row.get("modality") == "text":
            out.append(row)
    return out


def is_stub(row: dict) -> bool:
    gold = bt.gold_answer(row).strip()
    parent = str((row.get("parent") or {}).get("text") or "").strip()
    return gold in _STUB_ANSWERS or parent in _STUB_ANSWERS


def looks_perishable(row: dict) -> bool:
    text = bt.gold_answer(row).lower()
    return any(m in text for m in _PERISHABLE_MARKERS)


def prompt_focus(row: dict) -> str:
    """Question-focused prompt text for dedupe (same focus as few-shot recall)."""
    from app.services.few_shot import query_focus, _row_prompt
    return query_focus(_row_prompt(row))


def dedupe_near(rows: list[dict], *, sim_threshold: float,
                embed_fn=None) -> tuple[list[dict], int]:
    """Greedy keep-first dedupe by embedding similarity of prompt focus.

    `embed_fn(texts) -> ndarray` is injectable for tests; default uses the
    shared sentence-transformers embedder. At sim_threshold >= 1.0, skips
    embedding entirely (exact-focus dedupe only).
    """
    if not rows:
        return [], 0
    if sim_threshold >= 1.0:
        seen: set[str] = set()
        kept: list[dict] = []
        dropped = 0
        for r in rows:
            key = prompt_focus(r)
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            kept.append(r)
        return kept, dropped

    import numpy as np
    if embed_fn is None:
        from app.services.embeddings import embedder
        embed_fn = embedder.encode_many

    focuses = [prompt_focus(r) for r in rows]
    vecs = np.asarray(embed_fn(focuses), dtype=np.float32)
    kept_idx: list[int] = []
    for i, v in enumerate(vecs):
        if any(float(np.dot(v, vecs[j])) >= sim_threshold for j in kept_idx):
            continue
        kept_idx.append(i)
    dropped = len(rows) - len(kept_idx)
    return [rows[i] for i in kept_idx], dropped


def to_example(row: dict) -> dict:
    """One training record: clean prompt + verified target."""
    meta = row.get("meta") or {}
    messages = [
        {"role": m.get("role", "user"), "content": m.get("text", "")}
        for m in meta.get("messages") or []
    ]
    return {
        "id": row.get("id"),
        "task": row.get("task"),
        "outcome": row.get("user_outcome"),
        "system": meta.get("system") or "",
        "messages": messages,
        "target": bt.gold_answer(row),
        "reason": row.get("reason"),
    }


def expand_upweight(examples: list[dict], upweight_edited: int) -> list[dict]:
    """Repeat edited rows upweight_edited times; accepted rows once."""
    if upweight_edited <= 1:
        return list(examples)
    out: list[dict] = []
    for ex in examples:
        n = upweight_edited if ex.get("outcome") == "edited" else 1
        out.extend([ex] * n)
    return out


def curate(rows: list[dict], *, holdout_pct: int, dedupe_sim: float,
           upweight_edited: int = 1, embed_fn=None) -> dict:
    """Run the full funnel. Returns stats + train/holdout example lists."""
    text_n = len(rows)
    by_outcome = Counter(str(r.get("user_outcome") or "unknown") for r in rows)
    trusted = [r for r in rows if r.get("user_outcome") in ("accepted", "edited")]
    eligible = [r for r in trusted if bt.eligible(r)]
    missing_fidelity = [
        r for r in trusted
        if not ((r.get("meta") or {}).get("system")
                and (r.get("meta") or {}).get("messages"))
    ]
    no_gold = [r for r in trusted if not bt.gold_answer(r)]

    stubs = [r for r in eligible if is_stub(r)]
    clean = [r for r in eligible if not is_stub(r)]

    holdout_rows = [r for r in clean if bt.in_holdout(r.get("id", ""), holdout_pct)]
    train_pool = [r for r in clean if not bt.in_holdout(r.get("id", ""), holdout_pct)]

    deduped, near_dup_dropped = dedupe_near(
        train_pool, sim_threshold=dedupe_sim, embed_fn=embed_fn)

    perishable = [r for r in deduped if looks_perishable(r)]
    by_task = Counter(str(r.get("task") or "?") for r in deduped)
    by_train_outcome = Counter(str(r.get("user_outcome")) for r in deduped)

    train_examples = [to_example(r) for r in deduped]
    holdout_examples = [to_example(r) for r in holdout_rows]
    weighted = expand_upweight(train_examples, upweight_edited)

    n_pairs = len(train_examples)
    if n_pairs >= CRITICAL_READY:
        readiness = "ready"
    elif n_pairs >= CRITICAL_MIN:
        readiness = "critical_mass"
    else:
        readiness = "accumulating"

    return {
        "text_rows": text_n,
        "by_outcome": dict(by_outcome),
        "trusted": len(trusted),
        "eligible": len(eligible),
        "dropped_missing_fidelity": len(missing_fidelity),
        "dropped_no_gold": len(no_gold),
        "dropped_stub": len(stubs),
        "holdout_n": len(holdout_rows),
        "holdout_pct": holdout_pct,
        "train_before_dedupe": len(train_pool),
        "dropped_near_dup": near_dup_dropped,
        "train_pairs": n_pairs,
        "train_examples_weighted": len(weighted),
        "upweight_edited": upweight_edited,
        "by_task": dict(by_task),
        "by_train_outcome": dict(by_train_outcome),
        "flagged_perishable": len(perishable),
        "readiness": readiness,
        "critical_min": CRITICAL_MIN,
        "critical_ready": CRITICAL_READY,
        "train": train_examples,
        "holdout": holdout_examples,
        "weighted": weighted,
    }


def print_report(stats: dict) -> None:
    n = stats["train_pairs"]
    need = max(0, stats["critical_min"] - n)
    print("Phase 3 LoRA curation - escalate_distill.jsonl")
    print(f"  text rows                 {stats['text_rows']}")
    print(f"  outcomes                  {stats['by_outcome']}")
    print(f"  trusted (accepted/edited) {stats['trusted']}")
    print(f"  eligible (full-fidelity)  {stats['eligible']}")
    print(f"  dropped missing fidelity  {stats['dropped_missing_fidelity']}")
    print(f"  dropped no gold           {stats['dropped_no_gold']}")
    print(f"  dropped stub              {stats['dropped_stub']}")
    print(f"  holdout (pct={stats['holdout_pct']})     "
          f"{stats['holdout_n']}  (excluded from train)")
    print(f"  train before dedupe       {stats['train_before_dedupe']}")
    print(f"  dropped near-dup          {stats['dropped_near_dup']}")
    print(f"  TRAIN PAIRS               {n}")
    if stats["upweight_edited"] > 1:
        print(f"  weighted examples         {stats['train_examples_weighted']} "
              f"(edited ×{stats['upweight_edited']})")
    print(f"  by task                   {stats['by_task']}")
    print(f"  by outcome                {stats['by_train_outcome']}")
    print(f"  flagged perishable        {stats['flagged_perishable']} "
          "(form/behavior preferred; not dropped)")
    print()
    label = {
        "accumulating": f"ACCUMULATING - need ~{need} more pairs to hit "
                        f"{stats['critical_min']} (green light)",
        "critical_mass": f"CRITICAL MASS - {n} pairs (>={stats['critical_min']}); "
                         f"first light LoRA run is justified",
        "ready": f"READY - {n} pairs (>={stats['critical_ready']}); "
                 "solid first training set",
    }[stats["readiness"]]
    print(f"  readiness: {label}")


def write_jsonl(path: Path, examples: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--holdout-pct", type=int, default=34,
                    help="same deterministic split as bench_text --mode holdout")
    ap.add_argument("--dedupe-sim", type=float, default=0.95,
                    help="drop later rows whose prompt focus is ≥ this similar "
                         "(1.0 = exact-focus only, no embedder)")
    ap.add_argument("--upweight-edited", type=int, default=1,
                    help="repeat edited rows N times in --write output (default 1)")
    ap.add_argument("--write", type=Path, default=None,
                    help="write training JSONL (holdout excluded)")
    ap.add_argument("--write-holdout", type=Path, default=None,
                    help="optional: write holdout examples for inspection")
    ap.add_argument("--json", action="store_true", help="machine-readable stats")
    ap.add_argument("--no-dedupe-embed", action="store_true",
                    help="force exact-focus dedupe (skip loading the embedder)")
    args = ap.parse_args()

    from app.config import settings
    rows = load_all_text(Path(settings.escalate_log.path))
    dedupe_sim = 1.0 if args.no_dedupe_embed else args.dedupe_sim
    stats = curate(rows, holdout_pct=args.holdout_pct, dedupe_sim=dedupe_sim,
                   upweight_edited=args.upweight_edited)

    if args.write:
        write_jsonl(args.write, stats["weighted"])
        print(f"wrote {len(stats['weighted'])} examples -> {args.write}",
              file=sys.stderr)
    if args.write_holdout:
        write_jsonl(args.write_holdout, stats["holdout"])
        print(f"wrote {len(stats['holdout'])} holdout examples -> "
              f"{args.write_holdout}", file=sys.stderr)

    if args.json:
        payload = {k: v for k, v in stats.items()
                   if k not in ("train", "holdout", "weighted")}
        print(json.dumps(payload, indent=2))
        return
    print_report(stats)


if __name__ == "__main__":
    main()
