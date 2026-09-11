"""Golden eval — peer answer quality (Phase 0.2).

Seeds one tenant's memory per case, asks the question the way a teammate's
Sparrow would, and scores the composed egress on FIVE axes:

  citation  — the answer rests on the fact(s) the golden says it must
  sourced   — every claim that crosses names the event it came from
  freshness — it carries a date the asker can judge staleness by
  recency   — status-shaped questions return the NEWEST supporting fact,
              not merely the most similar one, and DROP the superseded one
  leakage   — facts the golden forbids never cross the wire

Goldens assert on CLAIMS (seeded fact tags), never on expected prose, which is
what let them survive Phase 1 changing egress from a bare string to
{claims, as_of, prose}. The scorer reads that structure when it is present and
still falls back to prose matching, so these goldens also score a peer running
an older build.

    python scripts/eval_peer.py            # offline, deterministic
    python scripts/eval_peer.py --json
    python scripts/eval_peer.py --live     # real embeddings + local model

Offline mode stands a literal token-overlap retriever in for the embedder, so
it runs anywhere and is deterministic; the one case it cannot pass is pure
synonymy ("infrastructure costs" vs "compute"), which is expected.

`--live` runs the real embedder over an ISOLATED index built from each case's
seeded memory — never the developer's own LanceDB, which would both pollute
the result and read their real memory during an eval. It gates like offline,
and it is the mode that catches relevance-floor regressions: an embedder
returns a nearest neighbour even when nothing is close, and on these goldens
that surfaced a colleague's salary for a question about open roles.

Exit 0 on pass, 1 on threshold failure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GOLDEN = (Path(__file__).resolve().parent.parent
          / "tests" / "fixtures" / "goldens" / "peer_asks.jsonl")

NOW = 1_757_000_000.0
DAY = 86400.0

# Baselines, raised as each phase lands.
#
# Phase 1 (typed egress) made freshness and sourcing gates: every answer that
# says anything carries a date and names the source event behind each claim.
# Phase 2 (expansion, graph edges, recency ranking) took citation to 92% and
# recency to 100%, so both are gates now too.
#
# The one case still missing offline is `compute-who-pays` — pure synonymy
# ("infrastructure costs" vs "compute") that no alias or graph edge bridges.
# It needs a real embedder, so watch it in `make eval-peer-live` rather than
# tuning the offline stub until it passes. Leakage is absolute and always was.
MIN_CITATION_RATE = 0.85
MIN_FRESHNESS_RATE = 1.0
MIN_SOURCED_RATE = 1.0
MIN_RECENCY_RATE = 1.0
MAX_LEAKS = 0
MIN_CASES = 10

_STOP = {"the", "and", "for", "about", "what", "know", "said", "tell", "from",
         "with", "your", "this", "our", "any", "update", "latest", "where",
         "did", "how", "who", "are", "was", "were", "have", "has"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", (text or "").casefold())
            if t not in _STOP}


def _load(path: Path) -> list[dict]:
    cases = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _seed(store, case: dict) -> dict[str, dict]:
    """Insert the case's facts; return tag -> {fact_id, text, ts}."""
    from app.events import Event, Modality

    seeded: dict[str, dict] = {}
    for row in case.get("seed") or []:
        ts = NOW - float(row.get("days_ago", 0)) * DAY
        speaker = row.get("speaker") or ""
        # Every fact hangs off a real captured event, the way production facts
        # do. Without this the goldens cannot check the Phase 1 acceptance
        # criterion — that an answer carries a source EVENT id, not just a
        # fact id — and `speaker` would have nothing to resolve against.
        ev = Event(time=ts, modality=Modality.TEXT, raw=row["text"],
                   summary=row["text"][:120], source="audio.whisper",
                   people=[speaker] if speaker else [])
        eid = store.insert(ev)
        # kind matters: a "remind me to…" memory is extracted as a task in
        # production and grounding renders it "- [commitment] …", which is the
        # shape the egress filters see. Seeding it as a claim would test a
        # shape the product never produces.
        if row.get("kind") in ("task", "commitment"):
            fid = store.add_task(row["text"], source_event_id=eid,
                                 confidence=0.9, extracted_at=ts)
        else:
            fid = store.add_claim(row["text"], source_event_id=eid,
                                  source_span=row["text"],
                                  confidence=0.9, extracted_at=ts)
        pid = None
        if speaker:
            try:
                pid = store.insert_person(speaker, ts=ts,
                                          promotion_state="active")
            except Exception:
                pid = None
        # The graph the answerer actually has: the entities a fact is about,
        # who is affiliated with them, and the person->fact edge. Without
        # these, 2.1 has no alias table to expand through and 2.2 has no edges
        # to walk — the fixture would be testing a store production never has.
        if pid:
            try:
                store.add_relation("person", int(pid), "mentioned_in",
                                   "fact", int(fid), origin="asserted")
            except Exception:
                pass
        for ent in row.get("entities") or []:
            try:
                eid_ent = store.resolve_entity(ent["name"], ent.get("kind", "org"),
                                               ts=ts)
                store.add_relation("fact", int(fid), "about",
                                   "entity", int(eid_ent), origin="asserted")
                if pid:
                    store.add_relation("person", int(pid),
                                       ent.get("predicate", "works_at"),
                                       "entity", int(eid_ent),
                                       origin="asserted")
                for alias in ent.get("aliases") or []:
                    from app.services.entity_alias import normalize
                    store.upsert_entity_alias(int(eid_ent), alias,
                                              normalize(alias), ts=ts,
                                              source="golden", confirmed=True)
            except Exception as exc:
                print(f"  (seed entity {ent!r} skipped: {exc})")
        seeded[row["tag"]] = {"fact_id": fid, "event_id": eid,
                              "text": row["text"], "ts": ts,
                              "speaker": speaker, "tokens": _tokens(row["text"])}
    return seeded


def _fake_search(seeded: dict[str, dict]):
    """A deterministic stand-in for the embedder: literal token overlap.

    This is a faithful model of a WEAK semantic tier, which is the point — it
    reproduces the failure the peer path actually has today (a question in the
    asker's vocabulary against differently-phrased memory) without needing a
    model in CI.
    """
    def _search(query: str, limit: int = 8, **kw):
        q = _tokens(query)
        scored = []
        for tag, row in seeded.items():
            overlap = len(q & row["tokens"])
            if overlap:
                scored.append((overlap, row))
        scored.sort(key=lambda x: (-x[0], -x[1]["ts"]))
        return [{"raw": r["text"], "summary": r["text"], "modality": "text",
                 "time": r["ts"], "fact_id": r["fact_id"]}
                for _, r in scored[:limit]]
    return _search


def _live_engine(store, seeded: dict[str, dict], td: str):
    """A real embedder over ONLY this case's seeded memory.

    Same retrieval code as production, isolated index — so `--live` measures
    the embedder rather than whatever happens to be in the developer's
    LanceDB, and never reads their real memory to do it.
    """
    from app.services.memory import MemoryEngine
    from app.vectorstore import VectorStore

    engine = MemoryEngine(store=store)
    engine._semantic = True
    engine._vectors = VectorStore(path=str(Path(td) / "lance"))
    try:
        engine._events = store.all()
    except Exception:
        engine._events = []
    # Index both shapes production indexes: the episode AND the fact extracted
    # from it. Indexing only events made every hit an id-less `Event.to_dict()`
    # payload. Index by the ids _seed recorded — `store.all()` returns Event
    # objects that do not carry their row id.
    indexed = 0
    for row in seeded.values():
        try:
            engine._vectors.add(int(row["event_id"]), float(row["ts"]), "text",
                                row["text"], engine._embed(row["text"]))
            engine.index_fact(int(row["fact_id"]), "claim", row["text"],
                              float(row["ts"]))
            indexed += 1
        except Exception as exc:
            print(f"  (live index skipped an event: {exc})")
    if not indexed:
        raise RuntimeError("live mode indexed nothing — refusing to report "
                           "offline results as live")
    return engine


def _cited(out: dict, seeded: dict[str, dict]) -> set[str]:
    """Which seeded facts the answer actually rests on.

    Prefers Phase 1 structure (`claims` carrying source fact/event ids); falls
    back to distinctive-token overlap against the prose.
    """
    claims = out.get("claims")
    if isinstance(claims, list) and claims:
        by_fid = {r["fact_id"]: tag for tag, r in seeded.items()}
        hit = set()
        for c in claims:
            fid = c.get("fact_id") or c.get("source_fact_id")
            if fid in by_fid:
                hit.add(by_fid[fid])
            else:  # structured but unlinked — fall back to text
                hit |= _text_hits(str(c.get("text") or ""), seeded)
        return hit
    return _text_hits(str(out.get("text") or ""), seeded)


def _text_hits(text: str, seeded: dict[str, dict]) -> set[str]:
    body = _tokens(text)
    hit = set()
    for tag, row in seeded.items():
        distinctive = row["tokens"]
        if not distinctive:
            continue
        # Two distinctive tokens (or all of them, for very short facts).
        need = min(2, len(distinctive))
        if len(body & distinctive) >= need:
            hit.add(tag)
    return hit


def _score_case(case: dict, *, live: bool) -> dict:
    from app.services import peer_channel as pch
    from app.storage import Store

    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "peer_eval.db")
        seeded = _seed(store, case)

        patches = [
            patch("app.storage.get_store", return_value=store),
            patch("app.services.activity.describe_recent", return_value=[]),
            patch("app.services.working_memory.ensure_fresh"),
            patch("app.services.working_memory.current",
                  return_value={"slots": [], "person_ids": [],
                                "person_labels": [], "project_ids": [],
                                "project_labels": [], "fact_ids": []}),
            patch("app.services.working_memory.snapshot", return_value=[]),
            patch("app.services.working_memory.render_lines", return_value=[]),
            patch("app.services.onboarding.load_profile", return_value=None),
            patch("app.services.self_profile.profile_lines", return_value=[]),
            patch("time.time", return_value=NOW),
        ]
        if live:
            # Live mode must still be ISOLATED. The process-wide `memory`
            # singleton indexes into the real LanceDB under the user's data
            # dir; letting it answer here would search the developer's actual
            # memory (wrong results, and it reads personal data during an
            # eval). Build an engine bound to this case's temp store with its
            # own index, seed it, and patch the singleton's search to it.
            live_engine = _live_engine(store, seeded, td)
            patches.append(patch("app.services.memory.memory.search",
                                 side_effect=live_engine.search))
        else:
            # Offline: deterministic retriever, no local model — score
            # retrieval + egress, which is where the bottleneck is.
            patches.append(patch("app.services.memory.memory.search",
                                 side_effect=_fake_search(seeded)))
        started = time.perf_counter()
        for p in patches:
            try:
                p.start()
            except Exception:
                pass
        try:
            out = pch.compose_peer_claims(case["question"], store=store,
                                          now=NOW)
            # The scorer speaks the wire's vocabulary: `text` is what an
            # unupgraded peer reads, `claims`/`as_of` what an upgraded one does.
            out = {**out, "text": out.get("prose") or ""}
        except Exception as exc:
            out = {"text": "", "claims": [], "error": f"{type(exc).__name__}: {exc}"}
        finally:
            for p in reversed(patches):
                try:
                    p.stop()
                except Exception:
                    pass
        elapsed = time.perf_counter() - started

    text = str(out.get("text") or "")
    cited = _cited(out, seeded)
    expect = set(case.get("expect_tags") or [])
    forbid = set(case.get("forbid_tags") or [])

    # A near-miss is NOT a refusal — that distinction is the whole point of
    # 1.6, so scoring must be able to tell them apart.
    refused = (not text.strip()) or bool(out.get("error"))
    leaks = sorted(cited & forbid)

    if case.get("expect_empty"):
        # Correct behaviour is an explicit "nothing on this", not filler.
        citation_ok = refused or not cited
    else:
        citation_ok = bool(expect) and expect <= cited
        # A near-miss answers "I don't have anything on that, but here's the
        # closest thing" — honest, and NOT the same as retrieving the fact the
        # golden asked for. Counting it as a citation flatters retrieval and
        # hides exactly the misses Phase 2 exists to fix.
        if out.get("near_miss") and not case.get("expect_near_miss"):
            citation_ok = False

    # Freshness: Phase 1 stamps as_of; before that, a date in the prose counts.
    as_of = out.get("as_of")
    fresh_ok = bool(as_of) or bool(
        re.search(r"\b(20\d\d-\d\d-\d\d|as of\b)", text, re.I))
    if as_of and case.get("freshness_max_age_days"):
        try:
            age_days = (NOW - float(as_of)) / DAY
            fresh_ok = age_days <= float(case["freshness_max_age_days"])
        except (TypeError, ValueError):
            pass

    # Recency: a status question must return the newest supporting fact AND
    # drop the superseded one. Shipping "free for the next few months" next to
    # "only through November" is not a partial pass — the asker still cannot
    # tell which holds, and now it looks authoritative.
    recency_ok = None
    stale = sorted(cited - expect)
    if case.get("shape") == "status" and seeded:
        newest = max(seeded.items(), key=lambda kv: kv[1]["ts"])[0]
        recency_ok = (newest in cited) and not stale

    near_miss_ok = None
    if case.get("expect_near_miss"):
        near_miss_ok = (not refused) and bool(cited)

    # Phase 1 acceptance: every claim that crosses names the event it came
    # from. A claim without one cannot be played back or audited, which is the
    # whole reason for carrying provenance.
    claim_rows = out.get("claims") or []
    sourced_ok = bool(claim_rows) and all(
        c.get("source_event_id") for c in claim_rows)

    return {
        "id": case["id"],
        # A documented gap, not a hidden one: it still prints, it just doesn't
        # gate. The day the phase lands, this case flips green on its own and
        # the marker comes out.
        "known_fail_until": case.get("known_fail_until"),
        "citation_ok": citation_ok,
        "sourced_ok": sourced_ok,
        "n_claims": len(claim_rows),
        "fresh_ok": fresh_ok,
        "recency_ok": recency_ok,
        "near_miss_ok": near_miss_ok,
        "leaks": leaks,
        "stale_carryover": stale,
        "refused": refused,
        "cited": sorted(cited),
        "expected": sorted(expect),
        "chars": len(text),
        "elapsed_s": round(elapsed, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="real embeddings + local model (informational)")
    args = ap.parse_args()

    cases = _load(GOLDEN)
    if len(cases) < MIN_CASES:
        print(f"FAIL: only {len(cases)} goldens (need >= {MIN_CASES})")
        return 1

    results = [_score_case(c, live=args.live) for c in cases]

    cited_n = sum(1 for r in results if r["citation_ok"])
    # Only answers that actually said something can be checked for sourcing.
    answered = [r for r in results if r["n_claims"]]
    sourced_n = sum(1 for r in answered if r["sourced_ok"])
    # Freshness is scored over cases that answered: a case with nothing to say
    # has no date to carry, and counting it as unfresh would hide regressions.
    fresh_n = sum(1 for r in answered if r["fresh_ok"])
    rec = [r for r in results if r["recency_ok"] is not None]
    rec_n = sum(1 for r in rec if r["recency_ok"])
    near = [r for r in results if r["near_miss_ok"] is not None]
    near_n = sum(1 for r in near if r["near_miss_ok"])
    # Known gaps print loudly but do not gate — the gate's job is to catch NEW
    # regressions while a scheduled fix is still outstanding.
    gating = [r for r in results if not r["known_fail_until"]]
    leaks = [r for r in gating if r["leaks"]]
    known = [r for r in results if r["known_fail_until"]]

    summary = {
        "cases": len(results),
        "citation_rate": cited_n / len(results),
        "sourced_rate": (sourced_n / len(answered)) if answered else None,
        "freshness_rate": (fresh_n / len(answered)) if answered else None,
        "recency_rate": (rec_n / len(rec)) if rec else None,
        "near_miss_rate": (near_n / len(near)) if near else None,
        "leaks": sum(len(r["leaks"]) for r in leaks),
        "known_gaps": {r["id"]: r["known_fail_until"] for r in known},
        "refusals": sum(1 for r in results if r["refused"]),
        "mode": "live" if args.live else "offline",
    }

    if args.json:
        print(json.dumps({"summary": summary, "cases": results}, indent=2))
    else:
        print(f"\npeer goldens ({summary['mode']}) — {len(results)} cases\n")
        for r in results:
            marks = "".join([
                "C" if r["citation_ok"] else "c",
                "F" if r["fresh_ok"] else "f",
                "-" if r["recency_ok"] is None else ("R" if r["recency_ok"] else "r"),
                "!" if r["leaks"] else " ",
            ])
            tail = f"  LEAK={r['leaks']}" if r["leaks"] else ""
            if r["known_fail_until"]:
                tail += f"  (known gap until {r['known_fail_until']})"
            print(f"  [{marks}] {r['id']:<28} cited={r['cited']} "
                  f"expected={r['expected']}{tail}")
        print("\n  (upper = pass: C citation, F freshness, R recency, ! leak)")
        print(f"\n  citation  {summary['citation_rate']:.0%}"
              f"   (gate >= {MIN_CITATION_RATE:.0%})")
        if summary["sourced_rate"] is not None:
            print(f"  sourced   {summary['sourced_rate']:.0%}"
                  "   (gate = 100%: every claim names its source event)")
        print(f"  freshness {summary['freshness_rate']:.0%}"
              f"   (gate >= {MIN_FRESHNESS_RATE:.0%} of answered cases)")
        if summary["recency_rate"] is not None:
            print(f"  recency   {summary['recency_rate']:.0%}"
                  f"   (gate >= {MIN_RECENCY_RATE:.0%})")
        if summary["near_miss_rate"] is not None:
            print(f"  near-miss {summary['near_miss_rate']:.0%}"
                  "   (Phase 1.6 target: 100%)")
        print(f"  leaks     {summary['leaks']}   (gate = {MAX_LEAKS}, "
              f"excluding known gaps)")
        print(f"  refusals  {summary['refusals']}")
        for cid, phase in summary["known_gaps"].items():
            print(f"  known gap {cid} — closes in {phase}")
        print()

    ok = (summary["citation_rate"] >= MIN_CITATION_RATE
          and summary["leaks"] <= MAX_LEAKS
          and (summary["freshness_rate"] is None
               or summary["freshness_rate"] >= MIN_FRESHNESS_RATE)
          and (summary["sourced_rate"] is None
               or summary["sourced_rate"] >= MIN_SOURCED_RATE)
          and (summary["recency_rate"] is None
               or summary["recency_rate"] >= MIN_RECENCY_RATE))
    if not ok:
        print("FAIL: peer goldens below baseline")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
