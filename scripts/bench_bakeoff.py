"""Base-model bake-off — choose the local text model at MATCHED RISK.

`QUILL_TEXT_LOCAL_MODEL` sits under everything else in the learning loop:
few-shot, exemplars, the router and any future LoRA all multiply whatever the
base can do. Swapping it is a one-line config change, so it is the cheapest
quality lever available — but only if the comparison is honest.

The trap this harness exists to avoid: `QUILL_TEXT_ESCALATE_MIN_CONF` (0.6)
was tuned against the incumbent's confidence distribution. A new base with a
shifted distribution keeps more or fewer answers local for reasons that have
nothing to do with answer quality, so comparing two models at the SAME
threshold measures calibration, not competence. Instead every candidate is
replayed once with the gate deferred, then the threshold is swept in
post-processing and each model is reported at its own *matched-risk*
threshold: the cheapest threshold whose confidently-wrong rate (stayed local,
sim < CONF_WRONG_SIM — the same constant that gates LoRA promotion) does not
exceed the incumbent's at today's production threshold. `stays_local` at that
threshold is then a fair savings number across models.

Reasoning tags: Qwen3-family models emit `<think>...</think>` unless the
non-thinking variant is pulled. That is stripped in production by
`ollama_text.strip_reasoning`, so this harness inherits it — no wrapper here,
and a thinking-default tag cannot skew sim scores either way.

Usage:
    python scripts/bench_bakeoff.py --pull                    # raw + fewshot arms
    python scripts/bench_bakeoff.py --arms fewshot exemplars  # once exemplars exist
    python scripts/bench_bakeoff.py --models qwen3:4b-instruct --arms fewshot
    python scripts/bench_bakeoff.py --mode holdout --pct 34   # only when gating LoRA

Each run appends one row per (model, arm) to data/bench/bakeoff_results.jsonl.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from bench_text import (PASS_SIM, in_holdout, load_rows,  # noqa: E402
                        probe_row)
from train_lora import CONF_WRONG_SIM  # noqa: E402  (single-sourced with the gate)

RESULTS = Path("data/bench/bakeoff_results.jsonl")

# Swept in ascending order, so the FIRST threshold that fits the risk budget is
# also the cheapest one (escalation is monotone in the threshold).
SWEEP = [round(i * 0.05, 2) for i in range(20)]           # 0.00 .. 0.95

# Below this many labeled rows the confidently-wrong rate moves in steps of
# 1/n and the matched-risk comparison is noise, not signal.
MIN_ROWS = 20

# The 2026 shortlist: instruct-tuned small bases that fit an 8-12GB card at Q4,
# preferring explicit -instruct tags where the family defaults to thinking mode.
# The configured incumbent is prepended automatically — it anchors the risk
# budget every other row is matched to. Override freely with --models.
DEFAULT_SLATE = ("llama3.2", "qwen3:4b-instruct", "qwen3:8b", "phi4-mini")
ARMS = ("raw", "fewshot", "exemplars")

# A model that ignores the CONFIDENCE trailer this often reports None, which
# reads as "unsure" and escalates everything — great sim scores, no savings.
CONF_MISSING_ALARM = 0.5


# ----------------------------- sweep -----------------------------------------
def escalates(row: dict, threshold: float) -> bool:
    """Production's escalate decision, recomposed at an arbitrary threshold."""
    eff = row["conf_effective"]
    return bool(row["hard_escalate"]) or eff is None or float(eff) < threshold


def sweep_point(scored: list[dict], threshold: float) -> dict:
    """Rates at one threshold. Both are fractions of ALL replayed rows (not of
    the stayed-local subset) so they stay monotone in the threshold and
    comparable across models that keep different amounts of traffic local."""
    n = len(scored)
    kept = [r for r in scored if not escalates(r, threshold)]
    wrong = sum(1 for r in kept if r["sim"] < CONF_WRONG_SIM)
    return {"threshold": threshold,
            "stays_local": round(len(kept) / n, 4),
            "conf_wrong": round(wrong / n, 4),
            "n_local": len(kept), "n_conf_wrong": wrong}


def matched_point(scored: list[dict], budget: float) -> dict | None:
    """Cheapest threshold whose confidently-wrong rate fits the risk budget.
    None when even the strictest swept threshold cannot — a candidate that
    cannot be made as safe as the incumbent is simply not a candidate."""
    for thr in SWEEP:
        p = sweep_point(scored, thr)
        if p["conf_wrong"] <= budget + 1e-9:
            return p
    return None


def roll(scored: list[dict]) -> dict:
    """Threshold-independent quality/latency rollup."""
    lats = sorted(r["latency_s"] for r in scored)
    return {
        "n": len(scored),
        "mean_sim": round(statistics.mean(r["sim"] for r in scored), 4),
        "pass_rate": round(sum(r["pass"] for r in scored) / len(scored), 4),
        "p50_latency_s": lats[len(lats) // 2],
        "p90_latency_s": lats[min(len(lats) - 1, int(len(lats) * 0.9))],
        # Protocol-drift tripwire: the model's OWN confidence, before the
        # few-shot evidence floor can paper over a missing trailer.
        "conf_missing_rate": round(
            sum(1 for r in scored if r["confidence"] is None) / len(scored), 4),
    }


# ----------------------------- runner ----------------------------------------
def pull(tag: str) -> bool:
    if not shutil.which("ollama"):
        print(f"  cannot pull {tag}: no `ollama` binary on PATH", file=sys.stderr)
        return False
    print(f"  pulling {tag} ...", file=sys.stderr)
    return subprocess.run(["ollama", "pull", tag]).returncode == 0


def run_arm(local, rows: list[dict], exclude: frozenset, cfg, arm: str,
            label: str) -> list[dict]:
    """Replay every row on one (model, arm). Rows that error are dropped —
    a model that errors on half the set will show it in `n`."""
    scored = []
    for i, row in enumerate(rows, 1):
        try:
            s = probe_row(row, local, fewshot=arm != "raw", exclude_ids=exclude,
                          fewshot_k=cfg.fewshot_k,
                          fewshot_min_sim=cfg.fewshot_min_sim,
                          conf_weight=cfg.fewshot_conf_weight,
                          exemplars=arm == "exemplars")
        except Exception as exc:
            print(f"  {label} row {str(row.get('id', '?'))[:8]} error: {exc}",
                  file=sys.stderr)
            continue
        scored.append(s)
        print(f"  {label} [{i}/{len(rows)}] {s['task']:<8} sim={s['sim']:.2f} "
              f"conf={s['confidence']} eff={s['conf_effective']} "
              f"{s['latency_s']:.1f}s", file=sys.stderr)
    return scored


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", nargs="+", default=list(DEFAULT_SLATE),
                    help="slate to bench (default: incumbent + 2026 candidates)")
    ap.add_argument("--incumbent", default=None,
                    help="risk anchor (default: QUILL_TEXT_LOCAL_MODEL)")
    ap.add_argument("--arms", nargs="+", choices=ARMS, default=["raw", "fewshot"])
    ap.add_argument("--primary", default=None,
                    help="arm the recommendation is read off (default: last arm)")
    ap.add_argument("--pull", action="store_true", help="`ollama pull` missing tags")
    ap.add_argument("--mode", choices=["loo", "holdout"], default="loo")
    ap.add_argument("--pct", type=int, default=34)
    ap.add_argument("--rows", type=int, default=0, help="cap rows (smoke runs)")
    ap.add_argument("--max-p50", type=float, default=None,
                    help="latency budget: candidates above it are not recommended")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from app.config import settings
    from app.services.ollama_text import OllamaText

    cfg = settings.text_local
    incumbent = args.incumbent or cfg.local_model
    models = list(dict.fromkeys([incumbent] + list(args.models)))
    primary = args.primary or args.arms[-1]
    if primary not in args.arms:
        sys.exit(f"--primary {primary} is not among --arms {args.arms}")

    rows = load_rows(Path(settings.escalate_log.path))
    if not rows:
        sys.exit("no labeled full-fidelity text rows to score — label some "
                 "escalations first (scripts/distill_label.py).")
    if args.mode == "holdout":
        eval_rows = [r for r in rows if in_holdout(r.get("id", ""), args.pct)]
        exclude = frozenset(r.get("id") for r in eval_rows)
        if not eval_rows:
            sys.exit(f"holdout slice is empty at --pct {args.pct} "
                     f"({len(rows)} labeled rows) — use loo mode or raise --pct.")
    else:
        eval_rows, exclude = rows, frozenset()
    if args.rows:
        eval_rows = eval_rows[:args.rows]

    underpowered = len(eval_rows) < MIN_ROWS
    if underpowered:
        print(f"\n!! {len(eval_rows)} labeled rows — below the {MIN_ROWS} this "
              f"comparison needs. The confidently-wrong rate moves in steps of "
              f"1/{len(eval_rows)} = {1 / len(eval_rows):.0%}, so matched-risk "
              f"thresholds are noise. Treat the output as a smoke test and "
              f"label more rows (scripts/distill_label.py) before flipping "
              f"anything.\n", file=sys.stderr)

    # Model-outer / arm-inner: each model stays resident across its own arms,
    # so the slate pays one cold load per model rather than one per arm.
    results: dict[tuple[str, str], dict] = {}
    for tag in models:
        local = OllamaText(model=tag)
        if not local.available():
            if not (args.pull and pull(tag) and local.available()):
                print(f"-- skipping {tag}: not present at {local.url} "
                      f"(use --pull)", file=sys.stderr)
                continue
        print(f"\n== {tag} ==", file=sys.stderr)
        try:
            local.complete("bakeoff_warmup", system="Reply OK.",
                           messages=[{"role": "user", "content": "OK?"}],
                           max_tokens=8)
        except Exception as exc:
            print(f"-- skipping {tag}: warmup failed ({exc})", file=sys.stderr)
            continue
        for arm in args.arms:
            scored = run_arm(local, eval_rows, exclude, cfg, arm, f"{tag}/{arm}")
            if scored:
                results[(tag, arm)] = {"rows": scored, **roll(scored)}

    if not results:
        sys.exit("nothing ran — no slate model was reachable.")

    # --- risk budget, per arm: the incumbent's confidently-wrong rate at the
    # threshold production runs today. Matching within an arm keeps the
    # comparison like-for-like (few-shot changes the incumbent's risk too).
    report: list[dict] = []
    for (tag, arm), res in results.items():
        anchor = results.get((incumbent, arm))
        budget = (sweep_point(anchor["rows"], cfg.escalate_min_conf)["conf_wrong"]
                  if anchor else None)
        prod = sweep_point(res["rows"], cfg.escalate_min_conf)
        match = matched_point(res["rows"], budget) if budget is not None else None
        report.append({"model": tag, "arm": arm, "incumbent": tag == incumbent,
                       **{k: v for k, v in res.items() if k != "rows"},
                       "risk_budget": budget, "at_production": prod,
                       "at_matched_risk": match})

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.time()
    with RESULTS.open("a", encoding="utf-8") as f:
        for r in report:
            f.write(json.dumps({"time": stamp, "mode": args.mode,
                                "underpowered": underpowered, **r}) + "\n")

    if args.json:
        print(json.dumps({"time": stamp, "incumbent": incumbent,
                          "underpowered": underpowered, "report": report,
                          "rows": {f"{t}/{a}": r["rows"]
                                   for (t, a), r in results.items()}}, indent=2))
        return

    hdr = (f"{'model':<22} {'arm':<10} {'n':>3} {'meanSim':>8} {'pass':>6} "
           f"{'p50':>6} {'noConf':>7} {'thr':>5} {'local':>6} {'cWrong':>7}")
    print("\n" + hdr)
    print("-" * len(hdr))
    for r in sorted(report, key=lambda x: (x["arm"], -x["mean_sim"])):
        m = r["at_matched_risk"]
        thr = f"{m['threshold']:.2f}" if m else "  --"
        loc = f"{m['stays_local']:.2f}" if m else "  --"
        cw = f"{m['conf_wrong']:.2f}" if m else "  --"
        print(f"{r['model']:<22} {r['arm']:<10} {r['n']:>3} {r['mean_sim']:>8.3f} "
              f"{r['pass_rate']:>6.2f} {r['p50_latency_s']:>6.1f} "
              f"{r['conf_missing_rate']:>7.2f} {thr:>5} {loc:>6} {cw:>7}"
              + ("   <- incumbent" if r["incumbent"] else ""))
    print(f"\nthr/local/cWrong are at each model's MATCHED-RISK threshold; the "
          f"risk budget is the incumbent's confidently-wrong rate (sim < "
          f"{CONF_WRONG_SIM}) at the live threshold {cfg.escalate_min_conf}. "
          f"pass = sim >= {PASS_SIM}. noConf = share of replies with no "
          f"CONFIDENCE trailer.")

    for r in report:
        if r["conf_missing_rate"] > CONF_MISSING_ALARM:
            print(f"!! {r['model']}/{r['arm']}: {r['conf_missing_rate']:.0%} of "
                  f"replies carried no CONFIDENCE line — this model does not "
                  f"follow the protocol and will escalate almost everything.")

    # --- per-task, primary arm: the input to the per-task local-model override.
    prim = {t: res for (t, a), res in results.items() if a == primary}
    tasks = sorted({r["task"] for res in prim.values() for r in res["rows"]})
    if len(tasks) > 1:
        print(f"\nper-task mean sim ({primary} arm)")
        print(f"{'model':<22} " + " ".join(f"{t:>10}" for t in tasks))
        for tag, res in prim.items():
            cells = []
            for t in tasks:
                sims = [r["sim"] for r in res["rows"] if r["task"] == t]
                cells.append(f"{statistics.mean(sims):>10.3f}" if sims else f"{'-':>10}")
            print(f"{tag:<22} " + " ".join(cells))

    # --- recommendation
    base = next((r for r in report
                 if r["model"] == incumbent and r["arm"] == primary), None)
    if not base:
        print(f"\nno incumbent row on the {primary} arm — cannot recommend.")
        return
    cands = [r for r in report if r["arm"] == primary and not r["incumbent"]
             and r["at_matched_risk"] and r["mean_sim"] > base["mean_sim"]
             and r["pass_rate"] >= base["pass_rate"]
             and (args.max_p50 is None or r["p50_latency_s"] <= args.max_p50)]
    # Rank by the savings number, since quality already cleared the incumbent.
    cands.sort(key=lambda r: (-r["at_matched_risk"]["stays_local"], r["p50_latency_s"]))
    print()
    if not cands:
        print(f"no candidate beat {incumbent} on the {primary} arm within the "
              f"risk"
              + (f" and p50 <= {args.max_p50}s" if args.max_p50 else "")
              + " constraints — keep the incumbent.")
        return
    win = cands[0]
    thr = win["at_matched_risk"]["threshold"]
    print(f"winner ({primary} arm): {win['model']}  "
          f"meanSim {base['mean_sim']:.3f} -> {win['mean_sim']:.3f}, "
          f"staysLocal {base['at_matched_risk']['stays_local']:.2f} -> "
          f"{win['at_matched_risk']['stays_local']:.2f} at matched risk, "
          f"p50 {base['p50_latency_s']:.1f}s -> {win['p50_latency_s']:.1f}s")
    print("\nflip BOTH together — the model alone voids the matched-risk guarantee:")
    print(f"    QUILL_TEXT_LOCAL_MODEL={win['model']}")
    print(f"    QUILL_TEXT_ESCALATE_MIN_CONF={thr}")
    print(f"rollback: QUILL_TEXT_LOCAL_MODEL={incumbent} "
          f"QUILL_TEXT_ESCALATE_MIN_CONF={cfg.escalate_min_conf}")
    if underpowered:
        print(f"\n!! NOT decidable at n={base['n']}. Label more rows first.")
    print(f"\ntrend log -> {RESULTS}")


if __name__ == "__main__":
    main()
