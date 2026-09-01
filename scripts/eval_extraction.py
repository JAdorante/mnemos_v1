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


# --------------------------------------------------------------------------
# WS1 gate: context prior (QUILL_EXTRACT_CONTEXT) — two-pass off/on compare.
# Flip-on bar (the vocab-hint bar): no precision or faithfulness regression,
# and >= 20% of ambiguous-reference fixtures gain a correct project
# attribution. Cases carry synthetic `anchors` (injected by patching
# context_anchor.anchors_for_window) + `expect_about_project`.
# --------------------------------------------------------------------------
def _attributed(pred: dict, expect: str) -> bool:
  want = _norm(expect)
  for r in pred.get("relations", []):
    if (r.get("predicate") == "about_project"
        and want and want in _norm(r.get("object", ""))):
      return True
  return False


def _faith_rate(pred: dict, transcript: str, span_is_faithful) -> tuple[int, int]:
  ok = tot = 0
  for f in _all_facts(pred):
    tot += 1
    if span_is_faithful(f.get("source_span", ""), transcript):
      ok += 1
  return ok, tot


def run_context_compare(golden: list[dict], *, span_is_faithful) -> int:
  import os
  from unittest.mock import patch

  from app.services.extractor import extractor

  cases = [c for c in golden if c.get("anchors")]
  if not cases:
    print("[eval-ctx] no cases with synthetic anchors — nothing to gate")
    return 1
  agg = {"off": dict(tp=0, fp=0, fn=0, fok=0, ftot=0, attr=0),
         "on": dict(tp=0, fp=0, fn=0, fok=0, ftot=0, attr=0)}
  ambiguous = [c for c in cases if c.get("expect_about_project")]
  for c in cases:
    turn = {"text": c["transcript"], "speaker": c.get("speaker", ""),
            "start": 1.0, "end": 2.0}
    for mode, env in (("off", "0"), ("on", "1")):
      with patch.dict(os.environ, {"QUILL_EXTRACT_CONTEXT": env}), \
           patch("app.services.context_anchor.anchors_for_window",
                 return_value=c["anchors"]):
        try:
          pred = extractor._extract_text(turn)
        except Exception as exc:
          print(f"  {c.get('id')}: LLM error ({exc}); skipping")
          pred = {}
      s = _score_case(c, pred, span_is_faithful=span_is_faithful)
      a = agg[mode]
      a["tp"] += s["tp"]; a["fp"] += s["fp"]; a["fn"] += s["fn"]
      a["fok"] += s["faith_ok"]; a["ftot"] += s["faith_tot"]
      if c.get("expect_about_project") and _attributed(
          pred, c["expect_about_project"]):
        a["attr"] += 1
  p_off = _pr(agg["off"]["tp"], agg["off"]["tp"] + agg["off"]["fp"])
  p_on = _pr(agg["on"]["tp"], agg["on"]["tp"] + agg["on"]["fp"])
  f_off = _pr(agg["off"]["fok"], agg["off"]["ftot"])
  f_on = _pr(agg["on"]["fok"], agg["on"]["ftot"])
  gain = ((agg["on"]["attr"] - agg["off"]["attr"]) / len(ambiguous)
          if ambiguous else 0.0)
  print("\n=== context-prior gate (QUILL_EXTRACT_CONTEXT) ===")
  print(f"precision     off={p_off:.2f}  on={p_on:.2f}")
  print(f"faithfulness  off={f_off:.2f}  on={f_on:.2f}")
  print(f"attribution   off={agg['off']['attr']}  on={agg['on']['attr']}  "
        f"gain={gain:.0%} of {len(ambiguous)} ambiguous fixtures")
  ok = (p_on >= p_off - 0.02) and (f_on >= f_off - 0.02) and gain >= 0.20
  print("GATE:", "PASS — safe to flip QUILL_EXTRACT_CONTEXT=1" if ok
        else "FAIL — keep the flag off")
  return 0 if ok else 1


# --------------------------------------------------------------------------
# WS3 gate: idea extraction (QUILL_EXTRACT_IDEAS). Enable-by-default bar:
# idea precision >= 0.8 on goldens, ZERO task/commitment double-emission,
# no regression on existing kinds (run the standard eval alongside).
# Cases carry `ideas` (expected keyword lists) and optionally `not_ideas`.
# --------------------------------------------------------------------------
def run_ideas_gate(golden: list[dict], *, span_is_faithful) -> int:
  import os
  from unittest.mock import patch

  from app.services.extractor import extractor

  cases = [c for c in golden if "ideas" in c or "not_ideas" in c]
  if not cases:
    print("[eval-ideas] no idea-labeled cases — nothing to gate")
    return 1
  tp = fp = fn = 0
  double = 0
  for c in cases:
    turn = {"text": c["transcript"], "speaker": c.get("speaker", ""),
            "start": 1.0, "end": 2.0}
    with patch.dict(os.environ, {"QUILL_EXTRACT_IDEAS": "1"}):
      try:
        pred = extractor._extract_text(turn)
      except Exception as exc:
        print(f"  {c.get('id')}: LLM error ({exc}); skipping")
        continue
    idea_texts = [i.get("text", "") for i in pred.get("ideas", [])]
    exp = c.get("ideas") or []
    found = [kw for kw in exp if any(_has_all(t, kw) for t in idea_texts)]
    matched = [t for t in idea_texts if any(_has_all(t, kw) for kw in exp)]
    tp += len(found)
    fp += len(idea_texts) - len(matched)
    fn += len(exp) - len(found)
    # Double-emission: an expected IDEA must not also land as a
    # task/commitment (the boundary rule).
    for kw in exp:
      if any(_has_all(a, kw) for a in _actionables(pred)):
        double += 1
        print(f"  {c.get('id')}: DOUBLE-EMITTED idea {kw}")
    for kw in (c.get("not_ideas") or []):
      if any(_has_all(t, kw) for t in idea_texts):
        fp += 1
        print(f"  {c.get('id')}: NOT-AN-IDEA emitted as idea: {kw}")
  prec = _pr(tp, tp + fp)
  rec = _pr(tp, tp + fn)
  print("\n=== ideas gate (QUILL_EXTRACT_IDEAS) ===")
  print(f"idea precision={prec:.2f} recall={rec:.2f} (tp={tp} fp={fp} fn={fn})")
  print(f"task/commitment double-emission: {double}")
  ok = prec >= 0.80 and double == 0
  print("GATE:", "PASS — safe to flip QUILL_EXTRACT_IDEAS=1" if ok
        else "FAIL — keep the flag off")
  return 0 if ok else 1


def main() -> int:
    from app.services.cog_telemetry import span_is_faithful
    from app.services.extractor import EXTRACTOR_MODEL, extractor

    parser = argparse.ArgumentParser(description="Run extraction golden eval.")
    parser.add_argument("--data", type=Path, default=DATA, help="Path to golden.jsonl")
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N cases")
    parser.add_argument("--context-gate", action="store_true",
                        help="WS1: two-pass off/on compare for the context "
                             "prior; exits nonzero if the flip-on bar fails")
    parser.add_argument("--ideas-gate", action="store_true",
                        help="WS3: idea precision + double-emission gate")
    args = parser.parse_args()

    if args.context_gate or args.ideas_gate:
        golden = _load_golden(args.data, limit=args.limit)
        if not golden:
            print(f"[eval] no cases found in {args.data}")
            return 1
        rc = 0
        if args.context_gate:
            rc |= run_context_compare(golden, span_is_faithful=span_is_faithful)
        if args.ideas_gate:
            rc |= run_ideas_gate(golden, span_is_faithful=span_is_faithful)
        return rc

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
