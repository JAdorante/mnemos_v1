"""Eval for multi-task decomposition — does the splitter fan out correctly?

The headline metric is the DROPPED-TASK RATE: a mixed message must never lose a
secondary intention (the exact failure the single-goal path had). Secondary
metrics: task-count accuracy, surface-routing accuracy, and dependency accuracy.

The rule-gate half runs offline (deterministic). The decomposition half calls the
router-tier LLM (like eval_extraction), so it needs an API key.

    python scripts/eval_multitask.py            # full (LLM) run
    python scripts/eval_multitask.py --gate     # offline rule-gate check only

Ground truth is a small labeled set below (extend as new failure modes appear).
Cases use neutral placeholders (<name>, Acme) — no real user data.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.services import multitask as mt          # noqa: E402

# (message, expect_multi, n_tasks, surfaces(set, order-independent), has_dependency)
CASES = [
    # --- same-surface / single intent -> ONE task ---
    ("summarize this page", False, 1, {"browser"}, False),
    ("what do I owe <name>", False, 1, {"none"}, False),
    ("find the pricing and features page on Acme", False, 1, {"browser"}, False),
    # --- cross-surface independent -> TWO tasks, no dependency ---
    ("text <name> that I'm running late and find Acme's careers page",
     True, 2, {"phone_link", "browser"}, False),
    ("create a folder for Acme and find Acme's careers page",
     True, 2, {"desktop", "browser"}, False),
    # --- cross-surface DEPENDENT -> browser first, phone second ---
    ("find Acme's careers page and text the link to <name>",
     True, 2, {"browser", "phone_link"}, True),
    # --- mixed answer + action ---
    ("tell me what I owe <name> and draft the follow-up email",
     True, 2, {"none", "browser"}, False),
    # --- three independent ---
    ("open the Acme folder, find their careers page, and text <name> about it",
     True, 3, {"desktop", "browser", "phone_link"}, True),
]


def _gate_metrics():
    ok = 0
    for msg, expect_multi, *_ in CASES:
        got = mt.looks_multi(msg)
        # the rule gate is a pre-filter: it must FIRE on every true-multi case
        # (a miss there means a dropped task); firing on a single case is harmless.
        if expect_multi and not got:
            print(f"  [gate MISS] {msg!r} — would skip decomposition")
        elif expect_multi and got:
            ok += 1
    total_multi = sum(1 for c in CASES if c[1])
    print(f"rule-gate recall on multi cases: {ok}/{total_multi}")
    return ok == total_multi


def _run_llm():
    n = len(CASES)
    count_ok = surf_ok = dep_ok = 0
    dropped = 0
    total_expected_tasks = 0
    for msg, expect_multi, n_tasks, surfaces, has_dep in CASES:
        tasks = mt.decompose(msg)
        got_n = len(tasks)
        got_surfaces = {t.surface_hint for t in tasks if t.surface_hint}
        got_dep = any(t.depends_on for t in tasks)
        total_expected_tasks += n_tasks
        # dropped tasks: fewer atomic tasks than expected = a lost intention
        dropped += max(0, n_tasks - got_n)
        if got_n == n_tasks:
            count_ok += 1
        # surface recall: did every expected surface show up?
        if surfaces <= got_surfaces or (surfaces == {"none"} and not got_surfaces):
            surf_ok += 1
        if got_dep == has_dep:
            dep_ok += 1
        flag = "" if got_n == n_tasks else "  <-- COUNT OFF"
        print(f"  {msg[:60]!r:62} -> {got_n} task(s) {sorted(got_surfaces)}{flag}")

    print("\n=== multi-task decomposition eval ===")
    print(f"task-count accuracy : {count_ok}/{n}")
    print(f"surface routing     : {surf_ok}/{n}")
    print(f"dependency accuracy : {dep_ok}/{n}")
    drop_rate = dropped / total_expected_tasks if total_expected_tasks else 0.0
    print(f"DROPPED-TASK RATE   : {drop_rate:.2f}  ({dropped}/{total_expected_tasks})  "
          "<- the number that must trend to 0")
    return dropped == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Eval multi-task decomposition")
    ap.add_argument("--gate", action="store_true",
                    help="offline rule-gate check only (no LLM)")
    args = ap.parse_args()
    if args.gate:
        return 0 if _gate_metrics() else 1
    _gate_metrics()
    print()
    return 0 if _run_llm() else 1


if __name__ == "__main__":
    raise SystemExit(main())
