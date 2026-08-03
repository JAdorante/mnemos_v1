"""Golden eval for fact extraction — general-purpose quality benchmark.

Runs the live extractor over a labeled golden set of realistic transcripts
(data/bench/extraction/golden.jsonl) and scores four things a regression
could quietly break:

  * actionables precision/recall  — does it pull the tasks/commitments actually
    stated, without inventing extras?
  * small-talk stays empty        — the false-proactive-offer guard (filler and
    questions must yield NO actionables).
  * claims recall                 — spot check that notable facts survive.
  * source faithfulness           — EVERY emitted fact's `source_span` must be a
    verbatim substring of the transcript. A span that isn't there is a
    hallucinated provenance pointer — an automatic, label-free hallucination
    rate that also runs over past DB facts (see scripts/audit_faithfulness.py).

Use this as a baseline before tuning ASR / the extractor / the agent — so a
change can be shown to help, not just felt.

    python scripts/eval_extraction.py
    python scripts/eval_extraction.py --limit 5          # quick smoke run
    QUILL_EXTRACT_MODEL=claude-haiku-4-5 python scripts/eval_extraction.py

Matching is keyword-based (an expected actionable is "found" when some predicted
actionable contains all its keywords) so paraphrases still count but hallucinated
extras are caught as false positives. Add cases to golden.jsonl as new failure
modes appear.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import app.*

DATA = Path("data/bench/extraction/golden.jsonl")

_WORD = re.compile(r"[a-z0-9$]+")


def _load_golden(path: Path, limit: int | None = None) -> list[dict]:
    cases: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
            if limit is not None and len(cases) >= limit:
                break
    return cases


def _norm(s: str) -> str:
  return " ".join(_WORD.findall((s or "").lower()))


def _has_all(text: str, keywords: list[str]) -> bool:
  t = _norm(text)
  return all(_norm(k) in t for k in keywords)


def _all_facts(pred: dict) -> list[dict]:
  """Every emitted fact that carries a source_span (tasks/commitments/claims)."""
  out: list[dict] = []
  for key in ("tasks", "commitments", "claims"):
    out += [f for f in pred.get(key, []) if isinstance(f, dict)]
  return out


def _actionables(pred: dict) -> list[str]:
  out = [t.get("text", "") for t in pred.get("tasks", [])]
  out += [c.get("text", "") for c in pred.get("commitments", [])]
  return [x for x in out if x.strip()]


def _pr(a: float, b: float) -> float:
  return (a / b) if b else 1.0


def _score_case(case: dict, pred: dict, *, span_is_faithful) -> dict:
  preds = _actionables(pred)
  exp = case["actionables"]

  found = [kw for kw in exp if any(_has_all(p, kw) for p in preds)]
  matched_preds = [p for p in preds if any(_has_all(p, kw) for kw in exp)]

  claim_hit = 0
  for kw in case["claims"]:
    if any(_has_all(c.get("text", ""), kw) for c in pred.get("claims", [])):
      claim_hit += 1

  unfaithful: list[str] = []
  faith_ok = faith_tot = 0
  for f in _all_facts(pred):
    faith_tot += 1
    if span_is_faithful(f.get("source_span", ""), case["transcript"]):
      faith_ok += 1
    else:
      unfaithful.append(f.get("source_span", ""))

  empty_ok = 0
  if case["expect_empty"]:
    empty_ok = 1 if not preds else 0

  return {
    "preds": preds,
    "tp": len(found),
    "fp": len(preds) - len(matched_preds),
    "fn": len(exp) - len(found),
    "claim_hit": claim_hit,
    "claim_tot": len(case["claims"]),
    "faith_ok": faith_ok,
    "faith_tot": faith_tot,
    "empty_ok": empty_ok,
    "empty_tot": 1 if case["expect_empty"] else 0,
    "unfaithful": unfaithful,
    "found": found,
    "exp": exp,
  }


def main() -> int:
    from app.services.cog_telemetry import span_is_faithful
    from app.services.extractor import EXTRACTOR_MODEL, extractor

    parser = argparse.ArgumentParser(description="Run extraction golden eval.")
    parser.add_argument("--data", type=Path, default=DATA, help="Path to golden.jsonl")
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N cases")
    args = parser.parse_args()

    golden = _load_golden(args.data, limit=args.limit)
    if not golden:
        print(f"[eval] no cases found in {args.data}")
        return 1

    print(f"[eval] extractor model: {EXTRACTOR_MODEL}")
    print(f"[eval] cases: {len(golden)} from {args.data}\n")

    tp = fp = fn = 0
    claim_hit = claim_tot = 0
    empty_ok = empty_tot = 0
    faith_ok = faith_tot = 0
    by_category: dict[str, list[dict]] = defaultdict(list)

    for i, case in enumerate(golden, 1):
        label = case.get("id") or f"case-{i}"
        category = case.get("category", "uncategorized")
        try:
            pred = extractor._extract_text(case["transcript"])
        except Exception as exc:
            print(f"  {label} [{category}]: LLM error ({exc}); skipping")
            continue

        scored = _score_case(case, pred, span_is_faithful=span_is_faithful)
        tp += scored["tp"]
        fp += scored["fp"]
        fn += scored["fn"]
        claim_hit += scored["claim_hit"]
        claim_tot += scored["claim_tot"]
        faith_ok += scored["faith_ok"]
        faith_tot += scored["faith_tot"]
        empty_ok += scored["empty_ok"]
        empty_tot += scored["empty_tot"]
        by_category[category].append(scored)

        flag = ""
        if case["expect_empty"] and scored["preds"]:
            flag = "  <-- FALSE PROACTIVE (should be empty)"
        elif len(scored["exp"]) - len(scored["found"]) > 0:
            flag = "  <-- MISSED an expected actionable"
        print(f"  {label} [{category}]: pred={scored['preds'] or '[]'}{flag}")
        if scored["unfaithful"]:
            print(f"           <-- UNFAITHFUL span(s): {scored['unfaithful']}")

    prec = _pr(tp, tp + fp)
    rec = _pr(tp, tp + fn)
    f1 = _pr(2 * prec * rec, prec + rec) if (prec + rec) else 0.0
    print("\n=== extraction golden eval ===")
    print(f"actionables  precision={prec:.2f}  recall={rec:.2f}  f1={f1:.2f}  "
          f"(tp={tp} fp={fp} fn={fn})")
    print(f"claims recall           {claim_hit}/{claim_tot}")
    print(f"small-talk stayed empty {empty_ok}/{empty_tot}  "
          f"(false-proactive rate {_pr(empty_tot - empty_ok, empty_tot):.2f})")
    print(f"source faithfulness     {faith_ok}/{faith_tot}  "
          f"(hallucinated-span rate {_pr(faith_tot - faith_ok, faith_tot):.2f})")

    if len(by_category) > 1:
        print("\n--- by category ---")
        for category in sorted(by_category):
            rows = by_category[category]
            c_tp = sum(r["tp"] for r in rows)
            c_fp = sum(r["fp"] for r in rows)
            c_fn = sum(r["fn"] for r in rows)
            c_claim_hit = sum(r["claim_hit"] for r in rows)
            c_claim_tot = sum(r["claim_tot"] for r in rows)
            c_empty_ok = sum(r["empty_ok"] for r in rows)
            c_empty_tot = sum(r["empty_tot"] for r in rows)
            c_prec = _pr(c_tp, c_tp + c_fp)
            c_rec = _pr(c_tp, c_tp + c_fn)
            parts = [f"n={len(rows)}"]
            if c_tp + c_fp + c_fn:
                parts.append(f"actionables P/R={c_prec:.2f}/{c_rec:.2f}")
            if c_claim_tot:
                parts.append(f"claims={c_claim_hit}/{c_claim_tot}")
            if c_empty_tot:
                parts.append(f"quiet={c_empty_ok}/{c_empty_tot}")
            print(f"  {category:16}  " + "  ".join(parts))

    return 0


if __name__ == "__main__":
  sys.exit(main())
