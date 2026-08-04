"""Golden eval #4 — query-type grounding routes (plan 3.3).

Loads tests/fixtures/goldens/grounding_routes.jsonl, seeds a temp store per
fixture, runs grounding.compose, and measures grounding rate:

  grounded = expected drawer content appears in block / sources

Also contrasts route-on vs route-forced-off for baseline_contrast cases
(AC: grounding rate ↑).

    python scripts/eval_grounding.py
    python scripts/eval_grounding.py --json

Exit 0 on pass, 1 on threshold failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GOLDEN = (Path(__file__).resolve().parent.parent
          / "tests" / "fixtures" / "goldens" / "grounding_routes.jsonl")

MIN_GROUNDING_RATE = 0.85
MIN_CASES = 12
NOW = 1_700_000_000.0


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


def _mk_store(td: str):
    from app.storage import Store
    return Store(Path(td) / "g.db")


def _seed_belief(store, *, speaker: str, subject: str, predicate: str,
                 obj: str, quote: str, ts: float = NOW) -> None:
    from app.services import kg_beliefs
    store.insert_person(speaker, ts=ts, promotion_state="active")
    subj = store.resolve_entity(subject, "other", ts=ts)
    oid = store.resolve_entity(obj, "other", ts=ts)
    kg_beliefs.record_from_claim(
        store, subj_type="entity", subj_id=subj,
        predicate=predicate, obj_type="entity", obj_id=oid,
        fact_id=1, confidence=0.9, ts=ts,
        quote=quote, source_class="private_conversation",
        speaker=speaker, speaker_is_source=False,
    )


def _seed_fixture(store, name: str) -> None:
    if name in (None, "", "empty"):
        return
    if name == "david_price":
        _seed_belief(
            store, speaker="David", subject="pilot plan",
            predicate="costs", obj="$49",
            quote="pilot plan is $49 a month")
        return
    if name == "david_chen_price":
        _seed_belief(
            store, speaker="David Chen", subject="pilot plan",
            predicate="costs", obj="$49",
            quote="David Chen said it costs $49")
        return
    if name == "marc_promise":
        _seed_belief(
            store, speaker="Marc", subject="demo link",
            predicate="due_on", obj="Friday",
            quote="Marc promised to send the demo link by Friday")
        return
    if name == "justin_price":
        _seed_belief(
            store, speaker="Justin", subject="seat",
            predicate="priced_at", obj="$55",
            quote="Justin mentioned seats at $55")
        return
    if name in ("field_week", "field_aging"):
        store.add_field_snapshot(
            version="v1", ts=NOW - 8 * 86400,
            focus_ids=["person:1", "fact:1"],
            periphery_ids=["entity:1"],
            per_node={
                "person:1": {"gravity_total": 0.5, "kind": "person"},
                "fact:1": {"gravity_total": 0.6, "kind": "task"},
                "entity:1": {"gravity_total": 0.2, "kind": "tool"},
            },
        )
        store.add_field_snapshot(
            version="v2", ts=NOW,
            focus_ids=["person:1", "person:2", "entity:1"],
            periphery_ids=[],
            per_node={
                "person:1": {"gravity_total": 0.85, "kind": "person"},
                "person:2": {"gravity_total": 0.7, "kind": "person"},
                "entity:1": {"gravity_total": 0.55, "kind": "tool"},
                "fact:1": {"gravity_total": 0.3, "kind": "task"},
            },
        )
        store.add_reflection(
            scope="daily",
            period_start=NOW - 2 * 86400,
            period_end=NOW - 86400,
            summary="Pricing talk moved into focus",
            created_at=NOW - 86400,
        )
        if name == "field_aging":
            store.add_task(
                "Stale commitment from last month",
                confidence=0.9, extracted_at=NOW - 10 * 86400)
        return


def _score_case(case: dict, *, force_default: bool = False) -> dict:
    from app.services import grounding as gr

    # Keep the eval offline + fast: no embed model, no WM rebuild, no activity.
    patches = [
        patch("app.services.memory.memory.search", return_value=[]),
        patch("app.services.activity.describe_recent", return_value=[]),
        patch("app.services.working_memory.current",
              return_value={"slots": [], "person_ids": [], "person_labels": [],
                            "project_ids": [], "project_labels": [],
                            "fact_ids": []}),
        patch("app.services.working_memory.ensure_fresh"),
        patch("app.services.working_memory.snapshot", return_value=[]),
        patch("app.services.working_memory.render_lines", return_value=[]),
        patch("app.services.onboarding.load_profile", return_value=None),
        patch("app.services.self_profile.profile_lines", return_value=[]),
        patch("time.time", return_value=NOW),
    ]

    with tempfile.TemporaryDirectory() as td:
        store = _mk_store(td)
        try:
            _seed_fixture(store, case.get("fixture") or "empty")
            q = case["q"]
            from contextlib import ExitStack
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                if force_default:
                    stack.enter_context(patch.object(
                        gr, "classify_query_route",
                        return_value={
                            "route": "default", "speaker": None,
                            "since": None, "via": "forced",
                        },
                    ))
                out = gr.compose(
                    q, store=store, allow_llm_route=False,
                    record_attention=False)
        finally:
            store.close()

    block = out.get("block") or ""
    labels = [s.get("label") or "" for s in (out.get("sources") or [])]
    route = (out.get("route") or {}).get("route") or "default"
    expected = case.get("expected_route") or "default"

    route_ok = route == expected
    missing = [m for m in (case.get("must_contain") or [])
               if m not in block]
    forbidden = [m for m in (case.get("must_not_contain") or [])
                 if m in block]
    src_sub = case.get("must_source_substr")
    src_ok = True
    if src_sub:
        src_ok = any(src_sub in lab for lab in labels)
    if case.get("must_not_route_section"):
        src_ok = not any(
            lab.startswith("beliefs from") or lab.startswith("changes since")
            for lab in labels
        )
    if case.get("allow_empty_route_section") and expected == "speaker_beliefs":
        # Route detected but no beliefs for that speaker — still grounded if
        # route classification is correct (section may be absent).
        content_ok = not missing and not forbidden
    else:
        content_ok = not missing and not forbidden and src_ok

    grounded = bool(route_ok and content_ok)
    return {
        "id": case.get("id"),
        "grounded": grounded,
        "route_ok": route_ok,
        "route": route,
        "expected": expected,
        "missing": missing,
        "forbidden": forbidden,
        "src_ok": src_ok,
        "labels": labels,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if not GOLDEN.is_file():
        print(f"FAIL: missing golden {GOLDEN}")
        return 1
    cases = _load(GOLDEN, limit=args.limit)
    if len(cases) < MIN_CASES:
        print(f"FAIL: need ≥{MIN_CASES} cases, got {len(cases)}")
        return 1

    results = [_score_case(c) for c in cases]
    n = len(results)
    grounded_n = sum(1 for r in results if r["grounded"])
    rate = grounded_n / n

    # AC: grounding rate ↑ vs forced-default baseline on contrast cases.
    contrast = [c for c in cases if c.get("baseline_contrast")]
    lifted = 0
    for c in contrast:
        with_r = _score_case(c, force_default=False)
        without = _score_case(c, force_default=True)
        if with_r["grounded"] and not without["grounded"]:
            lifted += 1

    failed = [r for r in results if not r["grounded"]]
    summary = {
        "cases": n,
        "grounded": grounded_n,
        "grounding_rate": round(rate, 4),
        "min_grounding_rate": MIN_GROUNDING_RATE,
        "baseline_contrast": len(contrast),
        "lifted_vs_default": lifted,
        "failed": [{"id": f["id"], "route": f["route"],
                    "expected": f["expected"], "missing": f["missing"]}
                   for f in failed[:12]],
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"grounding_rate={rate:.1%} ({grounded_n}/{n}) "
              f"threshold={MIN_GROUNDING_RATE:.0%}")
        print(f"baseline_contrast lifted={lifted}/{len(contrast)}")
        for f in failed[:8]:
            print(f"  FAIL {f['id']}: route={f['route']} "
                  f"expected={f['expected']} missing={f['missing']}")

    ok = rate >= MIN_GROUNDING_RATE and (
        not contrast or lifted >= max(1, len(contrast) // 2)
    )
    if not ok:
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
