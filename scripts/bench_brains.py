"""Brains performance pass: a synthetic store at the spec's scene size, timed
against the budgets in the Nov 10 2026 spec.

    python3 scripts/bench_brains.py [--brains 500] [--items 40]

Builds a temp store with N deal subjects, each carrying ~M facts spread over
the rule-placed kinds (thesis/risk keywords, commitments, tasks, decisions,
people edges), then reports full rebuild, incremental recompile of one
brain, GET /brains, GET /brains/{id}, and the packet, each p95 over a few
runs. Budgets: /brains < 150 ms, /brains/{id} < 100 ms, packet < 200 ms,
incremental < 50 ms per dirty brain, full rebuild < 30 s per 100k facts.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))
os.environ.setdefault("QUILL_HEADS", "off")
os.environ.setdefault("QUILL_EGRESS_RUNG", "connected")

import app.config  # noqa: E402
from app.events import Event, Modality  # noqa: E402
from app.services import brains  # noqa: E402
from app.services.records import node_store  # noqa: E402
from app.storage import Store  # noqa: E402

THESIS = ("thesis: margin expansion from pricing power in segment {i}",
          "the opportunity is growth from cross-sell in channel {i}",
          "upside comes from a durable moat around product {i}")
RISK = ("a risk is customer churn in cohort {i}",
        "the lender raised an objection about covenant {i}",
        "a concern: key-man exposure on team {i}")


def seed(store: Store, n_brains: int, n_items: int) -> list[int]:
    now = time.time()
    ev = store.insert(Event(time=now - 3600, modality=Modality.AUDIO, raw="bench",
                            summary="bench", source="meeting.audio"))
    people = [store.resolve_person(f"Person {p}", ts=now) for p in range(60)]
    ids = []
    for b in range(n_brains):
        name = f"Project Bench{b:04d}"
        eid = store.resolve_entity(name, "project", ts=now)
        ids.append(eid)
        per = max(3, n_items)
        for i in range(per):
            kind = i % 6
            t = now - 86400 * (i % 40) - b
            if kind in (0, 1):
                fid = store.add_claim(f"{name} {THESIS[i % 3].format(i=i)}.", source_event_id=ev,
                                      source_span="", confidence=0.8, extracted_at=t)
            elif kind == 2:
                fid = store.add_claim(f"{name} {RISK[i % 3].format(i=i)}.", source_event_id=ev,
                                      source_span="", confidence=0.7, extracted_at=t)
            elif kind == 3:
                fid = store.add_commitment(f"send item {i} for {name}", source_event_id=ev,
                                           from_person_id=people[i % 60], to_person_id=people[(i + 1) % 60],
                                           due="2026-12-01", confidence=0.9, extracted_at=t)
            elif kind == 4:
                fid = store.add_task(f"review item {i} for {name}", source_event_id=ev,
                                     owner_person_id=people[i % 60], confidence=0.7, extracted_at=t)
            else:
                node_store.insert_claim(store, {
                    "kind": "decision", "subject_ref": f"entity:{name.lower()}",
                    "subject_label": name, "predicate": "decided",
                    "value": {"text": f"Decided point {i} on {name}."},
                    "schema_version": "decision-v1", "canonical_hash": f"h{b}-{i}",
                    "confidence": 0.9, "evidence": [{"event_id": ev, "t": t, "source": "meeting"}],
                    "privacy_class": "internal", "capture_source": "meeting",
                    "status": "approved" if i % 2 else "proposed"})
                fid = None
            if fid and i % 2 == 0:
                store.review_fact(fid, "approved")
        for p in range(3):
            store.add_relation("person", people[(b + p) % 60], "works_at", "entity", eid,
                               origin="asserted", confidence=0.8, ts=now)
    return ids


def p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def timed(fn, runs=5):
    out = []
    for _ in range(runs):
        t0 = time.perf_counter(); fn(); out.append((time.perf_counter() - t0) * 1000)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brains", type=int, default=500)
    ap.add_argument("--items", type=int, default=40)
    ap.add_argument("--runs", type=int, default=5)
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="bench-brains-"))
    store = Store(db_path=tmp / "quill.db", audio_dir=tmp / "audio")
    t0 = time.perf_counter()
    ids = seed(store, a.brains, a.items)
    print(f"seeded {a.brains} subjects x {a.items} items in {time.perf_counter() - t0:.1f}s "
          f"({len(store.list_facts(limit=10**7))} facts)")

    t0 = time.perf_counter(); res = brains.rebuild(store); full_s = time.perf_counter() - t0
    n_facts = len(store.list_facts(limit=10**7))
    print(f"full rebuild: {full_s:.2f}s for {res['brains']} brains / {res['items']} items "
          f"({n_facts} facts -> {full_s / max(1, n_facts) * 100_000:.1f}s per 100k facts)")

    lst = brains.list_brains(store, limit=500)
    body = json.dumps(lst).encode()
    print(f"GET /brains payload: {len(body) / 1024:.0f} KB raw, {len(gzip.compress(body)) / 1024:.0f} KB gzipped, "
          f"{lst['total']} brains")
    bid = lst["brains"][0]["id"]

    # cold list (cache cleared) vs warm
    def cold_list():
        brains._list_cache.pop(id(store), None); brains.list_brains(store, limit=500)
    print(f"GET /brains cold p95: {p95(timed(cold_list, a.runs)):.0f} ms   "
          f"warm p95: {p95(timed(lambda: brains.list_brains(store, limit=500), a.runs)):.0f} ms")
    print(f"GET /brains/{{id}} p95: {p95(timed(lambda: brains.get_brain(store, bid), a.runs)):.0f} ms "
          f"({sum(len(s['items']) for s in brains.get_brain(store, bid)['sections'])} items)")
    print(f"POST packet p95: {p95(timed(lambda: brains.compile_packet(store, bid, include_pending=True), a.runs)):.0f} ms")

    # incremental: add one fact to one brain, recompile only it
    def incr():
        store.add_claim(f"Project Bench0001 thesis: a fresh upside {time.time()}.",
                        source_event_id=1, source_span="", confidence=0.8,
                        extracted_at=time.time())
        brains.rebuild(store, scope="dirty")
    print(f"incremental (one new fact, dirty pass) p95: {p95(timed(incr, a.runs)):.0f} ms")
    t0 = time.perf_counter(); brains.rebuild(store, scope="dirty", subjects_only=[("entity", ids[0])])
    print(f"recompile one brain (index load + compile): {(time.perf_counter() - t0) * 1000:.0f} ms")
    t0 = time.perf_counter(); idx = brains._Index(store, time.time()); load_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter(); brains.compile_subject(store, idx, "entity", ids[1]); comp_ms = (time.perf_counter() - t0) * 1000
    print(f"  of which index load {load_ms:.0f} ms, compile one subject {comp_ms:.0f} ms")
    store.close()


if __name__ == "__main__":
    main()
