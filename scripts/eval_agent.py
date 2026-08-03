"""Browser-agent eval suite — repeatable tasks + tracked metrics.

    python scripts/eval_agent.py                 # routing tier only (cheap, no browser)
    python scripts/eval_agent.py --live          # + live read-only browser tasks
    python scripts/eval_agent.py --live --headed  # watch the live tier run
    python scripts/eval_agent.py --json out.json  # also write raw results

ROUTING tier metrics (per task + aggregate):
  browser_correct     — router got requires_browser right
  approval_correct    — router got requires_user_approval right
  approval_FN         — DANGEROUS: task needed approval but router said no
  latency, cost

LIVE tier metrics:
  success (expect_substring), steps, replans, latency, cost, status,
  approval_correct (did it gate iff it should), intervention_count

Needs ANTHROPIC_API_KEY. The routing tier never opens a browser.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env so ANTHROPIC_API_KEY is available (browser_agent doesn't do this
# itself, unlike app.config which pulls it in on import).
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from browser_agent import config as cfg
from browser_agent.eval_tasks import ROUTE_TASKS, LIVE_TASKS


def _fmt(x, n=3):
    return f"{x:.{n}f}"


# ------------------------------- routing tier ------------------------------
def run_routing() -> dict:
    from browser_agent.llm import LLM

    llm = LLM()
    rows = []
    print("=== ROUTING tier (no browser) ===")
    print(f"{'task':<20} {'browser':>8} {'approval':>9} {'intent':<22} {'lat':>6}")
    for t in ROUTE_TASKS:
        t0 = time.time()
        r = llm.route(t.goal)
        dt = time.time() - t0
        b_ok = bool(r.get("requires_browser")) == t.requires_browser
        a_ok = bool(r.get("requires_user_approval")) == t.requires_approval
        # dangerous miss: needed approval, router said no
        a_fn = t.requires_approval and not bool(r.get("requires_user_approval"))
        intent = r.get("intent", "")
        rows.append({
            "id": t.id, "intent": intent,
            "browser_expected": t.requires_browser,
            "browser_got": bool(r.get("requires_browser")), "browser_correct": b_ok,
            "approval_expected": t.requires_approval,
            "approval_got": bool(r.get("requires_user_approval")),
            "approval_correct": a_ok, "approval_false_negative": a_fn,
            "latency_s": round(dt, 3),
        })
        flag = "" if a_ok else ("  <-- FN (unsafe)" if a_fn else "  <-- FP")
        print(f"{t.id:<20} {('ok' if b_ok else 'X'):>8} "
              f"{('ok' if a_ok else 'X'):>9} {intent[:22]:<22} {_fmt(dt,2):>6}{flag}")

    n = len(rows)
    agg = {
        "n": n,
        "browser_acc": sum(r["browser_correct"] for r in rows) / n,
        "approval_acc": sum(r["approval_correct"] for r in rows) / n,
        "approval_false_negatives": sum(r["approval_false_negative"] for r in rows),
        "cost": round(llm.cost(), 4),
        "avg_latency_s": round(sum(r["latency_s"] for r in rows) / n, 3),
    }
    print(f"\n  browser accuracy   : {agg['browser_acc']*100:.0f}%  ({n} tasks)")
    print(f"  approval accuracy  : {agg['approval_acc']*100:.0f}%")
    print(f"  approval FNs (unsafe): {agg['approval_false_negatives']}  "
          f"(tasks that needed approval but weren't flagged)")
    print(f"  cost ${agg['cost']}   avg latency {agg['avg_latency_s']}s")
    return {"rows": rows, "aggregate": agg}


# ------------------------------- live tier ---------------------------------
def run_live(headed: bool) -> dict:
    from browser_agent.orchestrator import Agent

    rows = []
    print("\n=== LIVE tier (headless browser, read-only) ===")
    for t in LIVE_TASKS:
        interventions = {"n": 0}

        def on_ask(q):
            # Read-only tasks shouldn't need a human. If one asks (login/approval),
            # count it as an intervention and decline so the run can't hang.
            interventions["n"] += 1
            return "cancel"

        logs = []
        agent = Agent(headless=not headed, start_url=t.start_url,
                      on_log=lambda s: logs.append(s), on_ask=on_ask)
        t0 = time.time()
        try:
            result, status = agent.run_goal(t.goal)
        except Exception as e:
            result, status = f"(error) {type(e).__name__}: {e}", "error"
        dt = time.time() - t0
        route = agent.last_route or {}
        cost = agent.cost()
        agent.close()

        res_l = (result or "").lower()
        success = any(s.lower() in res_l for s in t.expect_substring)
        needed = bool(route.get("requires_user_approval"))
        approval_correct = (needed == t.should_need_approval)
        rows.append({
            "id": t.id, "status": status, "success": success,
            "steps": agent.last_steps, "replans": agent.last_replans,
            "latency_s": round(dt, 2), "cost": round(cost, 4),
            "approval_correct": approval_correct,
            "interventions": interventions["n"],
            "result_preview": (result or "")[:160],
        })
        mark = "PASS" if success else "FAIL"
        print(f"  [{mark}] {t.id:<18} status={status:<22} steps={agent.last_steps} "
              f"lat={_fmt(dt,1)}s ${_fmt(cost,4)} interv={interventions['n']}")
        print(f"         -> {(result or '')[:140]!r}")

    n = len(rows)
    agg = {
        "n": n,
        "success_rate": sum(r["success"] for r in rows) / n if n else 0,
        "avg_steps": round(sum(r["steps"] for r in rows) / n, 1) if n else 0,
        "total_cost": round(sum(r["cost"] for r in rows), 4),
        "avg_latency_s": round(sum(r["latency_s"] for r in rows) / n, 1) if n else 0,
        "total_interventions": sum(r["interventions"] for r in rows),
    }
    print(f"\n  success {agg['success_rate']*100:.0f}%   avg steps {agg['avg_steps']}   "
          f"total ${agg['total_cost']}   avg latency {agg['avg_latency_s']}s   "
          f"interventions {agg['total_interventions']}")
    return {"rows": rows, "aggregate": agg}


def main(argv: list[str]) -> None:
    live = "--live" in argv
    headed = "--headed" in argv
    out_path = None
    if "--json" in argv:
        i = argv.index("--json")
        if i + 1 < len(argv):
            out_path = argv[i + 1]

    results = {"routing": run_routing()}
    if live:
        results["live"] = run_live(headed)

    if out_path:
        Path(out_path).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\n[eval] wrote raw results to {out_path}")


if __name__ == "__main__":
    main(sys.argv)
