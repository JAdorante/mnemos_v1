"""People v2 live smoke — sample recent mentions from local quill.db.

No LLM. Reports merge/create/leave rates for recent person_mentions (or
contact-candidate text when the mention ledger is empty).

Usage:
    .venv/Scripts/python.exe scripts/eval_people_live.py
    .venv/Scripts/python.exe scripts/eval_people_live.py --limit 100 --json

Does NOT loosen code defaults. For threshold experiments use
`make eval-people` + `scripts/eval_entity_resolution.py --sweep`.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sample_mentions(store, limit: int) -> list[dict]:
    rows: list[dict] = []
    try:
        with store._lock:
            cur = store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='person_mentions'")
            if cur.fetchone():
                q = store._conn.execute(
                    "SELECT mention_id, raw_text, normalized_text, "
                    "resolution_status, resolved_person_id, event_id, "
                    "created_at FROM person_mentions "
                    "ORDER BY mention_id DESC LIMIT ?", (int(limit),))
                for r in q.fetchall():
                    d = dict(r)
                    d["display"] = (d.get("raw_text")
                                    or d.get("normalized_text") or "").strip()
                    d["person_id"] = d.get("resolved_person_id")
                    d["decision"] = d.get("resolution_status")
                    rows.append(d)
    except Exception:
        rows = []
    return rows


def _decide_live(store, display: str, event_id, now: float) -> dict:
    from app.services import people_pipeline as pp
    # Pure scoring against the current roster — no DB writes / no minting.
    people = store.all_people() or []
    scored = pp.score_person_candidates(display, people)
    decision, person, conf = pp.decide_from_scores(
        scored, relationship_boost=0.6, create_person_candidates=True)
    return {
        "display": display,
        "decision": decision,
        "person_id": (int(person["id"]) if person and person.get("id") is not None
                      else None),
        "confidence": conf,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="People v2 live smoke harness")
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="score only; wrap store writes if needed")
    args = ap.parse_args()

    from app.services.memory import memory
    from app.services import people_pipeline as pp
    import time

    if not pp.enabled():
        print("[eval-people-live] QUILL_PEOPLE_V2 is off — nothing to smoke.")
        return 1

    store = memory._ensure_store()
    now = time.time()
    mentions = _sample_mentions(store, args.limit)
    results = []
    source = "person_mentions"

    if mentions:
        for m in mentions:
            display = (m.get("display") or "").strip()
            if not display:
                continue
            # Prefer recorded decision when present; re-run for rate report.
            try:
                results.append(_decide_live(
                    store, display, m.get("event_id"), now))
            except Exception as exc:
                results.append({"display": display, "decision": "error",
                                "error": str(exc)})
    else:
        source = "people_roster_sample"
        # Fallback: re-resolve existing people names (smoke shape only).
        for p in (store.all_people() or [])[: args.limit]:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            try:
                results.append(_decide_live(store, name, None, now))
            except Exception as exc:
                results.append({"display": name, "decision": "error",
                                "error": str(exc)})

    counts = Counter(r.get("decision") or "?" for r in results)
    n = len(results) or 1
    report = {
        "source": source,
        "sampled": len(results),
        "counts": dict(counts),
        "rates": {k: round(v / n, 4) for k, v in counts.items()},
        "note": ("Offline gate remains `make eval-people`. "
                 "Do not loosen code defaults from this smoke alone."),
    }
    print(
        f"[eval-people-live] source={source} n={len(results)} "
        f"counts={dict(counts)} rates={report['rates']}"
    )
    if args.json:
        print(json.dumps(report, indent=2))
    # Always exit 0 for smoke (informational); goldens own the gate.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
