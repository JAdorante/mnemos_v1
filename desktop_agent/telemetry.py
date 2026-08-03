"""Desktop telemetry (strategic doc #6) — measure reliability, don't assume it.

Every executed and refused desktop action is already written to the append-only
audit log by the driver. This turns that log into metrics: performance (launch
success, run-command success, actions per task, repeated-failure loops) and
safety (refusals bucketed by reason — jail escapes, unknown apps, blocked verbs,
shell attempts, ...). Pure read-model over the log; it never launches or mutates.

The classifier maps a free-text refusal reason to a stable category, so counts
survive wording changes. Records injectable for tests, so the metrics double as
evals: feed a synthetic trajectory, assert the numbers.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import config as cfg

# Ordered (reason substring -> category); first match wins. Substrings track the
# refusal strings raised in guards.py / driver.py; new reasons fall to "other".
_REASON_RULES: list[tuple[str, str]] = [
    ("budget", "budget_exhausted"),
    ("disabled in desktop access", "disabled_app"),
    ("not on allowlist or not installed", "unknown_app"),
    ("not found on this machine", "unknown_app"),
    ("refused by discovery policy", "unvetted_app"),
    ("not on the command allowlist", "blocked_verb"),
    ("blocked verb", "blocked_verb"),
    ("shell metacharacter", "shell_metachar"),
    ("sensitive", "secret_path"),
    ("secret", "secret_path"),
    ("traversal", "jail_escape"),
    ("escapes jail", "jail_escape"),
    ("outside jail", "jail_escape"),
    ("cannot open", "capability_mismatch"),
    ("does not open folders", "capability_mismatch"),
    ("pixel ui disabled", "ui_disabled"),
    ("file too large", "file_too_large"),
    ("timed out", "timeout"),
    ("exec error", "exec_error"),
    ("launch failed", "exec_error"),
    ("write failed", "exec_error"),
    ("empty command", "bad_input"),
    ("bad project name", "bad_input"),
    ("path is a directory", "bad_input"),
]

# How the reason categories roll up into the doc's safety counters.
_SAFETY_MAP = {
    "jail_escape": "jail_escape_attempts",
    "secret_path": "secret_path_attempts",
    "unknown_app": "unknown_app_attempts",
    "unvetted_app": "unvetted_app_attempts",
    "disabled_app": "disabled_app_attempts",
    "blocked_verb": "blocked_verb_attempts",
    "shell_metachar": "shell_attempts",
    "capability_mismatch": "capability_mismatch",
    "budget_exhausted": "budget_exhausted",
}


def classify_refusal(detail: str) -> str:
    """Map a refusal reason string to a stable category. 'other' if unrecognized."""
    low = (detail or "").lower()
    for needle, category in _REASON_RULES:
        if needle in low:
            return category
    return "other"


def load_audit(path: Path | None = None, window_s: float | None = None) -> list[dict]:
    """Parse the audit JSONL. `window_s` keeps only records newer than that."""
    path = path or (cfg.SESSIONS_ROOT / "desktop_audit.jsonl")
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    cutoff = (time.time() - window_s) if window_s else None
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if cutoff is not None and float(rec.get("ts", 0)) < cutoff:
            continue
        out.append(rec)
    return out


def _rate(num: int, den: int) -> float:
    return round(num / den, 3) if den else 0.0


def _action_of(rec: dict) -> str:
    return rec.get("action") or ("run_command" if "argv" in rec else "?")


def desktop_metrics(records: list[dict] | None = None, *,
                    path: Path | None = None,
                    window_s: float | None = None) -> dict:
    """Compute performance + safety metrics from audit records."""
    recs = records if records is not None else load_audit(path, window_s)

    executed = refused = nonzero = 0
    by_action: dict[str, dict] = {}
    refusals: dict[str, int] = {}
    launch_ok = launch_refused = 0
    run_ok = run_nonzero = run_refused = 0
    tasks: dict = {}
    repeated = 0
    prev_blocked_sig = None  # (task_id, action, category) of the previous refusal

    for rec in recs:
        outcome = rec.get("outcome")
        action = _action_of(rec)
        slot = by_action.setdefault(action, {"executed": 0, "refused": 0,
                                             "nonzero": 0})
        # per-task bookkeeping (only records that carry a task_id)
        tid = rec.get("task_id")
        task = None
        if tid is not None:
            task = tasks.setdefault(tid, {"actions": 0, "budget_exhausted": False})
            task["actions"] += 1

        if outcome == "ok":
            executed += 1
            slot["executed"] += 1
            if action == "launch_app":
                launch_ok += 1
            elif action == "run_command":
                run_ok += 1
            prev_blocked_sig = None
        elif outcome == "nonzero":
            nonzero += 1
            slot["nonzero"] += 1
            if action == "run_command":
                run_nonzero += 1
            prev_blocked_sig = None
        elif outcome == "blocked":
            refused += 1
            slot["refused"] += 1
            category = classify_refusal(rec.get("detail", ""))
            refusals[category] = refusals.get(category, 0) + 1
            if action == "launch_app":
                launch_refused += 1
            elif action == "run_command":
                run_refused += 1
            if category == "budget_exhausted" and task is not None:
                task["budget_exhausted"] = True
            sig = (tid, action, category)
            if sig == prev_blocked_sig:
                repeated += 1
            prev_blocked_sig = sig
        else:
            prev_blocked_sig = None

    safety = {v: 0 for v in dict.fromkeys(_SAFETY_MAP.values())}
    for category, count in refusals.items():
        key = _SAFETY_MAP.get(category)
        if key:
            safety[key] += count

    task_counts = [t["actions"] for t in tasks.values()]
    n_tasks = len(tasks)
    exhausted_tasks = sum(1 for t in tasks.values() if t["budget_exhausted"])

    total = executed + refused + nonzero
    return {
        "window_s": window_s,
        "totals": {
            "records": total,
            "executed": executed,
            "nonzero": nonzero,
            "refused": refused,
            "refusal_rate": _rate(refused, total),
        },
        "by_action": by_action,
        "launch": {
            "attempts": launch_ok + launch_refused,
            "success": launch_ok,
            "refused": launch_refused,
            "success_rate": _rate(launch_ok, launch_ok + launch_refused),
        },
        "run_command": {
            "ran": run_ok + run_nonzero,
            "success": run_ok,
            "nonzero": run_nonzero,
            "refused": run_refused,
            "success_rate": _rate(run_ok, run_ok + run_nonzero),
        },
        "refusals_by_reason": dict(sorted(refusals.items(),
                                          key=lambda kv: (-kv[1], kv[0]))),
        "safety": safety,
        "per_task": {
            "tasks": n_tasks,
            "avg_actions": round(sum(task_counts) / n_tasks, 2) if n_tasks else 0.0,
            "max_actions": max(task_counts) if task_counts else 0,
            "budget_exhaustion_rate": _rate(exhausted_tasks, n_tasks),
        },
        "repeated_failures": repeated,
    }


def format_report(m: dict) -> str:
    """A compact ASCII report of the metrics, for CLI / logs."""
    t = m["totals"]
    L = ["DESKTOP TELEMETRY" + (f" (last {int(m['window_s'])}s)"
         if m.get("window_s") else "")]
    L.append(f"  actions: {t['records']}  "
             f"(executed {t['executed']}, nonzero {t['nonzero']}, "
             f"refused {t['refused']} = {t['refusal_rate']:.0%})")
    lc = m["launch"]
    L.append(f"  launch:  {lc['success']}/{lc['attempts']} succeeded "
             f"({lc['success_rate']:.0%}), {lc['refused']} refused")
    rc = m["run_command"]
    L.append(f"  run_cmd: {rc['success']}/{rc['ran']} exit-0 "
             f"({rc['success_rate']:.0%}), {rc['refused']} refused")
    pt = m["per_task"]
    L.append(f"  tasks:   {pt['tasks']}  (avg {pt['avg_actions']} actions, "
             f"max {pt['max_actions']}, budget-exhaustion {pt['budget_exhaustion_rate']:.0%})")
    L.append(f"  repeated failed actions: {m['repeated_failures']}")
    if m["refusals_by_reason"]:
        L.append("  refusals by reason: " + ", ".join(
            f"{k}={v}" for k, v in m["refusals_by_reason"].items()))
    safety = {k: v for k, v in m["safety"].items() if v}
    L.append("  SAFETY: " + (", ".join(f"{k}={v}" for k, v in safety.items())
                             or "no unsafe attempts recorded"))
    return "\n".join(L)
