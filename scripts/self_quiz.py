"""Run the self-quiz once from the command line (idle scheduling comes later).

Quizzes the local model on human-verified memories, auto-scores against the
stored fact, and writes failure rows (gold = the fact) into the distill trail.
Entirely local — no Claude calls. See app/services/self_quiz.py for policy.

Usage:
    python scripts/self_quiz.py                 # 20 most recent trusted facts
    python scripts/self_quiz.py --limit 50
    python scripts/self_quiz.py --dry-run       # score + report, write nothing
    python scripts/self_quiz.py --model llama3.2-mnemos --pass-sim 0.6
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--pass-sim", type=float, default=0.6)
    ap.add_argument("--model", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from app.services.self_quiz import run_quiz
    stats = run_quiz(limit=args.limit, pass_sim=args.pass_sim,
                     model=args.model, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(stats, indent=2))
        return
    if not stats.get("ok"):
        sys.exit(f"self-quiz aborted: {stats.get('reason')} "
                 f"(model={stats.get('model')})")
    print(f"self-quiz  model={stats['model']}  facts={stats['facts']}  "
          f"asked={stats['asked']}  passed={stats['passed']}  "
          f"failed={stats['failed']}  rows_written={stats['rows_written']}"
          + (f"  mean_sim={stats.get('mean_sim')}" if stats.get("sims") else "")
          + ("  [dry-run]" if stats["dry_run"] else ""))
    if stats["skipped_qgen"]:
        print(f"  ({stats['skipped_qgen']} question(s) skipped: empty or "
              "answer-leaking)")
    if stats["failed"] and not stats["dry_run"]:
        print("  failures are now training rows (auto-labeled; gold = the "
              "stored fact). progress: python scripts/distill_curate.py "
              "--no-dedupe-embed")


if __name__ == "__main__":
    main()
