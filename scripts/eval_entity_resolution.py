"""Golden eval #2 — entity resolution + threshold sweep (plan 2.3).

Scores People v2 `resolve_person_mention` on
`tests/fixtures/goldens/entity_resolution.jsonl`.

Primary gate (merge-error ≈ 0):
  * merge_error_rate ≤ MAX_MERGE_ERROR_RATE  (false auto-resolve)
Secondary:
  * decision accuracy on labeled cases

Threshold sweep precomputes candidate scores once per case, then re-applies
`decide_from_scores` across the AUTO_RESOLVE / MARGIN / CREATE_NEW grid.

Decision gate:
  * green  → code default QUILL_PEOPLE_V2=1 justified
  * red    → set QUILL_PEOPLE_V2=0 until recalibrated

    python scripts/eval_entity_resolution.py
    python scripts/eval_entity_resolution.py --sweep
    python scripts/eval_entity_resolution.py --json

Exit 0 on pass, 1 on threshold failure or missing golden.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GOLDEN = (Path(__file__).resolve().parent.parent
          / "tests" / "fixtures" / "goldens" / "entity_resolution.jsonl")

# Architecture §MVP: false-merge ≤ 0.5%. Plan 2.3: merge-error ≈ 0.
MAX_MERGE_ERROR_RATE = 0.005
MIN_DECISION_ACCURACY = 0.85
MIN_CASES = 80

DEFAULT_AUTO = 0.92
DEFAULT_MARGIN = 0.15
DEFAULT_CREATE = 0.85


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


def _seed_roster(store, roster: list[dict], now: float) -> dict[str, int]:
    name_to_id: dict[str, int] = {}
    pending_absorbed: list[tuple[int, str]] = []
    for row in roster or []:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        pid = store.insert_person(
            name, ts=now,
            promotion_state=row.get("promotion_state") or "active")
        name_to_id[name] = pid
        for alias in row.get("aliases") or []:
            if alias and alias.strip():
                store.touch_person(pid, now, alias=alias.strip())
        ref = row.get("canonical_person_id")
        if isinstance(ref, str) and ref.startswith("REF:"):
            pending_absorbed.append((pid, ref[4:].strip()))
    for pid, target_name in pending_absorbed:
        target = name_to_id.get(target_name)
        if target:
            try:
                store.soft_merge_people(
                    target, pid, reason="golden absorbed", actor="eval")
            except Exception:
                try:
                    store._conn.execute(
                        "UPDATE people SET hide_from_people = 1, "
                        "canonical_person_id = ? WHERE id = ?",
                        (target, pid))
                    store._conn.commit()
                except Exception:
                    pass
    return name_to_id


class _identity:
    def __init__(self, name: str):
        self.name = name
        self._patch = None

    def __enter__(self):
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


def _prepare_case(case: dict) -> dict:
    """Seed roster once; capture early reject/self or candidate scores."""
    from app.services import name_quality as nq
    from app.services import people_pipeline as pp
    from app.services import self_profile
    from app.services import source_policy as sp
    from app.storage import Store

    self_profile.reset()
    tmp = Path(tempfile.mkdtemp(prefix="quill_er_"))
    store = Store(db_path=tmp / "eval.db", audio_dir=tmp / "audio")
    now = time.time()
    mention = case.get("mention") or ""
    text = case.get("text") or ""
    event_source = case.get("event_source") or "audio.whisper"
    window = case.get("window") or ""
    boost = float(case.get("relationship_boost") or 0.7)

    try:
        with patch.dict(os.environ, {
            "QUILL_PEOPLE_V2": "1",
            "USERNAME": "Dell AI User",
        }, clear=False):
            try:
                if hasattr(nq._os_account_names, "cache_clear"):
                    nq._os_account_names.cache_clear()
            except Exception:
                pass

            with _identity(case.get("enrolled_user") or "Hugh"):
                _seed_roster(store, case.get("roster") or [], now)
                raw = mention.strip()
                if not raw:
                    return {"case": case, "fixed": ("reject", None, 0.0)}

                if self_profile.is_self_name(raw):
                    return {"case": case, "fixed": ("self", None, 1.0)}

                display = nq.normalize_person_name(raw) or raw
                if nq.is_os_account_name(display) or not nq.is_plausible_person(display):
                    return {"case": case, "fixed": ("reject", None, 0.0)}

                policy = sp.policy_for_event(
                    event_source=event_source, window=window, text=text)
                if not policy.extract_mentions:
                    return {"case": case, "fixed": ("reject", None, 0.0)}

                people = [
                    p for p in store.list_people_embed()
                    if not p.get("canonical_person_id")
                    and not p.get("hide_from_people")
                ]
                # Normalize list_people_embed shape → name key
                norm = []
                for p in people:
                    norm.append({
                        "id": p["id"],
                        "name": p.get("name") or p.get("canonical_name") or "",
                        "aliases": p.get("aliases") or [],
                    })
                scored = pp.score_person_candidates(display, norm)
                return {
                    "case": case,
                    "scored": scored,
                    "create_person_candidates": policy.create_person_candidates,
                    "boost": boost,
                    "display": display,
                }
    finally:
        try:
            store.close()
        except Exception:
            pass
        self_profile.reset()


def _score_prepared(prep: dict, *, auto: float, margin: float,
                    create: float) -> dict:
    from app.services import people_pipeline as pp

    case = prep["case"]
    if "fixed" in prep:
        decision, resolved_name, conf = prep["fixed"][0], None, prep["fixed"][2]
        # self has no person name in this offline path
        chosen_name = None
    else:
        decision, chosen_row, conf = pp.decide_from_scores(
            prep["scored"],
            relationship_boost=prep["boost"],
            create_person_candidates=prep["create_person_candidates"],
            auto_resolve=auto,
            auto_margin=margin,
            create_new=create,
        )
        chosen_name = (chosen_row or {}).get("name") if chosen_row else None
        # create_new doesn't bind a roster row
        if decision == "create_new":
            chosen_name = prep.get("display")

    exp = case.get("expect") or {}
    want_dec = exp.get("decision")
    want_person = exp.get("person")
    forbid = exp.get("forbid_person")

    decision_ok = decision == want_dec
    if want_dec == "auto_resolve" and want_person:
        decision_ok = decision_ok and (
            (chosen_name or "").lower() == want_person.lower())
    elif want_person and decision == "auto_resolve":
        decision_ok = decision_ok and (
            (chosen_name or "").lower() == want_person.lower())

    merge_error = False
    if decision == "auto_resolve":
        if forbid and chosen_name and chosen_name.lower() == forbid.lower():
            merge_error = True
        elif want_dec == "auto_resolve" and want_person:
            if (chosen_name or "").lower() != want_person.lower():
                merge_error = True
        elif want_dec in ("leave_open", "reject", "create_new"):
            if case.get("merge_sensitive", True):
                merge_error = True
        elif want_dec == "self":
            merge_error = True

    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "decision": decision,
        "resolved_name": chosen_name,
        "want_decision": want_dec,
        "decision_ok": bool(decision_ok),
        "merge_error": bool(merge_error),
        "confidence": conf,
    }


def run_eval(cases: list[dict], *, auto: float = DEFAULT_AUTO,
             margin: float = DEFAULT_MARGIN,
             create: float = DEFAULT_CREATE,
             prepared: list[dict] | None = None) -> dict:
    if prepared is None:
        prepared = [_prepare_case(c) for c in cases]

    tp_dec = 0
    merge_err = 0
    by_cat: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "ok": 0, "merge_err": 0})
    failures: list[dict] = []

    for prep in prepared:
        r = _score_prepared(prep, auto=auto, margin=margin, create=create)
        cat = r["category"] or "uncategorized"
        by_cat[cat]["n"] += 1
        if r["decision_ok"]:
            tp_dec += 1
            by_cat[cat]["ok"] += 1
        else:
            failures.append(r)
        if r["merge_error"]:
            merge_err += 1
            by_cat[cat]["merge_err"] += 1
            if r not in failures:
                failures.append(r)

    n = len(prepared)
    return {
        "n": n,
        "decision_accuracy": (tp_dec / n) if n else 1.0,
        "decision_ok": tp_dec,
        "merge_errors": merge_err,
        "merge_error_rate": (merge_err / n) if n else 0.0,
        "thresholds": {
            "auto_resolve": auto,
            "auto_margin": margin,
            "create_new": create,
        },
        "by_category": dict(by_cat),
        "failures": failures[:40],
    }


def sweep(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    prepared = [_prepare_case(c) for c in cases]
    autos = [0.85, 0.88, 0.90, 0.92, 0.95]
    margins = [0.05, 0.10, 0.15, 0.20]
    creates = [0.80, 0.85, 0.90]
    rows: list[dict] = []
    for auto, margin, create in itertools.product(autos, margins, creates):
        m = run_eval(cases, auto=auto, margin=margin, create=create,
                     prepared=prepared)
        rows.append({
            "auto_resolve": auto,
            "auto_margin": margin,
            "create_new": create,
            "merge_error_rate": m["merge_error_rate"],
            "merge_errors": m["merge_errors"],
            "decision_accuracy": m["decision_accuracy"],
            "n": m["n"],
        })
    rows.sort(key=lambda r: (
        r["merge_error_rate"],
        -r["decision_accuracy"],
        abs(r["auto_resolve"] - DEFAULT_AUTO),
        abs(r["auto_margin"] - DEFAULT_MARGIN),
        abs(r["create_new"] - DEFAULT_CREATE),
    ))
    return rows, prepared


def _check_thresholds(m: dict) -> list[str]:
    fails: list[str] = []
    if m["n"] < MIN_CASES:
        fails.append(f"need >={MIN_CASES} cases, got {m['n']}")
    if m["merge_error_rate"] > MAX_MERGE_ERROR_RATE:
        fails.append(
            f"merge_error_rate {m['merge_error_rate']:.4f} "
            f"> {MAX_MERGE_ERROR_RATE} (false auto-resolve)")
    if m["decision_accuracy"] < MIN_DECISION_ACCURACY:
        fails.append(
            f"decision_accuracy {m['decision_accuracy']:.3f} "
            f"< {MIN_DECISION_ACCURACY}")
    return fails


def _gate_message(m: dict) -> str:
    fails = _check_thresholds(m)
    if fails:
        return (
            "GATE RED — set QUILL_PEOPLE_V2=0 until thresholds recalibrate; "
            "keep legacy resolver.resolve_person as fallback.\n  "
            + "\n  ".join(fails)
        )
    return (
        "GATE GREEN — merge-error ~ 0 at current thresholds; "
        "code default QUILL_PEOPLE_V2=1 is justified "
        "(legacy resolver remains for QUILL_PEOPLE_V2=0)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Entity-resolution golden eval (plan 2.3)")
    parser.add_argument("--data", type=Path, default=GOLDEN)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sweep", action="store_true",
                        help="Grid-search AUTO_RESOLVE/MARGIN/CREATE_NEW")
    parser.add_argument("--auto", type=float, default=DEFAULT_AUTO)
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    parser.add_argument("--create", type=float, default=DEFAULT_CREATE)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show-failures", action="store_true")
    args = parser.parse_args()

    if not args.data.exists():
        print(f"[eval] missing golden: {args.data}")
        print("       run: python scripts/gen_entity_resolution_golden.py")
        return 1

    cases = _load(args.data, limit=args.limit)
    print(f"[eval] entity resolution — {len(cases)} cases from {args.data}")

    prepared = None
    if args.sweep:
        print("\n=== threshold sweep ===")
        rows, prepared = sweep(cases)
        print(f"{'auto':>6} {'margin':>6} {'create':>6}  "
              f"{'merge_err':>9} {'dec_acc':>7}  note")
        for i, r in enumerate(rows[:15]):
            note = ""
            if (r["auto_resolve"] == DEFAULT_AUTO
                    and r["auto_margin"] == DEFAULT_MARGIN
                    and r["create_new"] == DEFAULT_CREATE):
                note = "<- current"
            if i == 0:
                note = (note + " BEST").strip()
            print(f"{r['auto_resolve']:6.2f} {r['auto_margin']:6.2f} "
                  f"{r['create_new']:6.2f}  "
                  f"{r['merge_error_rate']:9.4f} "
                  f"{r['decision_accuracy']:7.3f}  {note}")
        best = rows[0]
        print(f"\nbest merge-safe: auto>={best['auto_resolve']} "
              f"margin>={best['auto_margin']} create>={best['create_new']} "
              f"(merge_err={best['merge_error_rate']:.4f}, "
              f"acc={best['decision_accuracy']:.3f})")

    m = run_eval(cases, auto=args.auto, margin=args.margin,
                 create=args.create, prepared=prepared)

    print("\n=== entity resolution golden eval ===")
    thr = m["thresholds"]
    print(f"thresholds   auto>={thr['auto_resolve']}  "
          f"margin>={thr['auto_margin']}  create>={thr['create_new']}")
    print(f"decision     accuracy={m['decision_accuracy']:.3f}  "
          f"({m['decision_ok']}/{m['n']})")
    print(f"merge-error  {m['merge_errors']}/{m['n']}  "
          f"rate={m['merge_error_rate']:.4f}  "
          f"(threshold <= {MAX_MERGE_ERROR_RATE})")

    if m["by_category"]:
        print("\n--- by category ---")
        for cat in sorted(m["by_category"]):
            c = m["by_category"][cat]
            acc = (c["ok"] / c["n"]) if c["n"] else 1.0
            print(f"  {cat:28} n={c['n']:3}  acc={acc:.2f}  "
                  f"merge_err={c['merge_err']}")

    if args.show_failures and m["failures"]:
        print("\n--- failures (sample) ---")
        for f in m["failures"][:20]:
            print(f"  {f['id']}: got={f['decision']}/{f['resolved_name']} "
                  f"want={f['want_decision']} merge_err={f['merge_error']}")

    print("\n" + _gate_message(m))

    if args.json:
        out = {k: v for k, v in m.items() if k != "failures"}
        print("\n" + json.dumps(out, indent=2))

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
