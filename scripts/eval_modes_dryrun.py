"""Eval: agent modes + dry-run postures on real, no-login pages (Track B #3/#8).

Runs a handful of real tasks and shows the policy layer firing live — which
mode is resolved, how far each dry-run level lets the agent go, and where the
commit gate stops it. All targets are public and need no sign-in, so it runs
unattended (no approval prompts: the levels used here never ask).

    python scripts/eval_modes_dryrun.py            # headless
    python scripts/eval_modes_dryrun.py --show      # watch the browser
    python scripts/eval_modes_dryrun.py --only form # run one scenario

Needs ANTHROPIC_API_KEY (or `ant auth login`) and Chromium.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# (key, title, start_url, goal, dry_run, what-to-look-for)
# Targets force a real browser task (the answer isn't in the model's memory —
# it's on the live page) and use text inputs + a Submit button (which verify
# cleanly, unlike radios/checkboxes) so the commit gate is actually reached.
_FORM = "https://www.selenium.dev/selenium/web/web-form.html"

SCENARIOS = [
    ("plan", "plan-only (returns a plan, no browser actions)",
     _FORM,
     "Look at the form currently open in the browser and give me a step-by-step "
     "plan to fill in its text field and message box and submit it.",
     "plan",
     "Returns a PLAN only, status=plan_only, 0 steps executed."),

    ("navigate", "navigate-only (read the live page, never mutate)",
     _FORM,
     "Look at the web form currently open in the browser and list every input "
     "field label you can see on it.",
     "navigate",
     "Reads the live page and lists the fields; no type/click; answer grounded "
     "in the actual page."),

    ("form", "Form mode + draft-only (fill, STOP before Submit)",
     _FORM,
     "In the form on this page, type 'John Doe' into the text input and "
     "'Please deliver by 8pm' into the message textarea, then submit the form.",
     "draft",
     "Mode=Form; types both fields, then STOPS at the Submit gate without "
     "clicking it (draft-only). status=stopped_draft."),
]


def _run(scn, headless: bool) -> dict:
    key, title, url, goal, level, expect = scn
    from browser_agent.orchestrator import Agent

    print("\n" + "=" * 66)
    print(f"  [{key}] {title}")
    print(f"  url      : {url}")
    print(f"  goal     : {goal}")
    print(f"  dry_run  : {level}")
    print(f"  expect   : {expect}")
    print("-" * 66)
    agent = Agent(headless=headless, start_url=url)
    try:
        result, status = agent.run_goal(goal, dry_run=level)
        mode = agent.last_mode.label if agent.last_mode else "?"
        print(f"\n  -> mode={mode}  status={status}  "
              f"steps={agent.last_steps}  cost=${agent.cost():.4f}")
        print("  -> result:")
        for line in (result or "").strip().splitlines() or ["(empty)"]:
            print("     " + line)
        return {"key": key, "mode": mode, "status": status,
                "steps": agent.last_steps, "cost": agent.cost()}
    except Exception as exc:
        print(f"  -> ERROR: {type(exc).__name__}: {exc}")
        return {"key": key, "mode": "?", "status": f"error:{type(exc).__name__}",
                "steps": 0, "cost": agent.cost()}
    finally:
        agent.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Test modes + dry-run on real pages")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    ap.add_argument("--only", default=None, help="run one scenario by key "
                    "(plan|navigate|form)")
    args = ap.parse_args(argv[1:])

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ANTHROPIC_API_KEY is not set — add it to .env.", file=sys.stderr)
        return 2

    scenarios = SCENARIOS
    if args.only:
        scenarios = [s for s in SCENARIOS if s[0] == args.only]
        if not scenarios:
            print(f"no scenario '{args.only}' (choose plan|navigate|form)", file=sys.stderr)
            return 2

    rows = [_run(s, headless=not args.show) for s in scenarios]

    print("\n" + "=" * 66)
    print("  SUMMARY")
    print("-" * 66)
    total = 0.0
    for r in rows:
        total += r["cost"]
        print(f"  {r['key']:9} mode={r['mode']:9} status={r['status']:20} "
              f"steps={r['steps']:>2}  ${r['cost']:.4f}")
    print("-" * 66)
    print(f"  total cost: ${total:.4f}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
