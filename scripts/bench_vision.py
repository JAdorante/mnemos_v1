"""Vision benchmark harness — Benchmarks A (extraction) and B (classification).

Runs each provider under test over data/bench/vision, scores predictions against
the gold labels, and prints a comparison table. Every model call flows through
model_log (task="bench_vision"), so latency/cost also land in /console/models.

Usage:
    python scripts/bench_vision.py                 # all local + claude
    python scripts/bench_vision.py --models minicpm-v,moondream,llava:7b
    python scripts/bench_vision.py --claude --gemini   # include paid providers
    python scripts/bench_vision.py --limit 40      # quick smoke run

Metrics (per the BENCHMARKS spec):
  content_type: accuracy, macro-F1, todo_list recall, none precision, confusion
  items[]:      set-F1 (normalized), count-accuracy
  ocr_text:     CER (character error rate)
  operational:  p50 / p95 warm latency, est. cost (from model_log)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path

DATA = Path("data/bench/vision")


# ------------------------------- metrics -----------------------------------
def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower().strip()
                   if c.isalnum() or c.isspace()).strip()


def _levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(pred: str, gold: str) -> float:
    g = " ".join((gold or "").split())
    p = " ".join((pred or "").split())
    if not g:
        return 0.0 if not p else 1.0
    return _levenshtein(p, g) / len(g)


def items_f1(pred: list, gold: list) -> float:
    ps = {_norm(x) for x in (pred or []) if _norm(x)}
    gs = {_norm(x) for x in (gold or []) if _norm(x)}
    if not ps and not gs:
        return 1.0
    if not ps or not gs:
        return 0.0
    tp = len(ps & gs)
    prec = tp / len(ps)
    rec = tp / len(gs)
    return 0.0 if prec + rec == 0 else 2 * prec * rec / (prec + rec)


def macro_f1(rows, classes):
    """rows: list of (gold, pred). Returns (macro_f1, per_class dict)."""
    per = {}
    f1s = []
    for c in classes:
        tp = sum(1 for g, p in rows if g == c and p == c)
        fp = sum(1 for g, p in rows if g != c and p == c)
        fn = sum(1 for g, p in rows if g == c and p != c)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 0.0 if prec + rec == 0 else 2 * prec * rec / (prec + rec)
        per[c] = {"precision": prec, "recall": rec, "f1": f1, "support": tp + fn}
        if tp + fn:                 # only classes present in gold count in macro
            f1s.append(f1)
    return (sum(f1s) / len(f1s) if f1s else 0.0), per


# ------------------------------- providers ---------------------------------
def build_providers(args):
    provs = []
    from app.services.vlm import OllamaVLM
    models = (args.models.split(",") if args.models
              else ["minicpm-v"])
    for m in models:
        provs.append((f"ollama/{m}", OllamaVLM(model=m.strip())))
    if args.claude:
        from app.services.vlm import ClaudeVLM
        provs.append(("claude/opus-4-8", ClaudeVLM()))
    if args.gemini:
        from app.services.vlm_gemini import GeminiVLM
        g = GeminiVLM()
        if g.available():
            provs.append((f"gemini/{g.model}", g))
        else:
            print("[bench] --gemini set but GOOGLE_API_KEY missing; skipping Gemini.")
    return provs


# ------------------------------- run ---------------------------------------
def load_dataset(limit=None):
    rows = [json.loads(l) for l in (DATA / "labels.jsonl").read_text().splitlines() if l]
    return rows[:limit] if limit else rows


def run_provider(name, provider, dataset):
    gold_pred, item_scores, cers, lats, ok = [], [], [], [], 0
    for row in dataset:
        jpeg = (DATA / "frames" / f"{row['id']}.jpg").read_bytes()
        t0 = time.time()
        try:
            pred = provider.describe(jpeg)
            lats.append(time.time() - t0)
            ok += 1
        except Exception as exc:
            print(f"  [{name}] {row['id']} error: {exc}")
            pred = {"content_type": "none", "items": [], "ocr_text": ""}
        gold_pred.append((row["content_type"], pred.get("content_type", "none")))
        item_scores.append(items_f1(pred.get("items"), row["items"]))
        cers.append(cer(pred.get("ocr_text", ""), row["ocr_text"]))
    classes = sorted({g for g, _ in gold_pred} | {p for _, p in gold_pred})
    acc = sum(1 for g, p in gold_pred if g == p) / len(gold_pred)
    mf1, per = macro_f1(gold_pred, classes)
    todo_rec = per.get("todo_list", {}).get("recall", float("nan"))
    none_prec = per.get("none", {}).get("precision", float("nan"))
    p = sorted(lats) or [0]
    return {
        "name": name, "n": len(dataset), "ok": ok,
        "accuracy": acc, "macro_f1": mf1,
        "todo_recall": todo_rec, "none_precision": none_prec,
        "items_f1": statistics.mean(item_scores),
        "cer": statistics.mean(cers),
        "p50": p[len(p) // 2], "p95": p[min(len(p) - 1, int(len(p) * 0.95))],
        "confusion": _confusion(gold_pred, classes), "per_class": per,
    }


def _confusion(rows, classes):
    m = defaultdict(lambda: defaultdict(int))
    for g, p in rows:
        m[g][p] += 1
    return {g: dict(m[g]) for g in classes if g in m}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="", help="comma list of ollama models")
    ap.add_argument("--claude", action="store_true")
    ap.add_argument("--gemini", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    dataset = load_dataset(args.limit)
    print(f"dataset: {len(dataset)} frames (SYNTHETIC — printed text, upper bound)\n")
    provs = build_providers(args)
    results = []
    for name, provider in provs:
        if hasattr(provider, "available") and not provider.available():
            print(f"[bench] {name} unavailable; skipping.")
            continue
        print(f"running {name} ...")
        try:
            provider.describe((DATA / "frames" / f"{dataset[0]['id']}.jpg").read_bytes())
        except Exception:
            pass  # warmup (cold VRAM load excluded from latency)
        results.append(run_provider(name, provider, dataset))

    hdr = (f"{'model':22} {'acc':>6} {'macroF1':>8} {'todoRec':>8} "
           f"{'noneP':>6} {'itemsF1':>8} {'CER':>6} {'p50':>6} {'p95':>6}")
    print("\n" + hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['name']:22} {r['accuracy']:6.2f} {r['macro_f1']:8.2f} "
              f"{r['todo_recall']:8.2f} {r['none_precision']:6.2f} "
              f"{r['items_f1']:8.2f} {r['cer']:6.2f} "
              f"{r['p50']:6.1f} {r['p95']:6.1f}")

    out = DATA / "results.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nfull results (+ confusion matrices) -> {out}")


if __name__ == "__main__":
    main()
