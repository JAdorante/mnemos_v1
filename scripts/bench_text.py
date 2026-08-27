"""Text benchmark harness — Phase 2 of the learning loop (the promotion gate).

Scores the local text model against HUMAN-VERIFIED answers from the escalation
distill trail: every labeled row (accepted/edited, full-fidelity) is replayed
against the local model and the reply is scored by embedding similarity to the
verified answer (the human's edited text when present, else the parent's
accepted output). This is what makes "is the local model getting better?" a
number — and what a Phase 3 LoRA challenger must beat before promotion.

Contamination guard: the row being evaluated is never available to few-shot
retrieval for its own replay.

Modes:
  loo (default)  leave-one-out over ALL labeled rows — right at small scale;
                 each row is excluded from its own retrieval pool.
  holdout        deterministic id-hash split (--pct); rows in the holdout are
                 excluded from retrieval entirely. Use for Phase 3 gating,
                 where training itself will have seen the non-holdout rows.

Usage:
    python scripts/bench_text.py                      # loo, few-shot on
    python scripts/bench_text.py --no-fewshot         # raw model, no examples
    python scripts/bench_text.py --model llama3.2-mnemos   # challenger
    python scripts/bench_text.py --mode holdout --pct 34
    python scripts/bench_text.py --json

Each run appends one summary row to data/bench/text_results.jsonl (time, model,
mode, per-task metrics) so scores trend across days and model tags.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

RESULTS = Path("data/bench/text_results.jsonl")

# A reply this similar to the verified answer counts as a pass. 0.6 is loose on
# purpose: verified answers are one good phrasing, not the only one.
PASS_SIM = 0.6


# ----------------------------- dataset --------------------------------------
def gold_answer(row: dict) -> str:
    """Human-edited text beats the parent's accepted output. Local-kept rows
    (reason=local_kept/parent_failed) carry no parent side — there a 👍
    verifies the LOCAL text, so an accepted row's gold is the local answer."""
    gold = str(row.get("edited") or (row.get("parent") or {}).get("text") or "")
    if not gold and row.get("user_outcome") == "accepted":
        gold = str((row.get("local") or {}).get("text") or "")
    return gold


def eligible(row: dict) -> bool:
    """Labeled, trustworthy, and replayable (full-fidelity prompt stored)."""
    meta = row.get("meta") or {}
    return (row.get("modality") == "text"
            and row.get("user_outcome") in ("accepted", "edited")
            and bool(meta.get("system"))
            and bool(meta.get("messages"))
            and bool(gold_answer(row)))


def load_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
        except Exception:
            continue
        if eligible(row):
            rows.append(row)
    return rows


def in_holdout(row_id: str, pct: int) -> bool:
    """Deterministic id-hash split — stable across runs and machines."""
    try:
        return int(str(row_id)[:8], 16) % 100 < pct
    except Exception:
        return False


# ----------------------------- replay ----------------------------------------
def replay_messages(row: dict) -> list[dict]:
    return [{"role": m.get("role", "user"), "content": m.get("text", "")}
            for m in (row.get("meta") or {}).get("messages", [])]


def probe_row(row: dict, local, *, fewshot: bool, exclude_ids: frozenset,
              fewshot_k: int, fewshot_min_sim: float, conf_weight: float = 0.0,
              exemplars: bool = False) -> dict:
    """Replay one labeled call and score it, WITHOUT applying the escalate gate.

    Split out of `run_row` so a caller can sweep the confidence threshold over
    a single set of replays instead of re-running the model per threshold
    (scripts/bench_bakeoff.py). `hard_escalate` is the threshold-independent
    half of the router's policy — parse failure or a suspect answer, which
    escalate at any threshold; `conf_effective` is the half the threshold
    gates. `exemplars` mirrors production's QUILL_EXEMPLARS=1 path (exemplar
    store first, legacy few-shot fallback) — the third arm of the E.3 gate.
    """
    meta = row.get("meta") or {}
    system = meta["system"]
    messages = replay_messages(row)
    schema = meta.get("schema")
    ex: list[dict] = []
    if fewshot:
        from app.services.few_shot import few_shot
        if exemplars:
            try:
                from app.services.exemplar_store import (ROUTER_TASK_TYPES,
                                                         exemplar_store)
                from app.services.few_shot import query_focus, query_text
                types = ROUTER_TASK_TYPES.get(row["task"], ())
                q = query_focus(query_text(messages))
                if types and q:
                    ex = exemplar_store.examples(
                        types, q, k=fewshot_k,
                        exclude_pair_ids=frozenset({str(row.get("id"))}))
            except Exception as exc:
                print(f"  exemplar arm skipped ({exc})", file=sys.stderr)
        if not ex:
            ex = few_shot.examples(row["task"], messages, k=fewshot_k,
                                   min_sim=fewshot_min_sim,
                                   exclude_ids=exclude_ids | {row.get("id")})
        if ex:
            system = system + few_shot.render(ex, confidence_line=schema is None)
    t0 = time.time()
    res = local.complete(row["task"], system=system, messages=messages,
                         schema=schema)
    latency = time.time() - t0
    conf = res.get("confidence")
    from app.services.model_router import (effective_confidence,
                                           evidence_examples, suspect_answer)
    eff = effective_confidence(conf, evidence_examples(res.get("text"), ex),
                               weight=conf_weight)
    suspect = None if schema is not None else suspect_answer(
        res.get("text") or "", messages)
    from app.services.embeddings import embedder
    import numpy as np
    pred, gold = res.get("text") or "", gold_answer(row)
    vecs = embedder.encode_many([pred, gold])
    sim = float(np.dot(vecs[0], vecs[1]))
    return {"id": row.get("id"), "task": row["task"], "sim": round(sim, 4),
            "pass": sim >= PASS_SIM,
            "hard_escalate": (not res.get("parse_ok", True)) or bool(suspect),
            "confidence": conf,
            "conf_effective": None if eff is None else round(float(eff), 3),
            "fewshot_n": len(ex), "latency_s": round(latency, 2)}


def run_row(row: dict, local, *, escalate_min_conf: float, **kw) -> dict:
    """Replay one labeled call and apply the escalate gate at
    `escalate_min_conf`. The decision uses the ROUTER's own calibrated-
    confidence policy (model_router.effective_confidence), so bench numbers
    match production."""
    s = probe_row(row, local, **kw)
    eff = s["conf_effective"]
    s["would_escalate"] = (s.pop("hard_escalate")
                           or eff is None or float(eff) < escalate_min_conf)
    return s


def aggregate(scored: list[dict]) -> dict:
    """Per-task + overall rollups of the metrics that gate promotion."""
    def _roll(rows: list[dict]) -> dict:
        lats = sorted(r["latency_s"] for r in rows)
        return {
            "n": len(rows),
            "mean_sim": round(statistics.mean(r["sim"] for r in rows), 4),
            "pass_rate": round(sum(r["pass"] for r in rows) / len(rows), 4),
            "would_escalate_rate": round(
                sum(r["would_escalate"] for r in rows) / len(rows), 4),
            "p50_latency_s": lats[len(lats) // 2],
        }
    by_task = {}
    for task in sorted({r["task"] for r in scored}):
        by_task[task] = _roll([r for r in scored if r["task"] == task])
    return {"overall": _roll(scored), "by_task": by_task}


# ----------------------------- main ------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=None,
                    help="ollama model tag to test (default: configured local)")
    ap.add_argument("--mode", choices=["loo", "holdout"], default="loo")
    ap.add_argument("--pct", type=int, default=34,
                    help="holdout mode: percent of rows held out")
    ap.add_argument("--no-fewshot", action="store_true",
                    help="raw model, no retrieved examples")
    ap.add_argument("--exemplars", action="store_true",
                    help="exemplar-store retrieval first (production "
                         "QUILL_EXEMPLARS=1 behavior) — the E.3 third arm")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    from app.config import settings
    from app.services.ollama_text import OllamaText

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

    local = OllamaText(model=args.model)
    if not local.available():
        sys.exit(f"local model '{local.model}' not reachable at {local.url}.")

    cfg = settings.text_local
    fewshot = not args.no_fewshot
    print(f"model={local.model}  mode={args.mode}  fewshot={'on' if fewshot else 'off'}  "
          f"rows={len(eval_rows)}/{len(rows)} labeled", file=sys.stderr)

    local.complete("bench_warmup", system="Reply OK.",
                   messages=[{"role": "user", "content": "OK?"}], max_tokens=8)

    scored = []
    for i, row in enumerate(eval_rows, 1):
        try:
            s = run_row(row, local, fewshot=fewshot, exclude_ids=exclude,
                        fewshot_k=cfg.fewshot_k, fewshot_min_sim=cfg.fewshot_min_sim,
                        escalate_min_conf=cfg.escalate_min_conf,
                        conf_weight=cfg.fewshot_conf_weight,
                        exemplars=args.exemplars)
        except Exception as exc:
            print(f"  row {row.get('id', '?')[:8]} error: {exc}", file=sys.stderr)
            continue
        scored.append(s)
        print(f"  [{i}/{len(eval_rows)}] {s['task']:<10} sim={s['sim']:.2f} "
              f"conf={s['confidence']} eff={s['conf_effective']} "
              f"fewshot={s['fewshot_n']} "
              f"{'ESCALATES' if s['would_escalate'] else 'stays local'}",
              file=sys.stderr)
    if not scored:
        sys.exit("every replay failed — is Ollama healthy?")

    agg = aggregate(scored)
    summary = {"time": time.time(), "model": local.model, "mode": args.mode,
               "fewshot": fewshot, **agg}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary) + "\n")

    if args.json:
        print(json.dumps({**summary, "rows": scored}, indent=2))
        return
    hdr = f"{'task':<12} {'n':>3} {'meanSim':>8} {'pass':>6} {'escal':>6} {'p50':>6}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for task, m in {**agg["by_task"], "OVERALL": agg["overall"]}.items():
        print(f"{task:<12} {m['n']:>3} {m['mean_sim']:>8.2f} {m['pass_rate']:>6.2f} "
              f"{m['would_escalate_rate']:>6.2f} {m['p50_latency_s']:>6.1f}")
    print(f"\ntrend log -> {RESULTS}")


if __name__ == "__main__":
    main()
