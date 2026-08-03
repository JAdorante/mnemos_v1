"""Golden eval for #5 — does the planner engage for the CORE workflows only?

The planner is now ON by default for a locked allowlist (follow-up email, meeting
brief, to-do -> action) and OFF for everything else. This checks that gate is
right on both sides: the three workflows compile through the Personal Agent Layer,
and ordinary reads / searches / small talk stay on the raw path. Deterministic and
LLM-free (the gate is a cheap heuristic), so it runs in milliseconds.

    python scripts/eval_planner_routing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import app.*

# (goal, has_fact, surface, expect_plan, expect_workflow)
CASES = [
    # --- follow-up email workflow -> plan ---
    ("draft a follow-up email to Justin about pricing", False, None, True, "follow_up_email"),
    ("reply to Marc's email", False, None, True, "follow_up_email"),
    ("send Chris the deck", False, None, True, "follow_up_email"),
    ("write a note to the landlord", False, None, True, "follow_up_email"),
    # --- meeting brief workflow -> plan (read-only, surface none) ---
    ("prep me for my meeting with Marc", False, None, True, "meeting_brief"),
    ("brief me before my 1:1 with Sarah", False, None, True, "meeting_brief"),
    ("what's the agenda for the sync", False, None, True, "meeting_brief"),
    # --- to-do -> action workflow: a stored task, normal surface -> plan ---
    ("book the venue for Friday", True, None, True, "todo_action"),
    ("renew the passport", True, "browser", True, "todo_action"),
    # --- to-do on a dedicated surface stays RAW (already well-routed) ---
    ("text dad I'm running late", True, "phone_link", False, "todo_action"),
    ("open notepad and type the notes", True, "desktop", False, "todo_action"),
    # --- NOT core workflows -> raw path ---
    ("what's on my calendar today", False, None, False, None),
    ("look up the weather", False, None, False, None),
    ("book the venue for Friday", False, None, False, None),   # typed, no fact -> raw
    ("how was your weekend", False, None, False, None),
]


def main() -> int:
    from app.services.agent_planner import core_workflow_for
    from app.services.agent_bridge import _should_plan

    wf_ok = plan_ok = 0
    fails: list[str] = []
    for goal, has_fact, surface, exp_plan, exp_wf in CASES:
        wf = core_workflow_for(goal, has_fact=has_fact)
        plan = _should_plan(goal, 1 if has_fact else None, surface)
        wf_hit = (wf == exp_wf)
        plan_hit = (plan == exp_plan)
        wf_ok += wf_hit
        plan_ok += plan_hit
        mark = "" if (wf_hit and plan_hit) else "   <-- MISMATCH"
        if mark:
            fails.append(goal)
        print(f"  plan={plan!s:5} wf={str(wf):16} "
              f"(want plan={exp_plan!s:5} wf={exp_wf}){mark}  :: {goal[:44]!r}")

    n = len(CASES)
    print("\n=== planner routing eval ===")
    print(f"plan decision correct:  {plan_ok}/{n}")
    print(f"workflow label correct: {wf_ok}/{n}")
    if fails:
        print("MISMATCHES:", fails)
    return 1 if (plan_ok < n or wf_ok < n) else 0


if __name__ == "__main__":
    sys.exit(main())
