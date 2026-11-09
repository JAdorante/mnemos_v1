"""Sparrow 1.0 — the recursive training loop for the blank-slate base model.

Every user's Sparrow starts from the same base model; this loop is how that
base learns Sparrow's job (filing what lands, finding it again, answering from
it honestly) before it has seen anyone's data. One round:

  1. WORLDS   Claude writes fresh synthetic orgs, weighted toward the skills
              the current model failed last round (world.py)
  2. ROLLOUT  each world runs through the real pipeline with the current model
              as the local model; every local call is recorded (rollout.py)
  3. GRADE    Claude grades each call against the world's answer key and
              writes the correct output for that exact input (grade.py)
  4. ROWS     corrections + on-policy rehearsal rows, in the exact inference
              contract (pairs.py)
then
  5. TRAIN    QLoRA from the base on every round's rows -> sparrow-1.0-rN in
              Ollama (train.py)
  6. EVAL     the candidate and the incumbent on FROZEN holdout worlds that
              never feed training; promote only on a real gain

    python scripts/sparrow1/loop.py holdout --n 3        # once
    python scripts/sparrow1/loop.py eval                 # baseline the incumbent
    python scripts/sparrow1/loop.py round --worlds 6
    python scripts/sparrow1/loop.py train [--evict-ollama]
    python scripts/sparrow1/loop.py eval --model sparrow-1.0-r1
    python scripts/sparrow1/loop.py promote --model sparrow-1.0-r1
    python scripts/sparrow1/loop.py run --rounds 3 --worlds 6   # all of it
    python scripts/sparrow1/loop.py status

State: data/sparrow1/state.json. Claude spend: data/sparrow1/teacher_calls.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import grade as G  # noqa: E402
import pairs as P  # noqa: E402
import rollout as R  # noqa: E402
import world as W  # noqa: E402

DATA = ROOT / "data" / "sparrow1"
STATE = DATA / "state.json"
BASE_TAG = os.environ.get("SPARROW1_BASE_TAG", "qwen2.5:14b-instruct")
TAG_STEM = "sparrow-1.0"


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_key() -> None:
    """The teacher needs ANTHROPIC_API_KEY; take it from the repo .env when
    the shell does not export it. Only that key — the loop never imports the
    app, so the rest of .env stays out of this process."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        from dotenv import dotenv_values
        key = dotenv_values(ROOT / ".env").get("ANTHROPIC_API_KEY")
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
    except Exception:
        pass


def load_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text())
    return {"incumbent": BASE_TAG, "holdout": [], "rounds": [],
            "candidates": [], "evals": {}}


def save_state(st: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE)


# ------------------------------------------------------------- worlds ------
def make_worlds(seeds: list[int], out_dir: Path, *, focus: dict | None = None,
                notes: list[str] | None = None, parallel: int = 3) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    def one(seed: int) -> Path | None:
        spec = W.world_spec(seed, focus=focus)
        for attempt in range(2):
            try:
                w = W.generate(spec, focus_notes=notes)
                p = W.save(w, out_dir / f"{w['id']}.json")
                say(f"world {w['id']}: {len(w['items'])} items, "
                    f"{len(w['scenarios'])} questions, {len(w['notes'])} notes "
                    f"({spec['industry']})")
                return p
            except Exception as exc:
                say(f"world seed {seed} attempt {attempt + 1} failed: {exc}")
        return None

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        return [p for p in ex.map(one, seeds) if p]


def focus_from(graded: list[dict], worlds: dict[str, dict], *,
               max_notes: int = 8) -> tuple[dict, list[str]]:
    """Skill weights + short failure notes for the next round's generator."""
    sc = G.score(graded)["skills"]
    focus = {k: 0.5 + 2.0 * (1.0 - (v["score"] if v["score"] is not None else 1.0))
             for k, v in sc.items() if v["n"]}
    notes: list[str] = []
    fails = [g for g in graded if g.get("verdict") in ("wrong", "partial")
             and g.get("note")]
    fails.sort(key=lambda g: (g["verdict"] != "wrong", g["task"] != "chat"))
    for g in fails:
        if len(notes) >= max_notes:
            break
        w = worlds.get(g["world"]) or {}
        if g["task"] == "chat":
            s = next((x for x in w.get("scenarios", []) if x["key"] == g["ref"]), None)
            if s:
                notes.append(f"[{s['skill']}] asked \"{s['question']}\": {g['note']}")
        elif g["task"] in ("extract", "project_route"):
            notes.append(f"[{g['task']}] {g['note']}")
    return focus, notes


# ------------------------------------------------------------ rollouts -----
def roll_and_grade(world_paths: list[Path], run_root: Path, student: str, *,
                   want_correction: bool, parallel: int = 2) -> list[dict]:
    """Rollout + grade each world. Worlds run concurrently (each is its own
    subprocess; Ollama batches their requests), already-graded ones are
    reused so an interrupted round resumes where it stopped."""

    def one(wp: Path) -> list[dict]:
        w = W.load(wp)
        rd = run_root / w["id"]
        if (rd / "graded.jsonl").is_file():
            say(f"{w['id']}: already graded, reusing")
            return [json.loads(x) for x in
                    (rd / "graded.jsonl").read_text().splitlines() if x]
        try:
            if not (rd / "summary.json").is_file():
                R.run(wp, rd, student, log=say)
            return G.grade_rollout(w, rd, want_correction=want_correction,
                                   log=say)
        except Exception as exc:
            say(f"{w['id']}: skipped ({exc})")
            return []

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        return [g for part in ex.map(one, world_paths) for g in part]


def _overall(sc: dict) -> float | None:
    num = den = 0.0
    for t in sc["tasks"].values():
        if t["score"] is not None:
            num += t["score"] * t["n"]
            den += t["n"]
    return round(num / den, 4) if den else None


# ------------------------------------------------------------ commands -----
def cmd_holdout(args, st):
    if st["holdout"] and not args.force:
        say(f"holdout already has {len(st['holdout'])} worlds (use --force to add)")
        return
    seeds = list(range(args.seed, args.seed + args.n))
    paths = make_worlds(seeds, DATA / "worlds" / "holdout")
    st["holdout"] = sorted(set(st["holdout"]) | {str(p) for p in paths})
    save_state(st)
    say(f"holdout: {len(st['holdout'])} worlds")


def cmd_eval(args, st):
    tag = args.model or st["incumbent"]
    if not st["holdout"]:
        raise SystemExit("no holdout worlds — run `holdout` first")
    graded = roll_and_grade([Path(p) for p in st["holdout"]],
                            DATA / "eval" / tag.replace(":", "_"), tag,
                            want_correction=False)
    sc = G.score(graded)
    sc["overall"] = _overall(sc)
    st["evals"][tag] = {"time": time.time(), **sc}
    save_state(st)
    say(f"eval {tag}: overall {sc['overall']}  "
        + "  ".join(f"{t}={v['score']}(n={v['n']})" for t, v in sc["tasks"].items()))
    return sc


def cmd_round(args, st):
    n = len(st["rounds"]) + 1
    student = args.student or st["incumbent"]
    focus, notes = None, None
    if st["rounds"]:
        prev = st["rounds"][-1]
        focus, notes = prev.get("next_focus"), prev.get("next_notes")
    base_seed = 1000 * n
    t0 = time.time()
    say(f"round {n}: {args.worlds} worlds, student {student}"
        + (f", focus {json.dumps({k: round(v, 2) for k, v in (focus or {}).items()})}"
           if focus else ""))
    rdir = DATA / "rounds" / f"r{n}"
    paths = make_worlds(list(range(base_seed, base_seed + args.worlds)),
                        rdir / "worlds", focus=focus, notes=notes)
    graded = roll_and_grade(paths, rdir / "runs", student, want_correction=True)
    worlds = {W.load(p)["id"]: W.load(p) for p in paths}
    rows: list[dict] = []
    for wid in worlds:
        rd = rdir / "runs" / wid
        if (rd / "graded.jsonl").is_file():
            rows += P.rows_for_run(rd, round_n=n, world_id=wid)
    with (rdir / "rows.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    sc = G.score(graded)
    sc["overall"] = _overall(sc)
    nf, nn = focus_from(graded, worlds)
    import teacher as T
    entry = {"n": n, "student": student, "worlds": [str(p) for p in paths],
             "score": sc, "rows": len(rows),
             "teacher_rows": sum(1 for r in rows if r["source"] == "teacher"),
             "next_focus": nf, "next_notes": nn,
             "usd": T.spend(t0)["usd"], "minutes": round((time.time() - t0) / 60, 1)}
    st["rounds"].append(entry)
    save_state(st)
    say(f"round {n} done: student overall {sc['overall']}, {len(rows)} rows "
        f"({entry['teacher_rows']} corrections), ${entry['usd']}, "
        f"{entry['minutes']} min")


def cmd_train(args, st):
    import train as TR
    if not st["rounds"]:
        raise SystemExit("no rounds yet — run `round` first")
    n = len(st["rounds"])
    rows: list[dict] = []
    for r in st["rounds"]:
        p = DATA / "rounds" / f"r{r['n']}" / "rows.jsonl"
        if p.is_file():
            rows += [json.loads(x) for x in p.read_text().splitlines() if x]
    ds = P.build_dataset(rows, DATA / "train" / f"r{n}.jsonl",
                         student_ratio=args.student_ratio)
    say(f"dataset r{n}: {ds['rows']} rows {json.dumps(ds['per_task'])}")
    if ds["rows"] < 10:
        raise SystemExit("fewer than 10 rows — run more rounds first")
    tag = args.tag or f"{TAG_STEM}-r{n}"
    info = TR.train(Path(ds["path"]), tag, epochs=args.epochs, lr=args.lr,
                    rank=args.rank, max_seq=args.max_seq,
                    evict_ollama=args.evict_ollama, log=say)
    st["candidates"].append({"round": n, **info, "dataset": ds})
    save_state(st)
    return tag


def better(chall: dict, champ: dict, *, margin: float = 0.02,
           max_drop: float = 0.05) -> tuple[bool, list[str]]:
    """Promote only on an overall gain of at least `margin` with no task
    falling more than `max_drop`."""
    why: list[str] = []
    co, ho = chall.get("overall"), champ.get("overall")
    if co is None or ho is None:
        return False, ["missing eval"]
    ok = co >= ho + margin
    why.append(f"overall {ho} -> {co} ({'+' if co >= ho else ''}{co - ho:.3f})")
    for t, v in champ["tasks"].items():
        c = (chall["tasks"].get(t) or {}).get("score")
        if v["score"] is not None and c is not None and c < v["score"] - max_drop:
            ok = False
            why.append(f"{t} dropped {v['score']} -> {c}")
    return ok, why


def cmd_promote(args, st):
    tag = args.model
    champ = st["evals"].get(st["incumbent"])
    chall = st["evals"].get(tag)
    if not (champ and chall):
        raise SystemExit("eval both the incumbent and the candidate first")
    ok, why = better(chall, champ)
    for c in st["candidates"]:
        if c["tag"] == tag:
            c["promoted"] = ok
            c["gate"] = why
    if ok or args.force:
        st["incumbent"] = tag
    save_state(st)
    say(("PROMOTED " if ok or args.force else "KEPT INCUMBENT ") + "; ".join(why))
    return ok


def cmd_run(args, st):
    if not st["holdout"]:
        cmd_holdout(argparse.Namespace(n=args.holdout, seed=9000, force=False), st)
    if st["incumbent"] not in st["evals"]:
        cmd_eval(argparse.Namespace(model=None), st)
    for _ in range(args.rounds):
        cmd_round(argparse.Namespace(worlds=args.worlds, student=None), st)
        tag = cmd_train(argparse.Namespace(
            tag=None, epochs=args.epochs, lr=args.lr, rank=args.rank,
            max_seq=args.max_seq, evict_ollama=args.evict_ollama,
            student_ratio=args.student_ratio), st)
        cmd_eval(argparse.Namespace(model=tag), st)
        cmd_promote(argparse.Namespace(model=tag, force=False), st)


def cmd_status(args, st):
    import teacher as T
    print(json.dumps({
        "incumbent": st["incumbent"], "holdout_worlds": len(st["holdout"]),
        "rounds": [{k: r[k] for k in ("n", "student", "rows", "teacher_rows",
                                      "usd", "minutes")} |
                   {"overall": r["score"].get("overall")} for r in st["rounds"]],
        "candidates": [{k: c.get(k) for k in ("tag", "round", "promoted",
                                              "merged_dir")}
                       for c in st["candidates"]],
        "evals": {t: e.get("overall") for t, e in st["evals"].items()},
        "teacher_spend_total": T.spend(),
    }, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("holdout")
    p.add_argument("--n", type=int, default=3)
    p.add_argument("--seed", type=int, default=9000)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("eval")
    p.add_argument("--model", default=None)

    p = sub.add_parser("round")
    p.add_argument("--worlds", type=int, default=6)
    p.add_argument("--student", default=None)

    def train_flags(p):
        p.add_argument("--epochs", type=float, default=2.0)
        p.add_argument("--lr", type=float, default=1e-4)
        p.add_argument("--rank", type=int, default=32)
        p.add_argument("--max-seq", type=int, default=4096)
        p.add_argument("--student-ratio", type=float, default=1.0)
        p.add_argument("--evict-ollama", action="store_true",
                       help="unload the seats' Ollama models before each stage")

    p = sub.add_parser("train")
    p.add_argument("--tag", default=None)
    train_flags(p)

    p = sub.add_parser("promote")
    p.add_argument("--model", required=True)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("run")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--worlds", type=int, default=6)
    p.add_argument("--holdout", type=int, default=3)
    train_flags(p)

    sub.add_parser("status")

    args = ap.parse_args()
    _load_key()
    st = load_state()
    {"holdout": cmd_holdout, "eval": cmd_eval, "round": cmd_round,
     "train": cmd_train, "promote": cmd_promote, "run": cmd_run,
     "status": cmd_status}[args.cmd](args, st)


if __name__ == "__main__":
    main()
