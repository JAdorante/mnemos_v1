"""Agent-quality check for brain packets (brains spec, step 2).

Take real tasks, run each through the model three ways at the same token
budget, and score the outputs BLIND. This script does the running and the
blinding; a human does the scoring. If the packet does not clearly beat raw
search, fix the section schema before spending time on the 3D view.

    # 1. run: writes <out>/blind.jsonl (shuffled, unlabeled) and <out>/key.json
    python3 scripts/brains_quality.py run tasks.json --out quality/ [--budget 2000]

    # 2. score: fill the "score" field (1-5) in blind.jsonl, then
    python3 scripts/brains_quality.py score quality/

tasks.json: [{"task": "draft the follow-up email to the CFO", "subject": "Project Halcyon"}, ...]
`subject` may be a name or a brain id (entity:412). The run uses THIS
seat's store and model router (so the model tier is whatever the seat has
configured); the packet goes through egress like any copy, so each run
leaves rows in /privacy/egress.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONDITIONS = ("none", "search", "packet")
_SYSTEM = ("You are an analyst's assistant. Do the task using only the context "
           "given. Where the context does not say, write UNKNOWN rather than "
           "guessing. Be concrete and brief.")


def _tokens(text: str) -> int:
    return int(math.ceil(len(text) / 4.0))


def _trim(text: str, budget: int) -> str:
    """Cut to the token budget on a line boundary (same chars/4 rule the
    packet uses, so the three conditions see the same amount)."""
    if _tokens(text) <= budget:
        return text
    out: list[str] = []
    used = 0
    for line in text.splitlines():
        t = _tokens(line + "\n")
        if used + t > budget:
            break
        out.append(line)
        used += t
    return "\n".join(out) + "\n"


def search_context(store, task: str, subject_label: str, budget: int) -> str:
    """Raw memory search results, the way grounding would hand them over:
    one line per hit with its source, no sections, no review state."""
    from app.services.memory import memory
    q = f"{subject_label} {task}".strip()
    hits = memory.search(q, limit=40) or []
    lines = [f"Memory search results for: {q}", ""]
    for h in hits:
        text = (h.get("text") or h.get("raw") or h.get("summary") or "").strip().replace("\n", " ")
        if not text:
            continue
        src = h.get("source") or "memory"
        lines.append(f"- {text} [{src}]")
    return _trim("\n".join(lines) + "\n", budget)


def packet_context(store, brain_id: str, budget: int) -> str:
    from app.services import brains
    pkt = brains.compile_packet(store, brain_id, budget_tokens=max(100, min(8000, budget)),
                                destination="clipboard")
    if pkt.get("verdict") != "allow":
        return f"(packet withheld: {pkt.get('reason')})\n"
    return pkt["text"]


def answer(task: str, context: str) -> str:
    from app.services.model_router import router
    user = f"Task: {task}\n\nContext:\n{context or '(no context)'}"
    return router.complete("brains_quality", system=_SYSTEM,
                           messages=[{"role": "user", "content": user}], max_tokens=700)


def run(tasks_path: Path, out: Path, budget: int, seed: int) -> None:
    from app.services import brains
    from app.services.memory import memory
    store = memory._ensure_store()
    tasks = json.loads(tasks_path.read_text("utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for ti, t in enumerate(tasks):
        task = str(t.get("task") or "").strip()
        subject = str(t.get("subject") or "").strip()
        if not task:
            continue
        brain_id = brains.resolve_subject(store, subject) if subject else None
        label = subject
        if brain_id:
            try:
                label = brains.get_brain(store, brain_id)["label"]
            except Exception:
                pass
        ctx = {
            "none": "",
            "search": search_context(store, task, label, budget),
            "packet": packet_context(store, brain_id, budget) if brain_id else "(no brain for this subject)\n",
        }
        for cond in CONDITIONS:
            t0 = time.time()
            try:
                text = answer(task, ctx[cond])
            except Exception as exc:
                text = f"(model error: {exc})"
            rows.append({"task_index": ti, "task": task, "subject": label,
                         "brain": brain_id, "condition": cond,
                         "context_tokens": _tokens(ctx[cond]), "context": ctx[cond],
                         "answer": text, "latency_s": round(time.time() - t0, 2)})
            print(f"[{ti}] {cond:6s} {_tokens(ctx[cond]):5d} tok  {round(time.time() - t0, 1)}s")
    rng = random.Random(seed)
    order = list(range(len(rows)))
    rng.shuffle(order)
    key = {}
    with (out / "blind.jsonl").open("w", encoding="utf-8") as fh:
        for n, i in enumerate(order):
            r = rows[i]
            bid = f"b{n:03d}"
            key[bid] = {"task_index": r["task_index"], "condition": r["condition"],
                        "context_tokens": r["context_tokens"], "latency_s": r["latency_s"]}
            fh.write(json.dumps({"id": bid, "task": r["task"], "subject": r["subject"],
                                 "answer": r["answer"], "score": None, "notes": ""},
                                ensure_ascii=False) + "\n")
    (out / "key.json").write_text(json.dumps(key, indent=1), "utf-8")
    (out / "contexts.jsonl").write_text(
        "\n".join(json.dumps({k: v for k, v in r.items() if k != "answer"}, ensure_ascii=False)
                  for r in rows) + "\n", "utf-8")
    print(f"\n{len(rows)} answers -> {out / 'blind.jsonl'} (score 1-5 in the 'score' field; "
          f"conditions are in key.json, do not open it until you are done)")


def score(out: Path) -> None:
    key = json.loads((out / "key.json").read_text("utf-8"))
    by: dict[str, list[float]] = {c: [] for c in CONDITIONS}
    per_task: dict[int, dict[str, float]] = {}
    unscored = 0
    for line in (out / "blind.jsonl").read_text("utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        k = key.get(r["id"])
        if not k:
            continue
        if r.get("score") is None:
            unscored += 1
            continue
        by[k["condition"]].append(float(r["score"]))
        per_task.setdefault(k["task_index"], {})[k["condition"]] = float(r["score"])
    print(f"{'condition':8s} {'n':>3s} {'mean':>5s} {'median':>6s}")
    for c in CONDITIONS:
        xs = by[c]
        if xs:
            print(f"{c:8s} {len(xs):3d} {statistics.mean(xs):5.2f} {statistics.median(xs):6.1f}")
        else:
            print(f"{c:8s}   0")
    wins = sum(1 for t in per_task.values() if "packet" in t and "search" in t and t["packet"] > t["search"])
    ties = sum(1 for t in per_task.values() if "packet" in t and "search" in t and t["packet"] == t["search"])
    both = sum(1 for t in per_task.values() if "packet" in t and "search" in t)
    if both:
        print(f"\npacket beats search on {wins}/{both} tasks ({ties} ties)")
        verdict = "clearly better" if wins >= 0.7 * both else "NOT clearly better: fix the section schema first"
        print(f"verdict: {verdict}")
    if unscored:
        print(f"\n{unscored} answers still unscored")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("tasks", type=Path)
    r.add_argument("--out", type=Path, default=Path("quality"))
    r.add_argument("--budget", type=int, default=2000)
    r.add_argument("--seed", type=int, default=7)
    s = sub.add_parser("score")
    s.add_argument("out", type=Path)
    a = ap.parse_args()
    os.environ.setdefault("QUILL_HEADS", "off")
    if a.cmd == "run":
        run(a.tasks, a.out, a.budget, a.seed)
    else:
        score(a.out)


if __name__ == "__main__":
    main()
