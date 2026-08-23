#!/usr/bin/env python
"""Cohort checkpoint table from a folder of tester usage JSONs (WS-A tier 3).

This is the artifact the Oct 1 kill-or-continue decision reads. Feed it a
directory of `usage-<install_id>-<day>.json` files — from either sharing tier
(a tester emailing the manual report, or the opt-in weekly ping's collector
drop) — and it prints per-install WAU weeks, week-2 retention, and the cohort
totals against the two gates: **>= 8 weekly-active users** and **>= 25% week-2
retention**.

    python scripts/pilot_metrics.py data/pilot_reports
    python scripts/pilot_metrics.py data/pilot_reports --as-of 2025-10-01 --json

Duplicates are expected and handled: several reports from one install collapse
to the newest, and the day rows are unioned so a lost report costs nothing.

Everything is UTC, matching the ledger. A week is 7 days from the install day
(day 1 = install day), so "week 2" means days 8-14 for that install and not a
calendar week — testers install on different days.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.usage_ledger import (  # noqa: E402
    day_is_active, metrics_from_rows, utc_day,
)

WAU_GATE = 8
RETENTION_GATE = 0.25


def load_reports(folder: Path) -> dict[str, dict[str, Any]]:
    """Merge every JSON in `folder` into one record per install id.

    Day rows are unioned across reports (keeping the highest counts for a
    repeated day), so an install that shared twice is not double counted and
    an install whose latest report went missing keeps its earlier history.
    """
    installs: dict[str, dict[str, Any]] = {}
    for path in sorted(folder.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ! skipping {path.name}: {exc}", file=sys.stderr)
            continue
        if not isinstance(payload, dict) or payload.get("schema") != "mnemos.usage/1":
            print(f"  ! skipping {path.name}: not a mnemos.usage/1 report",
                  file=sys.stderr)
            continue
        iid = str(payload.get("install_id") or "").strip()
        if not iid:
            continue
        rec = installs.setdefault(iid, {
            "install_id": iid, "days": {}, "installed_at_day": None,
            "version": None, "os": None, "reports": 0,
            "latest_report_day": None,
        })
        rec["reports"] += 1
        for row in payload.get("days") or []:
            day = str(row.get("day") or "")
            if not day:
                continue
            prev = rec["days"].get(day)
            # Counters only grow within a day, so the larger row is the later one.
            if prev is None or _weight(row) >= _weight(prev):
                rec["days"][day] = row
        # Earliest install date wins: a report written before data/ was ever
        # lost is the more truthful one.
        stamp = payload.get("installed_at_day")
        if stamp and (rec["installed_at_day"] is None
                      or str(stamp) < rec["installed_at_day"]):
            rec["installed_at_day"] = str(stamp)
        gen = payload.get("generated_at_day")
        if gen and (rec["latest_report_day"] is None
                    or str(gen) > rec["latest_report_day"]):
            rec["latest_report_day"] = str(gen)
            rec["version"] = payload.get("version")
            rec["os"] = payload.get("os")
    return installs


def _weight(row: dict[str, Any]) -> int:
    total = 0
    for v in row.values():
        if isinstance(v, int):
            total += v
    return total


def summarize(installs: dict[str, dict[str, Any]], *,
              as_of: float) -> list[dict[str, Any]]:
    out = []
    for iid, rec in sorted(installs.items()):
        rows = [rec["days"][d] for d in sorted(rec["days"])]
        install_day = rec["installed_at_day"] or (
            sorted(rec["days"])[0] if rec["days"] else utc_day(as_of))
        m = metrics_from_rows(rows, install_day=install_day, now=as_of)
        active = sorted(d for d in rec["days"] if day_is_active(rec["days"][d]))
        # Which install-relative weeks had at least one active day. A "WAU
        # week" for one install is days 1-7, 8-14, ... from ITS install day.
        weeks = sorted({_week_of(d, install_day) for d in active})
        out.append({
            "install_id": iid,
            "short": iid[:8],
            "install_day": install_day,
            "os": rec["os"], "version": rec["version"],
            "reports": rec["reports"],
            "last_report": rec["latest_report_day"],
            "active_days": len(active),
            "wau_weeks": weeks,
            "is_wau": m["is_wau"],
            "active_days_last_7": m["active_days_last_7"],
            "wk1_active_days": m["wk1_active_days"],
            "wk2_active_days": m["wk2_active_days"],
            "retained_wk2": m["retained_wk2"],
            "wk2_complete": m["wk2_complete"],
            "totals": m["totals"],
        })
    return out


def _week_of(day: str, install_day: str) -> int:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    b = datetime.strptime(install_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return max(1, (d - b).days // 7 + 1)


def cohort(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    wau = [s for s in summaries if s["is_wau"]]
    # Only installs whose week 2 has actually elapsed belong in the retention
    # denominator — a tester who installed three days ago cannot have churned.
    eligible = [s for s in summaries if s["wk2_complete"]]
    retained = [s for s in eligible if s["retained_wk2"]]
    rate = (len(retained) / len(eligible)) if eligible else 0.0
    return {
        "installs": len(summaries),
        "wau": len(wau),
        "wau_gate": WAU_GATE,
        "wau_pass": len(wau) >= WAU_GATE,
        "wk2_eligible": len(eligible),
        "wk2_retained": len(retained),
        "wk2_rate": rate,
        "retention_gate": RETENTION_GATE,
        "retention_pass": bool(eligible) and rate >= RETENTION_GATE,
        "wk2_pending": len(summaries) - len(eligible),
    }


def print_table(summaries: list[dict[str, Any]], totals: dict[str, Any],
                as_of_day: str) -> None:
    print(f"\nMnemos pilot — checkpoint as of {as_of_day} (UTC)")
    print("=" * 96)
    head = (f"{'install':<10}{'installed':<12}{'os':<9}{'ver':<8}"
            f"{'active':>7}{'last7':>7}{'wk1':>5}{'wk2':>5}{'WAU':>5}"
            f"{'ret':>6}  weeks")
    print(head)
    print("-" * 96)
    for s in summaries:
        ret = "-" if not s["wk2_complete"] else ("yes" if s["retained_wk2"] else "no")
        print(f"{s['short']:<10}{s['install_day']:<12}"
              f"{(s['os'] or '?'):<9}{(s['version'] or '?'):<8}"
              f"{s['active_days']:>7}{s['active_days_last_7']:>7}"
              f"{s['wk1_active_days']:>5}{s['wk2_active_days']:>5}"
              f"{('yes' if s['is_wau'] else 'no'):>5}{ret:>6}  "
              f"{','.join(str(w) for w in s['wau_weeks']) or '-'}")
    print("-" * 96)
    print(f"installs: {totals['installs']}")
    print(f"weekly active: {totals['wau']} / gate {totals['wau_gate']}  "
          f"-> {'PASS' if totals['wau_pass'] else 'FAIL'}")
    den = totals["wk2_eligible"]
    print(f"week-2 retention: {totals['wk2_retained']}/{den} = "
          f"{totals['wk2_rate'] * 100:.0f}% / gate "
          f"{totals['retention_gate'] * 100:.0f}%  "
          f"-> {'PASS' if totals['retention_pass'] else 'FAIL'}")
    if totals["wk2_pending"]:
        print(f"  ({totals['wk2_pending']} install(s) still inside week 2 — "
              "excluded from the denominator, not counted as churn)")
    if not den:
        print("  (no install has finished week 2 yet — retention is unmeasured, "
              "not zero)")
    print()
    agg = {}
    for s in summaries:
        for k, v in s["totals"].items():
            agg[k] = agg.get(k, 0) + v
    print("cohort activity totals: " + ", ".join(
        f"{k}={v}" for k, v in sorted(agg.items()) if v))
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, help="directory of tester usage JSONs")
    ap.add_argument("--as-of", default=None,
                    help="evaluate the windows on this UTC day (YYYY-MM-DD); "
                         "default now")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if not args.folder.is_dir():
        print(f"not a directory: {args.folder}", file=sys.stderr)
        return 2
    if args.as_of:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").replace(
            hour=23, minute=59, tzinfo=timezone.utc).timestamp()
    else:
        as_of = datetime.now(tz=timezone.utc).timestamp()

    installs = load_reports(args.folder)
    summaries = summarize(installs, as_of=as_of)
    totals = cohort(summaries)
    if args.json:
        print(json.dumps({"as_of": utc_day(as_of), "cohort": totals,
                          "installs": summaries}, indent=2, sort_keys=True))
    else:
        print_table(summaries, totals, utc_day(as_of))
    # Exit 0 always: this reports, it does not gate CI.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
