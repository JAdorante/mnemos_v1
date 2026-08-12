"""Noise eval (People v3 WS-G) — junk-mint, wrong-owner, mention share.

Replays the noisy-by-construction corpus
(`tests/fixtures/goldens/people_noise.jsonl`) through the LIVE resolution
pipeline (`people_pipeline.resolve_person_mention` + source policies) against
a throwaway store, then scores the three People-v3 noise metrics
(app/services/people_noise_metrics.py — shared with the WS-B shadow report).

Gates (spec v3 §7):
  * junk-mint      <= 0.5 / audio-hour · 0 from document surfaces
  * wrong-owner    <= 2% (misses don't count — wrong person does)
  * mention share  <= 30% for every top-10 person_score

P0 is measurement only: the default run REPORTS the numbers and exits 0, so
`make eval` stays green while the baseline is still noisy. `--gate` enforces
the thresholds (exit 1) — flip it into CI when the P3/P4 flags turn on.

    python scripts/eval_people_noise.py
    python scripts/eval_people_noise.py --gate
    python scripts/eval_people_noise.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GOLDEN = (Path(__file__).resolve().parent.parent
          / "tests" / "fixtures" / "goldens" / "people_noise.jsonl")


def load_golden() -> list[dict]:
    if not GOLDEN.exists():
        print(f"missing golden: {GOLDEN}", file=sys.stderr)
        sys.exit(1)
    rows = []
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def run(rows: list[dict]) -> dict:
    from app.services import people_pipeline as pp
    from app.services import people_noise_metrics as nm
    from app.storage import Store

    now = time.time()
    meta = next((r for r in rows if r.get("type") == "meta"), {})
    audio_hours = float(meta.get("audio_hours") or 1.0)

    tmp = Path(tempfile.mkdtemp(prefix="quill_noise_"))
    store = Store(db_path=tmp / "noise.db", audio_dir=tmp / "audio")
    try:
        # Seed the known roster.
        name_to_id: dict[str, int] = {}
        for r in rows:
            if r.get("type") != "person":
                continue
            pid = store.insert_person(r["name"], ts=now)
            name_to_id[r["name"]] = pid
            for alias in r.get("aliases") or []:
                store.touch_person(pid, now, alias=alias)
        id_to_name = {v: k for k, v in name_to_id.items()}

        def _resolve(r: dict):
            return pp.resolve_person_mention(
                r["name"], store=store,
                event_source=r.get("event_source") or "audio.whisper",
                window=r.get("window") or "", text=r.get("text") or "",
                now=now, relationship_boost=float(r.get("boost") or 0.6))

        # --- junk-mint: replay ambient mentions --------------------------
        junk_minted = 0
        doc_mints = 0
        mint_log = []
        for r in rows:
            if r.get("type") != "mention":
                continue
            res = _resolve(r)
            minted = res.decision == "create_new"
            g = r.get("golden") or {}
            if minted and g.get("junk"):
                junk_minted += 1
            if minted and g.get("surface") == "document":
                doc_mints += 1
            mint_log.append({"case": r["case"], "decision": res.decision,
                             "minted": minted})

        # --- wrong-owner: replay owner mentions vs golden labels ---------
        assignments = []
        owner_log = []
        for r in rows:
            if r.get("type") != "owner":
                continue
            res = _resolve(r)
            resolved = id_to_name.get(res.person_id) if res.person_id else None
            assignments.append((r["golden_person"], resolved))
            owner_log.append({"case": r["case"], "golden": r["golden_person"],
                              "resolved": resolved,
                              "decision": res.decision})
    finally:
        # Windows: the SQLite handle must close before the dir can go.
        try:
            store.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # --- mention share over the constructed graph profiles ---------------
    people = []
    people_v2 = []
    for r in rows:
        if r.get("type") != "score_profile":
            continue
        # WS-B: profiles carry provenance (mention_source) + dialogue turns;
        # v1 scoring ignores both, so the v1 numbers are byte-identical.
        src = r.get("mention_source") or "audio.whisper"
        edges = []
        for i in range(int(r.get("typed") or 0)):
            edges.append({"obj_type": "fact", "obj_id": i, "predicate": "owns",
                          "source": src})
        for i in range(int(r.get("mentions") or 0)):
            edges.append({"obj_type": "fact", "obj_id": 10_000 + i,
                          "predicate": "mentioned_in", "source": src})
        if r.get("cooccur"):
            edges.append({"obj_type": "person", "obj_id": 1,
                          "predicate": "co_occurs",
                          "weight": float(r["cooccur"])})
        for i in range(int(r.get("asserted") or 0)):
            edges.append({"obj_type": "entity", "obj_id": 20_000 + i,
                          "origin": "asserted"})
        last_seen = now - float(r.get("last_seen_days") or 0) * 86400.0
        people.append((r["name"], edges, last_seen))
        people_v2.append((r["name"], edges, last_seen,
                          float(r.get("dialogue_turns") or 0)))
    shares = nm.top10_mention_shares(people, now)

    # v2 shares (WS-B, report-only — never part of the gate verdict).
    shares_v2 = None
    v2_error = None
    try:
        shares_v2 = nm.topn_mention_shares_v2(people_v2, now)
    except Exception as exc:
        v2_error = str(exc)

    junk_rate = nm.junk_mint_rate(junk_minted, audio_hours)
    wrong_rate = nm.wrong_owner_rate(assignments)
    gates = nm.gate_report(junk_rate=junk_rate, doc_mints=doc_mints,
                           wrong_rate=wrong_rate, shares=shares)
    return {
        "audio_hours": audio_hours,
        "junk_minted": junk_minted,
        "junk_mint_per_audio_hour": round(junk_rate, 3),
        "doc_mints": doc_mints,
        "wrong_owner_rate": round(wrong_rate, 4),
        "owner_assignments": owner_log,
        "mint_decisions": mint_log,
        "top10_mention_share": [
            {"name": n, "score": round(s, 2), "mention_share": round(m, 3)}
            for n, s, m in shares],
        "top10_mention_share_v2": (None if shares_v2 is None else [
            {"name": n, "score": round(s, 2), "mention_share": round(m, 3)}
            for n, s, m in shares_v2]),
        "v2_error": v2_error,
        "gates": gates,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="machine output")
    ap.add_argument("--gate", action="store_true",
                    help="enforce thresholds (exit 1 on failure)")
    args = ap.parse_args()

    report = run(load_golden())
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        g = report["gates"]
        print("People v3 noise eval (WS-G)")
        print(f"  junk-mint    {report['junk_mint_per_audio_hour']:.2f}/audio-h"
              f"  (gate <= 0.5)      {'PASS' if g['junk_mint_ok'] else 'FAIL'}")
        print(f"  doc mints    {report['doc_mints']}"
              f"                 (gate == 0)        "
              f"{'PASS' if g['doc_mint_ok'] else 'FAIL'}")
        print(f"  wrong-owner  {report['wrong_owner_rate']:.1%}"
              f"              (gate <= 2%)       "
              f"{'PASS' if g['wrong_owner_ok'] else 'FAIL'}")
        worst = max((s["mention_share"] for s in
                     report["top10_mention_share"]), default=0.0)
        print(f"  mention shr  {worst:.1%} worst of top-10 (gate <= 30%) "
              f"{'PASS' if g['mention_share_ok'] else 'FAIL'}")
        for s in report["top10_mention_share"]:
            flag = "  <-- over" if s["mention_share"] > 0.30 else ""
            print(f"    {s['name']:<18} score {s['score']:>6}  "
                  f"mention {s['mention_share']:.1%}{flag}")
        v2 = report.get("top10_mention_share_v2")
        if v2 is None:
            print(f"  score v2 (WS-B): NOT READY "
                  f"({report.get('v2_error')})")
        else:
            worst2 = max((s["mention_share"] for s in v2), default=0.0)
            print(f"  score v2 (WS-B, report-only): worst mention share "
                  f"{worst2:.1%} (v1 was {worst:.1%})  "
                  f"{'PASS' if worst2 <= 0.30 else 'FAIL'}")
            for s in v2:
                flag = "  <-- over" if s["mention_share"] > 0.30 else ""
                print(f"    {s['name']:<18} score {s['score']:>6}  "
                      f"mention {s['mention_share']:.1%}{flag}")
        mode = "GATED" if args.gate else "report-only (P0 baseline)"
        print(f"  overall: {'PASS' if g['ok'] else 'FAIL'}  [{mode}]")
    if args.gate and not report["gates"]["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
