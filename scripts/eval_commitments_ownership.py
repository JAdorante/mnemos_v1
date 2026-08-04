"""Golden eval #1 — commitments / ownership (plan 2.2).

Scores:
  * Ownership accuracy on two-speaker / me-relative cases (offline, no LLM)
  * Gate policy: quoted/hypothetical never auto-insert (offline)
  * Actionables P/R/F1 — offline uses oracle preds; `--live` calls the extractor
  * expect_empty precision — negated / quoted / hyp / small-talk stay non-insert

Thresholds (fail the process if breached):
  * precision >= 0.90  (keeps auto-insert)
  * ownership errors < 5% on ownership-labeled cases
  * quoted/hypothetical auto-insert rate == 0

    python scripts/eval_commitments_ownership.py              # offline (CI)
    python scripts/eval_commitments_ownership.py --live       # calls LLM
    python scripts/eval_commitments_ownership.py --limit 20

Exit code 0 on pass, 1 on threshold failure or missing golden.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GOLDEN = (Path(__file__).resolve().parent.parent
          / "tests" / "fixtures" / "goldens" / "commitments_ownership.jsonl")

# Plan 2.2 thresholds
MIN_PRECISION = 0.90
MAX_OWNERSHIP_ERROR_RATE = 0.05
MAX_REVIEW_AUTO_INSERT = 0.0  # quoted/hypothetical must never auto-insert

_WORD = re.compile(r"[a-z0-9$]+")


def _load(path: Path, limit: int | None = None) -> list[dict]:
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


def _actionables(pred: dict, *, skip_review_assertions: bool = False) -> list[str]:
    """Texts that would auto-insert. When skip_review_assertions, omit
    quoted/hypothetical items (gate → review, not insert)."""
    out: list[str] = []
    for key in ("tasks", "commitments"):
        for item in pred.get(key) or []:
            if not isinstance(item, dict):
                continue
            if skip_review_assertions:
                assertion = (item.get("assertion") or "").lower()
                if assertion in ("quoted", "hypothetical"):
                    continue
            text = (item.get("text") or "").strip()
            if text:
                out.append(text)
    return out


def _pr(a: float, b: float) -> float:
    return (a / b) if b else 1.0


def _score_actionables(case: dict, pred: dict, *, live: bool) -> tuple[int, int, int]:
    # Offline: honor assertion tags so quoted/hyp don't count as false
    # positives against expect_empty (they go to review, not auto-insert).
    preds = _actionables(pred, skip_review_assertions=not live)
    exp = case.get("actionables") or []
    if case.get("expect_empty"):
        # Any remaining auto-insertable actionable is a false positive.
        return 0, len(preds), 0
    found = [kw for kw in exp if any(_has_all(p, kw) for p in preds)]
    matched = [p for p in preds if any(_has_all(p, kw) for kw in exp)]
    tp = len(found)
    fp = len(preds) - len(matched)
    fn = len(exp) - len(found)
    return tp, fp, fn


def _ownership_ok(ex, store, case: dict, now: float) -> bool | None:
    """True/False when case has ownership ground truth; None if N/A."""
    own = case.get("ownership")
    if not own:
        return None
    from app.services import self_profile
    party = own.get("from_person") or "me"
    pid = ex._resolve_person_id(
        party, now, turn_speaker=case.get("speaker") or "",
        text=case.get("transcript") or "")
    expect = own.get("expect")
    if expect == "self":
        return pid == self_profile.self_person_id(store)
    if expect == "speaker":
        name = own.get("expect_name") or case.get("speaker")
        want = store.resolve_person(name, ts=now) if name else 0
        return bool(want) and pid == int(want)
    if expect == "named":
        name = own.get("expect_name")
        want = store.resolve_person(name, ts=now) if name else 0
        return bool(want) and pid == int(want)
    if expect == "none":
        return pid is None
    return None


def _gate_action(item: dict, transcript: str) -> str:
    from app.services.fact_gate import gate_fact
    v = gate_fact(
        "commitment", item.get("text") or "",
        item.get("confidence"), item.get("source_span") or "",
        transcript, assertion=item.get("assertion"))
    return v.action


def run_eval(cases: list[dict], *, live: bool = False) -> dict:
    from app.services import self_profile
    from app.services.extractor import Extractor
    from app.storage import Store

    self_profile.reset()
    tmp = Path(tempfile.mkdtemp())
    store = Store(db_path=tmp / "eval.db", audio_dir=tmp / "audio")
    ex = Extractor(store=store)
    now = time.time()
    # Seed common people so speaker resolution is stable.
    for name in ("Hugh", "Marc", "Sarah", "Chris", "Alex", "Jordan", "Eve"):
        store.resolve_person(name, ts=now)

    tp = fp = fn = 0
    own_ok = own_tot = 0
    review_auto = 0  # quoted/hyp that would insert
    review_tot = 0
    empty_ok = empty_tot = 0
    by_cat: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "tp": 0, "fp": 0, "fn": 0, "own_ok": 0, "own_tot": 0})

    try:
        for case in cases:
            cat = case.get("category") or "uncategorized"
            by_cat[cat]["n"] += 1

            with _identity(case.get("enrolled_user") or "Hugh"):
                # Ownership (offline)
                ok = _ownership_ok(ex, store, case, now)
                if ok is not None:
                    own_tot += 1
                    by_cat[cat]["own_tot"] += 1
                    if ok:
                        own_ok += 1
                        by_cat[cat]["own_ok"] += 1

                # Gate policy on oracle commitments
                oracle = case.get("oracle") or {}
                for item in oracle.get("commitments") or []:
                    assertion = (item.get("assertion") or "").lower()
                    if assertion in ("quoted", "hypothetical"):
                        review_tot += 1
                        action = _gate_action(item, case.get("transcript") or "")
                        if action == "insert":
                            review_auto += 1

                # Predictions: live LLM or oracle
                if live:
                    from app.services.consolidation import Turn
                    turn = Turn(
                        start=now, end=now,
                        speaker=case.get("speaker") or "",
                        text=case.get("transcript") or "",
                        event_ids=[], n_utterances=1)
                    try:
                        pred = ex._extract_text(turn)
                    except Exception as exc:
                        print(f"  {case.get('id')}: LLM error ({exc}); skip")
                        continue
                else:
                    pred = oracle

                c_tp, c_fp, c_fn = _score_actionables(case, pred, live=live)
                tp += c_tp
                fp += c_fp
                fn += c_fn
                by_cat[cat]["tp"] += c_tp
                by_cat[cat]["fp"] += c_fp
                by_cat[cat]["fn"] += c_fn

                if case.get("expect_empty"):
                    empty_tot += 1
                    if not _actionables(pred, skip_review_assertions=not live):
                        empty_ok += 1
    finally:
        try:
            store.close()
        except Exception:
            pass
        self_profile.reset()

    prec = _pr(tp, tp + fp)
    rec = _pr(tp, tp + fn)
    f1 = _pr(2 * prec * rec, prec + rec) if (prec + rec) else 0.0
    own_err = _pr(own_tot - own_ok, own_tot)
    review_rate = _pr(review_auto, review_tot)

    return {
        "n": len(cases),
        "precision": prec,
        "recall": rec,
        "f1": f1,
        "tp": tp, "fp": fp, "fn": fn,
        "ownership_ok": own_ok,
        "ownership_tot": own_tot,
        "ownership_error_rate": own_err,
        "review_auto_insert": review_auto,
        "review_tot": review_tot,
        "review_auto_insert_rate": review_rate,
        "empty_ok": empty_ok,
        "empty_tot": empty_tot,
        "by_category": dict(by_cat),
        "live": live,
    }


class _identity:
    def __init__(self, name: str):
        self.name = name
        self._patch = None

    def __enter__(self):
        from unittest.mock import patch
        from app.services import self_profile
        self_profile.reset()
        self._patch = patch(
            "app.services.identity.user_identity",
            return_value={"name": self.name, "source": "profile"})
        self._patch.start()
        return self

    def __exit__(self, *exc):
        from app.services import self_profile
        if self._patch:
            self._patch.stop()
        self_profile.reset()
        return False


def _check_thresholds(m: dict) -> list[str]:
    fails: list[str] = []
    if m["n"] < 150:
        fails.append(f"need >=150 cases, got {m['n']}")
    if m["precision"] < MIN_PRECISION:
        fails.append(
            f"precision {m['precision']:.3f} < {MIN_PRECISION} "
            f"(auto-insert gate)")
    if m["ownership_tot"] and m["ownership_error_rate"] > MAX_OWNERSHIP_ERROR_RATE:
        fails.append(
            f"ownership error rate {m['ownership_error_rate']:.3f} "
            f"> {MAX_OWNERSHIP_ERROR_RATE}")
    if m["review_tot"] and m["review_auto_insert_rate"] > MAX_REVIEW_AUTO_INSERT:
        fails.append(
            f"quoted/hypothetical auto-insert "
            f"{m['review_auto_insert_rate']:.3f} > {MAX_REVIEW_AUTO_INSERT}")
    return fails


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Commitments/ownership golden eval (plan 2.2)")
    parser.add_argument("--data", type=Path, default=GOLDEN)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--live", action="store_true",
                        help="Call the live extractor (needs API key)")
    parser.add_argument("--json", action="store_true",
                        help="Print machine-readable summary JSON")
    args = parser.parse_args()

    if not args.data.exists():
        print(f"[eval] missing golden: {args.data}")
        print("       run: python scripts/gen_commitments_ownership_golden.py")
        return 1

    cases = _load(args.data, limit=args.limit)
    mode = "live" if args.live else "offline"
    print(f"[eval] commitments/ownership ({mode}) — {len(cases)} cases "
          f"from {args.data}")

    m = run_eval(cases, live=args.live)
    print("\n=== commitments / ownership golden eval ===")
    print(f"actionables  precision={m['precision']:.2f}  "
          f"recall={m['recall']:.2f}  f1={m['f1']:.2f}  "
          f"(tp={m['tp']} fp={m['fp']} fn={m['fn']})")
    print(f"ownership    {m['ownership_ok']}/{m['ownership_tot']}  "
          f"error_rate={m['ownership_error_rate']:.3f}  "
          f"(threshold < {MAX_OWNERSHIP_ERROR_RATE})")
    print(f"review-guard quoted/hyp auto-insert="
          f"{m['review_auto_insert']}/{m['review_tot']}  "
          f"(threshold == 0)")
    print(f"expect_empty {m['empty_ok']}/{m['empty_tot']}")
    print(f"thresholds   precision>={MIN_PRECISION}  "
          f"ownership_err<{MAX_OWNERSHIP_ERROR_RATE}")

    if m["by_category"]:
        print("\n--- by category ---")
        for cat in sorted(m["by_category"]):
            c = m["by_category"][cat]
            c_prec = _pr(c["tp"], c["tp"] + c["fp"])
            c_rec = _pr(c["tp"], c["tp"] + c["fn"])
            own = ""
            if c["own_tot"]:
                own = (f"  ownership={c['own_ok']}/{c['own_tot']}")
            print(f"  {cat:16} n={c['n']}  "
                  f"P/R={c_prec:.2f}/{c_rec:.2f}{own}")

    if args.json:
        print("\n" + json.dumps(
            {k: v for k, v in m.items() if k != "by_category"}, indent=2))

    fails = _check_thresholds(m)
    if fails:
        print("\nFAIL thresholds:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\nPASS thresholds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
