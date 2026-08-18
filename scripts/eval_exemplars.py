"""Exemplar A/B harness (Workstream C.5) — the workstream's justification.

Replays a held-out slice of confirmed learning_pairs through the local model
with exemplar injection ON vs OFF and scores each reply against the pair's
verified target (embedding cosine, same metric as bench_text). Per-task-type
deltas land in data/exemplar_ab_report.json; a type whose ON-vs-OFF delta is
negative gets auto-gated off in data/exemplar_type_gates.json and surfaced in
the Learning tab — a gated-off type is a valid, reportable outcome, not a
failure to hide.

Contamination guard: a held-out pair is never its own exemplar (the retrieval
call excludes its pair id), and held-out pairs are excluded from retrieval
entirely via the same deterministic id-hash split bench_text uses.

    python scripts/eval_exemplars.py                # holdout replay
    python scripts/eval_exemplars.py --pct 34
    python scripts/eval_exemplars.py --json
    python scripts/eval_exemplars.py --no-gate      # report only, no gating
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

REPORT = Path("data/exemplar_ab_report.json")

# Minimal per-type replay instruction — the harness is a proxy replay (the
# original prompt templates aren't all persisted); ON and OFF arms share it,
# so the delta isolates the exemplar block.
_TYPE_SYSTEM = {
    "extraction.task": ("Extract the single actionable task from the text. "
                        "Reply with the task statement only."),
    "extraction.commitment": ("Extract the commitment (who promised what) "
                              "from the text. Reply with it only."),
    "extraction.claim": ("Extract the factual claim from the text. "
                         "Reply with it only."),
    "escalation.text": "Answer the request directly and concisely.",
    "brief.section": "Rewrite the insight so it is accurate and useful.",
}


def in_holdout(row_id: str, pct: int) -> bool:
    try:
        return int(str(row_id)[:8], 16) % 100 < pct
    except Exception:
        return False


def run_pair(local, pair: dict, *, with_exemplars: bool,
             exclude_ids: frozenset) -> float | None:
    """One replay; returns cosine(reply, verified target) or None on error."""
    from app.services import exemplar_store
    from app.services.few_shot import few_shot

    task_type = str(pair["task_type"])
    system = _TYPE_SYSTEM.get(task_type,
                              "Answer the request directly and concisely.")
    if with_exemplars:
        ex = exemplar_store.exemplar_store.examples(
            (task_type,), pair["input_text"],
            exclude_pair_ids=exclude_ids | {str(pair["id"])})
        if ex:
            system = system + few_shot.render(ex)
    try:
        res = local.complete("exemplar_ab", system=system,
                             messages=[{"role": "user",
                                        "content": pair["input_text"]}])
    except Exception as exc:
        print(f"  replay error ({exc})", file=sys.stderr)
        return None
    reply = str(res.get("text") or "")
    if not reply:
        return None
    import numpy as np
    from app.services.embeddings import embedder
    v = embedder.encode_many([reply, str(pair["final_target"])])
    return float(np.dot(v[0], v[1]))


def evaluate(pairs: list[dict], local, *, pct: int = 34) -> dict:
    """Pure-ish core (unit-tested with a mocked local): holdout split, two
    arms per pair, per-type aggregate."""
    held = [p for p in pairs if in_holdout(str(p["id"]), pct)]
    exclude = frozenset(str(p["id"]) for p in held)
    by_type: dict[str, dict] = {}
    for p in held:
        s_on = run_pair(local, p, with_exemplars=True, exclude_ids=exclude)
        s_off = run_pair(local, p, with_exemplars=False, exclude_ids=exclude)
        if s_on is None or s_off is None:
            continue
        t = by_type.setdefault(str(p["task_type"]), {"on": [], "off": []})
        t["on"].append(s_on)
        t["off"].append(s_off)
    out = {}
    for task_type, arms in by_type.items():
        if not arms["on"]:
            continue
        mean_on = statistics.mean(arms["on"])
        mean_off = statistics.mean(arms["off"])
        out[task_type] = {"n": len(arms["on"]),
                          "mean_sim_on": round(mean_on, 4),
                          "mean_sim_off": round(mean_off, 4),
                          "delta": round(mean_on - mean_off, 4)}
    return {"time": time.time(), "holdout_pct": pct, "by_type": out}


def apply_gates(report: dict, *, min_n: int = 5) -> list[str]:
    """Auto-disable injection for types the A/B shows exemplars HURT (delta
    < 0 with enough samples); re-open types that recovered. Returns gated."""
    from app.services.exemplar_store import exemplar_store
    gated = []
    for task_type, m in (report.get("by_type") or {}).items():
        if m["n"] < min_n:
            continue
        if m["delta"] < 0:
            exemplar_store.set_gate(task_type, True,
                                    reason=f"ab_delta={m['delta']}")
            gated.append(task_type)
        else:
            g = exemplar_store.gates().get(task_type) or {}
            if str(g.get("reason", "")).startswith("ab_delta"):
                exemplar_store.set_gate(task_type, False)
    return gated


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pct", type=int, default=34)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-gate", action="store_true")
    args = ap.parse_args()

    from app.services.ollama_text import OllamaText
    from app.storage import get_store

    pairs = [p for p in get_store().list_learning_pairs(limit=5000)
             if p.get("verdict") in ("accepted", "edited")
             and p.get("human_confirmed") and p.get("final_target")]
    if not pairs:
        sys.exit("no confirmed positive learning pairs — harvest verdicts "
                 "first (Workstream A).")
    local = OllamaText()
    if not local.available():
        sys.exit(f"local model '{local.model}' not reachable at {local.url}.")

    report = evaluate(pairs, local, pct=args.pct)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    # Keep a history (newest last) — the E.2 saturation trigger compares the
    # last two evals' deltas to detect an exemplar-gain plateau.
    history = []
    if REPORT.is_file():
        try:
            old = json.loads(REPORT.read_text(encoding="utf-8"))
            history = old.get("history") or ([old] if old.get("by_type") else [])
        except Exception:
            history = []
    history = (history + [report])[-12:]
    REPORT.write_text(json.dumps({**report, "history": history}, indent=2),
                      encoding="utf-8")
    gated = [] if args.no_gate else apply_gates(report)

    if args.json:
        print(json.dumps({**report, "gated_off": gated}, indent=2))
        return
    hdr = f"{'task_type':<24} {'n':>3} {'ON':>7} {'OFF':>7} {'delta':>7}"
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for t, m in sorted((report["by_type"] or {}).items()):
        print(f"{t:<24} {m['n']:>3} {m['mean_sim_on']:>7.3f} "
              f"{m['mean_sim_off']:>7.3f} {m['delta']:>+7.3f}"
              + ("   GATED OFF" if t in gated else ""))
    if not report["by_type"]:
        print("(holdout slice empty — add pairs or raise --pct)")
    print(f"\nreport -> {REPORT}")


if __name__ == "__main__":
    main()
