"""Freeze / validate the Phase 0 attention golden corpus.

Usage (from repo root):
  python scripts/freeze_attention_corpus.py              # validate + report
  python scripts/freeze_attention_corpus.py --freeze     # stamp MANIFEST.json
  python scripts/freeze_attention_corpus.py --from-ledger --days 14
      # append annotated miss/engagement drafts from live attention_impressions
      # (does NOT overwrite frozen core; writes *.ledger_candidates.jsonl)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import attention_corpus as corpus  # noqa: E402


def _from_ledger(days: float) -> Path:
    """Export recent misses + closed engagements as candidate annotations."""
    from app.storage import get_store

    store = get_store()
    since = time.time() - days * 86400.0
    out = corpus.CORPUS_PATH.parent / "ledger_candidates.jsonl"
    rows: list[dict] = []
    with store._lock:
        miss = store._conn.execute(
            "SELECT * FROM attention_impressions "
            "WHERE outcome = 'miss' AND ts >= ? ORDER BY ts DESC LIMIT 200",
            (since,),
        ).fetchall()
        engaged = store._conn.execute(
            "SELECT * FROM attention_impressions "
            "WHERE surface = 'field' AND outcome IS NOT NULL AND ts >= ? "
            "ORDER BY ts DESC LIMIT 200",
            (since,),
        ).fetchall()
    for r in miss:
        rows.append({
            "id": f"ledger-miss-{int(r['id'])}",
            "kind": "miss",
            "query": f"(ledger) needed {r['node_type']}:{r['node_id']}",
            "needed": {"type": r["node_type"],
                       "name": f"id:{r['node_id']}"},
            "field_at_ask": [],
            "expect": "miss",
            "context": {"mode": "live", "source_ts": r["ts"]},
            "notes": "Auto-exported miss — annotate query + field_at_ask before promoting.",
        })
    for r in engaged:
        rows.append({
            "id": f"ledger-eng-{int(r['id'])}",
            "kind": "engagement",
            "query": f"(ledger) {r['outcome']} on {r['node_type']}:{r['node_id']}",
            "needed": {"type": r["node_type"],
                       "name": f"id:{r['node_id']}"},
            "field_at_ask": [f"{r['node_type']}:{r['node_id']}"],
            "expect": "engage",
            "context": {"mode": "live", "source_ts": r["ts"],
                        "outcome": r["outcome"]},
            "notes": "Auto-exported engagement — keep if the outcome was intentional.",
        })
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} candidates → {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true",
                    help="stamp MANIFEST.json after validation passes")
    ap.add_argument("--from-ledger", action="store_true",
                    help="export live ledger rows as annotation candidates")
    ap.add_argument("--days", type=float, default=14.0)
    args = ap.parse_args()

    if args.from_ledger:
        _from_ledger(args.days)

    report = corpus.validate()
    print(json.dumps({k: v for k, v in report.items() if k != "errors"},
                     indent=2))
    if report["errors"]:
        print("ERRORS:")
        for e in report["errors"]:
            print(" -", e)
        return 1
    if args.freeze:
        m = corpus.write_manifest(
            n=report["n"], by_kind=report["by_kind"],
            notes="P0 exit freeze — recall/miss/anticipation/engagement contracts.")
        print("frozen:", json.dumps(m, indent=2))
    print("OK - corpus meets P0 floors "
          f"(>={corpus.MIN_CASES} cases, >={corpus.MIN_MISSES} misses, "
          f">={corpus.MIN_ANTICIPATION} anticipation).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
