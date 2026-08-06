"""KG v2 M3 cutover soak — backfill + N parity runs, print gate status.

Usage:
    py -3.11 scripts/kg_cutover_soak.py              # backfill + 7 parity runs
    py -3.11 scripts/kg_cutover_soak.py --runs 3     # fewer (dev)
    py -3.11 scripts/kg_cutover_soak.py --no-backfill
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="KG v2 affiliation cutover soak")
    ap.add_argument("--runs", type=int, default=7,
                    help="parity reports to accumulate (default 7)")
    ap.add_argument("--no-backfill", action="store_true",
                    help="skip legacy asserted/user → belief backfill")
    ap.add_argument("--json", action="store_true", help="machine-readable out")
    args = ap.parse_args()

    from app.services import kg_backfill, kg_parity
    from app.services.memory import memory

    store = memory._ensure_store()
    out: dict = {"backfill": None, "parity": [], "status": None}

    if not args.no_backfill:
        bf = kg_backfill.run(store)
        out["backfill"] = bf
        print(f"[soak] backfill: {bf}")

    n = max(1, int(args.runs))
    for i in range(n):
        report = kg_parity.run(store)
        crit = int(report.get("critical") or 0)
        out["parity"].append({"i": i + 1, "critical": crit})
        print(f"[soak] parity {i + 1}/{n}: critical={crit}")

    st = kg_parity.status(store)
    out["status"] = st
    gate = st.get("gate") or {}
    print(
        f"[soak] gate.ready={gate.get('ready')} "
        f"reports={gate.get('reports')}/{st.get('reports_needed')} "
        f"critical_in_window={gate.get('critical_in_window')} "
        f"read_v2={st.get('read_v2')} "
        f"env_override={st.get('env_override')!r}"
    )
    if not gate.get("ready"):
        # Document blockers for this install (do not force QUILL_KG_READ_V2=1).
        last = (out.get("parity") or [{}])[-1]
        print(
            "[soak] BLOCKERS: gate not ready. Inspect /kg/parity for critical "
            "deltas (dangling person endpoints, missing/extra dual-write edges). "
            f"Last run critical={last.get('critical')}. "
            "Keep rollback QUILL_KG_READ_V2=0 until clean."
        )
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    return 0 if gate.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
