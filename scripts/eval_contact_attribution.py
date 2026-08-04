"""Golden eval — contact attribution (plan 2.4).

Loads tests/fixtures/goldens/contact_attribution.jsonl and checks:
  * misattribution rate ≈ 0  (write when expect skip/review/deny)
  * mandate sentences all pass
  * article/news surfaces deny contact mint (source_policy)
  * weak scores route to review, not auto-write

    python scripts/eval_contact_attribution.py

Exit 0 on pass, 1 on threshold failure.
"""
from __future__ import annotations

import argparse
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
          / "tests" / "fixtures" / "goldens" / "contact_attribution.jsonl")

MAX_MISATTRIBUTION_RATE = 0.005  # ≈ 0
MIN_MANDATE_PASS = 1.0
MIN_CASES = 50


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


def _norm_phone(v: str) -> str:
    import re
    return re.sub(r"[^\d+]", "", v or "")


def _value_match(got: str, want: str, kind: str) -> bool:
    if kind == "phone":
        g, w = _norm_phone(got), _norm_phone(want)
        return bool(g) and bool(w) and (g == w or w in g or g in w)
    g = (got or "").lower().rstrip(".,;:)>\"'")
    w = (want or "").lower().rstrip(".,;:)>\"'")
    return bool(g) and bool(w) and (g == w or w in g or g in w)


def _run_probe(store, case: dict, probe: dict, now: float) -> dict:
    from app.services import people_pipeline as pp
    from app.services import source_policy as sp

    name = probe["person_name"]
    pid = store.insert_person(name, ts=now, promotion_state="active")
    details = pp.attribute_contacts_detailed(
        case.get("text") or "",
        store=store, person_id=pid, person_name=name,
        event_id=1, now=now + 1,
        event_source=case.get("event_source") or "",
        window=case.get("window") or "",
    )
    expect = probe.get("expect")
    kind = probe.get("kind")
    value = probe.get("value")

    # Policy deny: whole call returns a single deny_policy row
    if expect == "deny_policy":
        pol = sp.policy_for_event(
            event_source=case.get("event_source") or "",
            window=case.get("window") or "",
            text=case.get("text") or "")
        denied = (not pol.extract_contacts) or any(
            d.action == "deny_policy" for d in details)
        mint_denied = not pol.create_person_candidates
        ok = denied and mint_denied
        return {
            "ok": ok,
            "misattrib": False,
            "expect": expect,
            "got": "deny_policy" if denied else (
                details[0].action if details else "none"),
            "mint_denied": mint_denied,
            "source_class": pol.source_class,
        }

    # Filter to the contact under test when specified
    relevant = details
    if kind or value:
        relevant = [
            d for d in details
            if d.action != "deny_policy"
            and (not kind or d.kind == kind)
            and (not value or _value_match(d.value, value, kind or d.kind))
        ]

    wrote = [d for d in relevant if d.action == "write"]
    reviewed = [d for d in relevant if d.action == "review"]

    if expect == "write":
        ok = bool(wrote)
        misattrib = False
        got = "write" if wrote else (
            reviewed[0].action if reviewed else (
                relevant[0].action if relevant else "none"))
    elif expect == "review":
        ok = bool(reviewed) and not wrote
        misattrib = bool(wrote)  # auto-write when should review
        got = "review" if reviewed and not wrote else (
            "write" if wrote else (
                relevant[0].action if relevant else "none"))
    elif expect == "skip":
        ok = not wrote
        misattrib = bool(wrote)  # wrote onto wrong person
        got = "write" if wrote else "skip"
    else:
        ok = False
        misattrib = False
        got = relevant[0].action if relevant else "none"

    return {
        "ok": ok,
        "misattrib": misattrib,
        "expect": expect,
        "got": got,
        "person": name,
    }


def run_eval(cases: list[dict]) -> dict:
    from app.services import people_pipeline as pp  # noqa: F401
    from app.storage import Store

    tmp = Path(tempfile.mkdtemp(prefix="quill_attr_"))
    # Fresh store per probe to avoid contact bleed across people.
    probe_tot = probe_ok = misattrib = 0
    mandate_tot = mandate_ok = 0
    article_deny_ok = article_deny_tot = 0
    by_cat: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "ok": 0, "misattrib": 0})
    failures: list[dict] = []

    with patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"}):
        for case in cases:
            cat = case.get("category") or "uncategorized"
            for probe in case.get("probes") or []:
                store = Store(db_path=tmp / f"{case['id']}_{probe['person_name']}.db",
                              audio_dir=tmp / "audio")
                now = time.time()
                try:
                    r = _run_probe(store, case, probe, now)
                finally:
                    try:
                        store.close()
                    except Exception:
                        pass

                probe_tot += 1
                by_cat[cat]["n"] += 1
                if r["ok"]:
                    probe_ok += 1
                    by_cat[cat]["ok"] += 1
                else:
                    failures.append({
                        "id": case.get("id"), "cat": cat, **r,
                        "text": (case.get("text") or "")[:80],
                    })
                if r.get("misattrib"):
                    misattrib += 1
                    by_cat[cat]["misattrib"] += 1

                if case.get("mandate"):
                    mandate_tot += 1
                    if r["ok"]:
                        mandate_ok += 1

                if cat in ("article_mint_deny", "news_contact_deny"):
                    article_deny_tot += 1
                    if r["ok"]:
                        article_deny_ok += 1

    return {
        "n_cases": len(cases),
        "n_probes": probe_tot,
        "probe_accuracy": (probe_ok / probe_tot) if probe_tot else 1.0,
        "probe_ok": probe_ok,
        "misattribution": misattrib,
        "misattribution_rate": (misattrib / probe_tot) if probe_tot else 0.0,
        "mandate_pass_rate": (mandate_ok / mandate_tot) if mandate_tot else 1.0,
        "mandate_ok": mandate_ok,
        "mandate_tot": mandate_tot,
        "article_deny_rate": (
            (article_deny_ok / article_deny_tot) if article_deny_tot else 1.0),
        "by_category": dict(by_cat),
        "failures": failures[:40],
    }


def _check(m: dict) -> list[str]:
    fails: list[str] = []
    if m["n_cases"] < MIN_CASES:
        fails.append(f"need >={MIN_CASES} cases, got {m['n_cases']}")
    if m["misattribution_rate"] > MAX_MISATTRIBUTION_RATE:
        fails.append(
            f"misattribution_rate {m['misattribution_rate']:.4f} "
            f"> {MAX_MISATTRIBUTION_RATE}")
    if m["mandate_tot"] and m["mandate_pass_rate"] < MIN_MANDATE_PASS:
        fails.append(
            f"mandate pass {m['mandate_pass_rate']:.3f} < {MIN_MANDATE_PASS} "
            f"({m['mandate_ok']}/{m['mandate_tot']})")
    if m.get("article_deny_rate", 1.0) < 1.0:
        fails.append(
            f"article/news mint-deny incomplete "
            f"(rate={m['article_deny_rate']:.3f})")
    return fails


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Contact-attribution golden eval (plan 2.4)")
    parser.add_argument("--data", type=Path, default=GOLDEN)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show-failures", action="store_true")
    args = parser.parse_args()

    if not args.data.exists():
        print(f"[eval] missing golden: {args.data}")
        print("       run: python scripts/gen_contact_attribution_golden.py")
        return 1

    cases = _load(args.data, limit=args.limit)
    print(f"[eval] contact attribution — {len(cases)} cases from {args.data}")
    m = run_eval(cases)

    print("\n=== contact attribution golden eval ===")
    print(f"probes       accuracy={m['probe_accuracy']:.3f}  "
          f"({m['probe_ok']}/{m['n_probes']})")
    print(f"misattrib    {m['misattribution']}/{m['n_probes']}  "
          f"rate={m['misattribution_rate']:.4f}  "
          f"(threshold <= {MAX_MISATTRIBUTION_RATE})")
    print(f"mandate      {m['mandate_ok']}/{m['mandate_tot']}  "
          f"pass_rate={m['mandate_pass_rate']:.3f}")
    print(f"articledeny  rate={m['article_deny_rate']:.3f}")

    if m["by_category"]:
        print("\n--- by category ---")
        for cat in sorted(m["by_category"]):
            c = m["by_category"][cat]
            acc = (c["ok"] / c["n"]) if c["n"] else 1.0
            print(f"  {cat:24} n={c['n']:3}  acc={acc:.2f}  "
                  f"misattrib={c['misattrib']}")

    if args.show_failures and m["failures"]:
        print("\n--- failures (sample) ---")
        for f in m["failures"][:25]:
            print(f"  {f['id']} [{f.get('person')}]: "
                  f"got={f['got']} want={f['expect']} | {f.get('text')}")

    if args.json:
        out = {k: v for k, v in m.items() if k != "failures"}
        print("\n" + json.dumps(out, indent=2))

    fails = _check(m)
    if fails:
        print("\nFAIL thresholds:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\nPASS thresholds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
