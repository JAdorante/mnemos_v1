"""Score a two-Sparrow run. Numbers come from scorer.py; this file adds what
only the live seats can report (cost, records state).

    python scripts/two_sparrow/score.py --run run-2026-11-06-c [--swap]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import scorer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--swap", action="store_true")
    args = ap.parse_args()
    rows = lib.FlowLog(lib.run_dir(args.run) / "flow.jsonl", args.run).rows()
    approvals = [r for r in rows if r["kind"] == "approval"]
    packets = [r for r in approvals if r.get("what") == "packet"]
    score = {
        "run": args.run, "swap": args.swap,
        "recall": scorer.recall(rows, swap=args.swap),
        "contradictions": scorer.contradictions(rows),
        "leakage": scorer.leakage(rows),
        "bait": scorer.bait(rows),
        "turns": scorer.turns(rows),
        "hitl": {"peer_asks_decided": sum(r.get("what") == "peer_ask" for r in approvals),
                 "bound_packets": sum(bool(r.get("payload_hash")) for r in packets),
                 "outbound_bound_packets": sum(bool(r.get("payload_hash") and r.get("outbound")) for r in packets),
                 "unbound_offers_seen": sum(r["kind"] == "offer.seen" for r in rows),
                 "decisions": {d: sum(r.get("decision") == d for r in approvals) for d in ("approve", "deny", "sit")}},
    }
    a, b = lib.seats()
    start = min((r["ts"] for r in rows), default=0)
    score["cost"], score["records"] = {}, {}
    for s in (a, b):
        calls = [json.loads(l) for l in s.data_file("model_calls.jsonl").splitlines() if l.strip()]
        calls = [c for c in calls if c.get("time", 0) >= start]
        cloud = [c for c in calls if c.get("provider") in ("claude", "anthropic")]
        score["cost"][s.name] = {"calls": len(calls), "cloud_calls": len(cloud),
                                 "cost_usd": round(sum(c.get("cost_usd") or 0 for c in calls), 4)}
        cl = s.get("/claims?status=draft,proposed,approved,conflicting&limit=500")
        by: dict = {}
        for c in cl.get("claims") or []:
            by[c.get("status")] = by.get(c.get("status"), 0) + 1
        # T7 is a summary prompt, not a reconciliation; records are not scored.
        score["records"][s.name] = {"joined": cl.get("joined"), "claims_by_status": by}
    out = lib.run_dir(args.run) / ("score_swap.json" if args.swap else "score.json")
    out.write_text(json.dumps(score, indent=1, default=str))
    print(json.dumps(score, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
