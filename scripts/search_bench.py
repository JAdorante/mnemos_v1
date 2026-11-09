"""Retrieval benchmark: does the evidence for a question reach the local model?

No model calls, no Claude, no GPU. It replays questions against Sparrow
stores that were already built by real ingestion (the Sparrow 1.0 worlds in
data/sparrow1/: synthetic orgs with answer keys, ingested through the real
pipeline by scripts/sparrow1/rollout.py), and checks whether each answerable
question's evidence phrases appear in what the chat lane would hand the
model: the planner's goal text plus the memory block from
agent_bridge._make_memory_provider (grounding.compose).

Each world runs in its own subprocess on a fresh COPY of its store, with the
clock frozen at the moment ingestion finished, so a run is repeatable and
two runs differ only by the code (or the flags in --env) under test.

Pass 1 measures the production path, question by question, in order. Pass 2
diagnoses every question without touching pass 1's numbers:
  in_goal       the planner's own context already carried the evidence
  in_uncapped   compose found it but the 2600-char block cap cut it
  rank_goal     rank of the first evidence-bearing hit in memory.search(goal)
  rank_question the same for the bare question (goal text drift shows here)

    python scripts/search_bench.py                         # holdout worlds
    python scripts/search_bench.py --set dev               # tuning worlds
    python scripts/search_bench.py --env QUILL_X=1 --label try-x
    python scripts/search_bench.py --show-misses

Results: data/search_bench/<label>-<time>.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
S1 = ROOT / "scripts" / "sparrow1"
sys.path.insert(0, str(S1))

import grade as G  # noqa: E402
import rollout as R  # noqa: E402
import world as W  # noqa: E402

DATA = ROOT / "data" / "sparrow1"
OUT = ROOT / "data" / "search_bench"
SEM_LIMIT = 5          # what _make_memory_provider asks compose for

# Model-free replay. The one departure from a seat: compose's optional LLM
# query router (speaker-beliefs / what-changed routes) runs regex-only.
NO_MODEL_ENV = {"QUILL_TEXT_LOCAL": "0", "QUILL_QUERY_ROUTE_LLM": "0"}


# ------------------------------------------------------------- scoring -----
def classify(row: dict) -> str:
    """Why a question's evidence did or did not reach the model."""
    if row["hit"]:
        return "hit"
    if row.get("in_uncapped"):
        return "cut_by_block_cap"
    used = row.get("rank_used", row.get("rank_goal"))
    rg, rq = row.get("rank_goal"), row.get("rank_question")
    if used is not None and used < SEM_LIMIT:
        return "found_but_dropped"        # min_score / dedup / section rules
    if rq is not None and rq < SEM_LIMIT and used != rq:
        return "query_drift"              # the bare question finds it
    if any(x is not None for x in (used, rg, rq)):
        return "ranked_below_limit"
    return "not_in_top50"


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    by_skill: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by_skill[r["skill"]].append(int(r["hit"]))
    return {
        "questions": n, "hits": hits,
        "recall": round(hits / n, 4) if n else None,
        "skills": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_skill.items())},
        "reasons": dict(Counter(classify(r) for r in rows).most_common()),
        "mean_block_chars": round(sum(r["block_chars"] for r in rows) / n) if n else 0,
    }


# -------------------------------------------------------------- parent -----
def world_sets() -> dict[str, list[tuple[Path, Path]]]:
    """name -> [(world.json, run dir with an ingested store)]."""
    st = json.loads((DATA / "state.json").read_text()) if (DATA / "state.json").is_file() else {}
    sets: dict[str, list[tuple[Path, Path]]] = {"holdout": [], "dev": []}
    base_eval = DATA / "eval" / str(st.get("incumbent", "qwen2.5:14b-instruct")).replace(":", "_")
    for wp in st.get("holdout", []):
        w = W.load(Path(wp))
        rd = base_eval / w["id"]
        if (rd / "summary.json").is_file():
            sets["holdout"].append((Path(wp), rd))
    for wp in sorted((DATA / "worlds" / "dev").glob("*.json")):
        rd = DATA / "search_dev" / wp.stem
        if (rd / "summary.json").is_file():
            sets["dev"].append((wp, rd))
    return sets


def bench_world(world_path: Path, run_dir: Path, *, env_extra: dict[str, str]
                ) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="search-bench-") as tmp:
        data = Path(tmp) / "data"
        shutil.copytree(run_dir / "data", data)
        env = R.child_env(data, "none")
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            env.pop(k, None)            # keyless: the planner's heuristic path,
        env.update(NO_MODEL_ENV)        # as on every seat without a key
        env.update(env_extra)
        out = Path(tmp) / "rows.jsonl"
        proc = subprocess.run(
            [str(R.CHILD_PY if R.CHILD_PY.exists() else sys.executable),
             str(Path(__file__).resolve()), "--child", "--world", str(world_path),
             "--run", str(run_dir), "--out", str(out)],
            cwd=tmp, env=env, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(f"bench failed for {world_path.name}:\n"
                               + (proc.stdout + proc.stderr)[-2000:])
        return [json.loads(x) for x in out.read_text().splitlines() if x]


# --------------------------------------------------------------- child -----
def _hit_rank(hits: list[dict], phrases: list[str]) -> int | None:
    for i, h in enumerate(hits):
        text = " ".join(str(h.get(k) or "") for k in
                        ("chunk_text", "raw", "text", "summary"))
        if any(G.evidence_seen(p, text) for p in phrases):
            return i
    return None


def _child(world_path: Path, run_dir: Path, out: Path) -> None:
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    from datetime import datetime

    wd = W.load(world_path)
    calls = [json.loads(x) for x in (run_dir / "calls.jsonl").read_text().splitlines() if x]
    frozen = max(c["time"] for c in calls) + 60.0
    time.time = lambda: frozen
    from app.services import clock
    clock.now_local = lambda: datetime.fromtimestamp(frozen)

    from app.services import agent_bridge, grounding, search_rank
    from app.services.memory import memory
    from app.storage import get_store
    R.connectors_connected()
    store = get_store()
    memory._ensure_store()
    memory._ensure_vectors()
    try:
        from app.services.agent_planner import planner
    except Exception:
        planner = None

    qs = [s for s in wd["scenarios"] if not s["not_in_memory"]]
    rows = []
    for s in qs:                                   # pass 1: production path
        goal = s["question"]
        if planner is not None:
            try:
                steps = planner.compile(s["question"]).steps
                if steps:
                    goal = steps[0].to_goal_text()
            except Exception:
                pass
        sink: dict = {}
        provider = agent_bridge._make_memory_provider(sink=sink)
        mem = provider(goal) if provider else ""
        seen = goal + "\n" + mem
        rows.append({
            "world": wd["id"], "key": s["key"], "skill": s["skill"],
            "question": s["question"], "phrases": s["evidence_phrases"],
            "goal": goal, "block_chars": len(sink.get("block") or ""),
            "sections": [x.get("label") for x in sink.get("sources") or []],
            "hit": any(G.evidence_seen(p, seen) for p in s["evidence_phrases"]),
            "in_goal": any(G.evidence_seen(p, goal) for p in s["evidence_phrases"]),
        })
    cap = grounding._MAX_BLOCK_CHARS
    for r in rows:                                 # pass 2: diagnosis
        grounding._MAX_BLOCK_CHARS = 10 ** 7
        try:
            g = grounding.compose(r["goal"], semantic_limit=SEM_LIMIT,
                                  min_score=0.15, record_attention=False)
        finally:
            grounding._MAX_BLOCK_CHARS = cap
        r["in_uncapped"] = any(G.evidence_seen(p, g["block"]) for p in r["phrases"])
        r["uncapped_chars"] = len(g["block"])
        r["rank_goal"] = _hit_rank(memory.search(r["goal"], limit=50), r["phrases"])
        r["rank_question"] = _hit_rank(memory.search(r["question"], limit=50),
                                       r["phrases"])
        # The rank in the list the search section actually takes its top
        # SEM_LIMIT from: the query compose uses, reranked when that is on.
        q = grounding.semantic_query(r["goal"])
        if search_rank.enabled():
            pool = search_rank.rerank(memory.search(q, limit=max(SEM_LIMIT * 4, 20)),
                                      q, store=store)
        else:
            pool = memory.search(q, limit=50)
        r["rank_used"] = _hit_rank(pool, r["phrases"])
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def answers(pairs: list[tuple[Path, Path]], *, env_extra: dict[str, str],
            label: str, student: str = "qwen2.5:14b-instruct") -> dict:
    """End to end: ask each world's questions through the real chat lane and
    local model on a copy of its ingested store (rollout chat-only mode),
    then have Claude grade the answers against the world's key. This is
    the only mode here that spends: ~16 judge calls per world."""
    import loop as L
    L._load_key()
    graded: list[dict] = []
    for wp, rd in pairs:
        w = W.load(wp)
        out = OUT / "answers" / label / w["id"]
        R.run(wp, out, student, chat_from=rd, env_extra=env_extra)
        graded += G.grade_rollout(w, out, want_correction=False, log=lambda *_: None)
    sc = G.score([g for g in graded if g["task"] == "chat"])
    return {"chat": sc["tasks"].get("chat"), "skills": sc["skills"],
            "with_evidence": sc["chat_with_evidence"],
            "retrieval_missed": sc["chat_retrieval_missed"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--set", default="holdout", choices=["holdout", "dev", "all"])
    ap.add_argument("--env", action="append", default=[],
                    help="KEY=VALUE for the replay (flags under test)")
    ap.add_argument("--label", default="run")
    ap.add_argument("--show-misses", action="store_true")
    ap.add_argument("--answers", action="store_true",
                    help="also grade the local model's answers end to end "
                         "(Claude judge, ~$0.6 per world)")
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--world", type=Path)
    ap.add_argument("--run", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    if args.child:
        _child(args.world, args.run, args.out)
        sys.stdout.flush()
        os._exit(0)

    sets = world_sets()
    pairs = sets["holdout"] + sets["dev"] if args.set == "all" else sets[args.set]
    if not pairs:
        raise SystemExit(f"no ingested worlds in set '{args.set}'")
    env_extra = dict(kv.split("=", 1) for kv in args.env)
    t0 = time.time()
    rows: list[dict] = []
    for wp, rd in pairs:
        part = bench_world(wp, rd, env_extra=env_extra)
        s = summarise(part)
        print(f"{wp.stem}: {s['hits']}/{s['questions']}  {s['reasons']}")
        rows += part
    for r in rows:
        r["why"] = classify(r)
    summ = summarise(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{args.label}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"label": args.label, "set": args.set,
                                "env": env_extra, "summary": summ,
                                "rows": rows}, indent=1, ensure_ascii=False))
    print(f"\nRECALL {summ['hits']}/{summ['questions']} = {summ['recall']}   "
          f"({args.set}, {args.label}, {time.time() - t0:.0f}s)")
    print("by skill: " + "  ".join(f"{k} {v}" for k, v in summ["skills"].items()))
    print("misses:   " + "  ".join(f"{k} {v}" for k, v in summ["reasons"].items()
                                   if k != "hit"))
    print(f"mean block {summ['mean_block_chars']} chars -> {path.relative_to(ROOT)}")
    if args.answers:
        a = answers(pairs, env_extra=env_extra, label=args.label)
        summ["answers"] = a
        path.write_text(json.dumps({"label": args.label, "set": args.set,
                                    "env": env_extra, "summary": summ,
                                    "rows": rows}, indent=1, ensure_ascii=False))
        print(f"ANSWERS chat {a['chat']}  skills "
              + "  ".join(f"{k} {v['score']}" for k, v in a["skills"].items()))
    if args.show_misses:
        for r in rows:
            if not r["hit"]:
                print(f"\n[{r['why']}] {r['world']} {r['key']} ({r['skill']}) "
                      f"rank used={r.get('rank_used')} q={r['rank_question']} "
                      f"goal={r['rank_goal']} "
                      f"uncapped={r['uncapped_chars']}\n  Q: {r['question']}\n"
                      f"  evidence: {r['phrases']}\n  sections: {r['sections']}")


if __name__ == "__main__":
    main()
