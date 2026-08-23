#!/usr/bin/env python
"""Latency benchmark — the acceptance gate for every phase of the program.

Runs a fixed workload against the local stack and prints the target table from
the program brief, so "did that help?" is answered by the same numbers every
time rather than by feel.

    python scripts/bench_latency.py                  # text path, warm
    python scripts/bench_latency.py --cold           # unload first: cold-start tax
    python scripts/bench_latency.py --json out.json  # machine-readable
    python scripts/bench_latency.py --baseline b.json  # compare against a run

What it measures, and what it deliberately does not:

* **TTFT / TTCA** on the local text path, via the router's real seam, with the
  stage breakdown Ollama reports for free (load / prefill / generation).
* **Retrieval** latency through the real `MemoryEngine.search`.
* **Cold-start tax** — with `--cold`, unload the model between calls and
  report the difference against the warm run. This is the number Phase 1.1's
  `keep_alive` has to remove.
* **Prefix-cache behaviour** — repeat the same task with different trailing
  content; a working static prefix shows a falling prefill on repeats.

It never calls a paid model. `--allow-cloud` is deliberately absent: a
benchmark that can spend money will eventually be run in a loop.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Representative of what the local tier is actually asked to do. Short, mixed
# intent, no personal content — this file is committed.
QUERIES = [
    "what did I commit to this week",
    "summarize my last meeting",
    "who is Dana",
    "what is capital-connect",
    "did I say I would send the renewal deck",
    "what happened yesterday afternoon",
    "list my open tasks",
    "who did I talk to about pricing",
    "when is my next meeting",
    "what did we decide about the migration",
]

EXTRACT_INPUTS = [
    "I'll send Dana the renewal deck by Thursday.",
    "We agreed to push the migration to Q3.",
    "Can you remind me to call the vendor tomorrow?",
    "Marcus said the pricing page is wrong.",
    "Nothing much happened today.",
]


def _pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100.0 * len(xs) + 0.5)) - 1))
    return xs[k]


def _summary(name, samples):
    return {
        "name": name, "n": len(samples),
        "p50": round(_pct(samples, 50), 1),
        "p90": round(_pct(samples, 90), 1),
        "p99": round(_pct(samples, 99), 1),
        "mean": round(statistics.fmean(samples), 1) if samples else 0.0,
        "min": round(min(samples), 1) if samples else 0.0,
    }


def unload_model(model: str, url: str) -> None:
    """Ask Ollama to drop the model (keep_alive=0) so the next call is cold."""
    import urllib.request
    body = json.dumps({"model": model, "keep_alive": 0,
                       "messages": [{"role": "user", "content": "x"}],
                       "options": {"num_predict": 1}}).encode()
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60).read()
        time.sleep(1.0)
    except Exception as exc:
        print(f"  ! could not unload {model}: {exc}")


def bench_text(*, rounds: int, cold: bool) -> dict:
    from app.config import settings
    from app.services import latency
    from app.services.model_router import router
    from app.services.ollama_text import OllamaText

    cfg = settings.text_local
    client = OllamaText()
    if not client.available():
        return {"skipped": f"Ollama model {client.model!r} unavailable"}

    out: dict = {"model": client.model, "keep_alive": cfg.keep_alive}
    ttca, prefill, generation, load = [], [], [], []

    for i in range(rounds):
        q = QUERIES[i % len(QUERIES)]
        if cold:
            unload_model(client.model, cfg.ollama_url)
        with latency.trace("bench_chat", task="chat") as tr:
            t0 = time.perf_counter()
            try:
                router.complete("chat", system="Answer briefly.",
                                messages=[{"role": "user", "content": q}],
                                max_tokens=64, speculative=True)
            except Exception as exc:
                print(f"  ! query failed: {exc}")
                continue
            ttca.append((time.perf_counter() - t0) * 1000.0)
            prefill.append(tr.stages.get("prefill", 0.0))
            generation.append(tr.stages.get("generation", 0.0))
            load.append(tr.stages.get("model_load", 0.0))

    out["ttca_ms"] = _summary("ttca", ttca)
    out["prefill_ms"] = _summary("prefill", prefill)
    out["generation_ms"] = _summary("generation", generation)
    out["model_load_ms"] = _summary("model_load", load)
    out["cold_calls"] = sum(1 for v in load if v >= latency.COLD_LOAD_MS)
    return out


def bench_prefix_cache(*, rounds: int) -> dict:
    """Same task, different tails: prefill should fall after the first call
    once the static prefix is stable (Phase 1.2)."""
    from app.services import latency
    from app.services.model_router import router
    from app.services.ollama_text import OllamaText

    if not OllamaText().available():
        return {"skipped": "Ollama unavailable"}
    system = "You are a terse assistant. " + ("Context rules. " * 40)
    prefills = []
    for i in range(rounds):
        with latency.trace("bench_prefix", task="chat") as tr:
            try:
                router.complete("chat", system=system,
                                messages=[{"role": "user",
                                           "content": f"say ok ({i})"}],
                                max_tokens=8, speculative=True)
            except Exception as exc:
                print(f"  ! prefix round failed: {exc}")
                continue
            prefills.append(tr.stages.get("prefill", 0.0))
    if len(prefills) < 2:
        return {"skipped": "not enough samples"}
    return {
        "first_ms": round(prefills[0], 1),
        "rest_p50_ms": round(_pct(prefills[1:], 50), 1),
        "samples": [round(p, 1) for p in prefills],
        # >1 means repeats are cheaper than the first call, i.e. the cache hit.
        "speedup": round(prefills[0] / max(1e-6, _pct(prefills[1:], 50)), 2),
    }


def bench_retrieval(*, rounds: int) -> dict:
    from app.services import latency
    from app.services.memory import memory
    samples = []
    for i in range(rounds):
        with latency.trace("bench_retrieval", task="search") as tr:
            t0 = time.perf_counter()
            try:
                memory.search(QUERIES[i % len(QUERIES)], limit=10)
            except Exception as exc:
                print(f"  ! search failed: {exc}")
                return {"skipped": str(exc)}
            samples.append((time.perf_counter() - t0) * 1000.0)
            tr.add("retrieval", samples[-1])
    return _summary("retrieval", samples)


def bench_embedding(*, rounds: int) -> dict:
    """MiniLM single vs batch — the number Phase 3.1's batching has to beat."""
    from app.services.embeddings import embedder
    try:
        embedder.warmup()
        texts = [q for q in QUERIES]
        t0 = time.perf_counter()
        for t in texts:
            embedder.encode(t)
        one_by_one = (time.perf_counter() - t0) * 1000.0
        t0 = time.perf_counter()
        embedder.encode_many(texts)
        batched = (time.perf_counter() - t0) * 1000.0
    except Exception as exc:
        return {"skipped": str(exc)}
    return {
        "n": len(QUERIES),
        "one_by_one_ms": round(one_by_one, 1),
        "batched_ms": round(batched, 1),
        "speedup": round(one_by_one / max(1e-6, batched), 2),
    }


def print_report(res: dict, baseline: dict | None = None) -> None:
    def delta(path: list[str], value) -> str:
        if not baseline or not isinstance(value, (int, float)):
            return ""
        node = baseline
        for k in path:
            if not isinstance(node, dict) or k not in node:
                return ""
            node = node[k]
        if not isinstance(node, (int, float)) or node == 0:
            return ""
        d = (value - node) / node * 100.0
        arrow = "▼" if d < 0 else "▲"
        return f"  {arrow} {abs(d):5.1f}% vs baseline"

    print("\nMnemos latency benchmark")
    print("=" * 74)
    txt = res.get("text") or {}
    if txt.get("skipped"):
        print(f"  text path: SKIPPED ({txt['skipped']})")
    else:
        print(f"  model {txt.get('model')}  keep_alive={txt.get('keep_alive')}")
        for label, key in (("TTCA (complete answer)", "ttca_ms"),
                           ("  prefill", "prefill_ms"),
                           ("  generation", "generation_ms"),
                           ("  model load", "model_load_ms")):
            s = txt.get(key) or {}
            print(f"  {label:<26} p50 {s.get('p50', 0):>8.1f} ms   "
                  f"p90 {s.get('p90', 0):>8.1f} ms"
                  + delta(["text", key, "p50"], s.get("p50")))
        print(f"  cold-load calls            {txt.get('cold_calls', 0)} / "
              f"{(txt.get('ttca_ms') or {}).get('n', 0)}")

    pc = res.get("prefix_cache") or {}
    if pc.get("skipped"):
        print(f"\n  prefix cache: SKIPPED ({pc['skipped']})")
    else:
        print(f"\n  prefix cache   first {pc.get('first_ms')} ms -> "
              f"repeat p50 {pc.get('rest_p50_ms')} ms  "
              f"({pc.get('speedup')}x)")

    rt = res.get("retrieval") or {}
    if rt.get("skipped"):
        print(f"  retrieval: SKIPPED ({rt['skipped']})")
    else:
        print(f"  retrieval      p50 {rt.get('p50', 0):>8.1f} ms   "
              f"p90 {rt.get('p90', 0):>8.1f} ms"
              + delta(["retrieval", "p50"], rt.get("p50")))

    em = res.get("embedding") or {}
    if not em.get("skipped"):
        print(f"  embedding      {em.get('n')} texts: "
              f"{em.get('one_by_one_ms')} ms one-by-one vs "
              f"{em.get('batched_ms')} ms batched ({em.get('speedup')}x)")

    print("\n  Targets: TTCA p50 <= 3000 ms local.  "
          "Cold-load share of user-facing calls < 1%.")
    print("  No cloud calls are made by this benchmark.\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--cold", action="store_true",
                    help="unload the model before each call (cold-start tax)")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--baseline", type=Path, default=None,
                    help="a previous --json run to compare against")
    ap.add_argument("--skip-text", action="store_true")
    args = ap.parse_args(argv)

    # Spans on for the duration: the benchmark IS the measurement.
    os.environ["QUILL_LATENCY_SPANS"] = "1"

    res: dict = {"at": time.time(), "cold": bool(args.cold),
                 "rounds": args.rounds}
    if not args.skip_text:
        print("running text path ...")
        res["text"] = bench_text(rounds=args.rounds, cold=args.cold)
        print("running prefix-cache probe ...")
        res["prefix_cache"] = bench_prefix_cache(rounds=max(3, args.rounds // 2))
    print("running retrieval ...")
    res["retrieval"] = bench_retrieval(rounds=args.rounds)
    print("running embedding ...")
    res["embedding"] = bench_embedding(rounds=args.rounds)

    baseline = None
    if args.baseline and args.baseline.is_file():
        try:
            baseline = json.loads(args.baseline.read_text())
        except Exception as exc:
            print(f"  ! unreadable baseline: {exc}")
    print_report(res, baseline)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
