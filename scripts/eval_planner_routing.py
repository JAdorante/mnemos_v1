"""Planner eval (plan #5 + 5.2 graduation).

Covers:
  1. Core-workflow labels (`core_workflow_for`) — env-independent.
  2. Core-only gate when QUILL_PLANNER=0.
  3. Global planner default (QUILL_PLANNER unset/1) plans non-core goals.
  4. Multi-step compile → ≥2 packets (mocked MultiTask decompose).

    python scripts/eval_planner_routing.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (goal, has_fact, surface, expect_plan_when_core_only, expect_workflow)
CORE_CASES = [
    ("draft a follow-up email to Justin about pricing", False, None, True, "follow_up_email"),
    ("reply to Marc's email", False, None, True, "follow_up_email"),
    ("send Chris the deck", False, None, True, "follow_up_email"),
    ("write a note to the landlord", False, None, True, "follow_up_email"),
    ("prep me for my meeting with Marc", False, None, True, "meeting_brief"),
    ("brief me before my 1:1 with Sarah", False, None, True, "meeting_brief"),
    ("what's the agenda for the sync", False, None, True, "meeting_brief"),
    # Track D scheduling takes precedence over generic todo_action
    ("book the venue for Friday", True, None, True, "scheduling_propose"),
    ("renew the passport", True, "browser", True, "todo_action"),
    ("text dad I'm running late", True, "phone_link", False, "todo_action"),
    ("open notepad and type the notes", True, "desktop", False, "todo_action"),
    ("what's on my calendar today", False, None, False, None),
    ("look up the weather", False, None, False, None),
    ("book the venue for Friday", False, None, True, "scheduling_propose"),
    ("how was your weekend", False, None, False, None),
]


def _eval_labels() -> tuple[int, int, list[str]]:
    from app.services.agent_planner import core_workflow_for

    ok, fails = 0, []
    for goal, has_fact, _surface, _ep, exp_wf in CORE_CASES:
        wf = core_workflow_for(goal, has_fact=has_fact)
        hit = wf == exp_wf
        ok += hit
        if not hit:
            fails.append(f"wf {goal!r}: got {wf}, want {exp_wf}")
        print(f"  wf={str(wf):22} want={exp_wf}  :: {goal[:48]!r}"
              + ("" if hit else "   <-- MISMATCH"))
    return ok, len(CORE_CASES), fails


def _eval_core_only_gate() -> tuple[int, int, list[str]]:
    from app.services.agent_bridge import _should_plan

    ok, fails = 0, []
    with mock.patch.dict(os.environ, {
        "QUILL_PLANNER": "0",
        "QUILL_PLANNER_CORE": "1",
    }):
        for goal, has_fact, surface, exp_plan, _wf in CORE_CASES:
            plan = _should_plan(goal, 1 if has_fact else None, surface)
            hit = plan == exp_plan
            ok += hit
            if not hit:
                fails.append(f"core-gate {goal!r}: got {plan}, want {exp_plan}")
            print(f"  plan={plan!s:5} want={exp_plan!s:5}  :: {goal[:48]!r}"
                  + ("" if hit else "   <-- MISMATCH"))
    return ok, len(CORE_CASES), fails


def _eval_global_default() -> tuple[int, int, list[str]]:
    """With planner default ON, non-core browser goals should plan."""
    from app.services.agent_bridge import _should_plan, _planner_enabled

    fails: list[str] = []
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("QUILL_PLANNER", None)
        if not _planner_enabled():
            fails.append("QUILL_PLANNER code default is not ON")
            print("  planner default ON: FAIL")
            return 0, 1, fails
        # Non-core should plan under global default (_should_plan True for all).
        ok = 0
        n = 0
        for goal, _ in [
            ("what's on my calendar today", True),
            ("look up the weather", True),
            ("how was your weekend", True),
        ]:
            n += 1
            plan = _should_plan(goal, None, None)
            hit = plan is True
            ok += hit
            if not hit:
                fails.append(f"global {goal!r}: expected plan=True")
            print(f"  global plan={plan!s:5} want=True  :: {goal!r}"
                  + ("" if hit else "   <-- MISMATCH"))
        print(f"  planner default ON: ok")
        return ok, n, fails


def _eval_multistep_packets() -> tuple[int, int, list[str]]:
    from app.services import agent_planner as ap
    from app.services.multitask import AtomicTask

    fails: list[str] = []
    tasks = [
        AtomicTask(id="t1", text="Send Marc the pricing follow-up"),
        AtomicTask(id="t2", text="Prep me for my meeting with Marc"),
    ]
    ap._LLM = False
    try:
        with mock.patch("app.services.multitask.decompose", return_value=tasks):
            layer = ap.PersonalAgentLayer(store=mock.Mock())
            layer.select_context = lambda goal, person=None: ap.SelectedContext(
                memory_block="- Marc", source_fact_ids=[1])
            plan = layer.compile(
                "Send Marc the pricing follow-up and prep me for my meeting with Marc")
    finally:
        ap._LLM = None

    n_ok = 0
    checks = 3
    if len(plan.steps) >= 2:
        n_ok += 1
        print(f"  steps={len(plan.steps)} (>=2) ok")
    else:
        fails.append(f"expected >=2 steps, got {len(plan.steps)}")
        print(f"  steps={len(plan.steps)} (>=2) FAIL")
    if all(s.packet is not None for s in plan.steps):
        n_ok += 1
        print("  every step has a packet ok")
    else:
        fails.append("missing packet on a step")
        print("  every step has a packet FAIL")
    if all((s.packet.summary or s.packet.goal or s.goal) for s in plan.steps):
        n_ok += 1
        print("  packets carry goal/summary ok")
    else:
        fails.append("empty packet summary/goal")
        print("  packets carry goal/summary FAIL")
    return n_ok, checks, fails


def main() -> int:
    all_fails: list[str] = []
    print("=== 1. workflow labels ===")
    a_ok, a_n, f = _eval_labels()
    all_fails.extend(f)
    print("=== 2. core-only gate (QUILL_PLANNER=0) ===")
    b_ok, b_n, f = _eval_core_only_gate()
    all_fails.extend(f)
    print("=== 3. global planner default ===")
    c_ok, c_n, f = _eval_global_default()
    all_fails.extend(f)
    print("=== 4. multi-step compile ≥2 packets ===")
    d_ok, d_n, f = _eval_multistep_packets()
    all_fails.extend(f)

    print("\n=== planner eval (5.2) ===")
    print(f"workflow labels:     {a_ok}/{a_n}")
    print(f"core-only gate:      {b_ok}/{b_n}")
    print(f"global default:      {c_ok}/{c_n}")
    print(f"multi-step packets:  {d_ok}/{d_n}")
    if all_fails:
        print("FAILURES:")
        for x in all_fails:
            print(" ", x)
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
