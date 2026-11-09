"""Roll one synthetic world through the REAL Sparrow pipeline with a student
model, recording every call the app makes to the local model.

Nothing here rebuilds a prompt by hand. The world's items land through the
connector path (scheduler.land_unseen) and are mined by the real text lane;
notes go through projects.classify; questions go through the chat lane's own
functions (planner.compile -> memory provider -> LLM.direct_answer). A wrapper
on OllamaText.complete records the exact system prompt, messages and schema
each call sent, so a training row built from a recording is byte-for-byte
what the app sends that model in production.

Isolation: one subprocess per world with its own QUILL_DATA_DIR. The child
does not load the repo .env (it would turn on this dev box's flags); it runs
the code defaults plus what the hosted seats set (QUILL_TEXT_LOCAL=1, the
student as QUILL_TEXT_LOCAL_MODEL). The router's paid seam is replaced with
one that raises, so an escalation keeps the student's answer (the router's
own parent_failed path) and nothing in a rollout reaches Claude; the
Anthropic base URL also points at a dead local port as a second guard.

    python scripts/sparrow1/rollout.py --world W.json --out DIR --student TAG
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CHILD_PY = ROOT / ".venv" / "bin" / "python"

# Pins on top of code defaults. Mirrors deploy/hosted/gb10 (local text tier on)
# minus anything that would leave the box or need a device.
SEAT_ENV = {
    "QUILL_TEXT_LOCAL": "1",
    "QUILL_EXEMPLARS": "0",          # train on the clean prompt, no few-shot
    "QUILL_ORG_NETWORK": "0",
    "QUILL_FLEET": "0",
    "QUILL_SHADOW_EVAL": "0",
    "QUILL_IDLE_TRAIN": "0",
    "QUILL_ROUTER": "off",
    "QUILL_ONBOARDING": "1",
    # Seats time a local call out at 45 s; at ~21 tok/s a long extraction
    # misses that, and two worlds sharing the GPU halve the rate. Rollouts
    # need the student's actual output to grade, so they wait longer.
    "QUILL_TEXT_LOCAL_TIMEOUT_S": "120",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "ANTHROPIC_API_KEY": "sparrow1-rollout-no-cloud",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
    "QUILL_CREDENTIALS_FILE": "/nonexistent/sparrow1.credentials.env",
}


def child_env(data_dir: Path, student: str, *,
              ollama_url: str = "http://127.0.0.1:11434") -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("QUILL_", "ANTHROPIC_", "AGENT_"))}
    env.update(SEAT_ENV)
    env["QUILL_DATA_DIR"] = str(Path(data_dir).resolve())
    env["QUILL_TEXT_LOCAL_MODEL"] = student
    env["QUILL_OLLAMA_URL"] = ollama_url
    env["PYTHONPATH"] = str(ROOT)
    return env


def run(world_path: Path, out_dir: Path, student: str, *,
        timeout_s: float = 4 * 3600, log=print,
        chat_from: Path | None = None,
        env_extra: dict[str, str] | None = None) -> dict:
    """Parent side: one world -> out_dir/{calls,scenarios}.jsonl + summary.

    `chat_from` is an earlier run dir of the same world: its ingested store
    is copied in and only the questions are asked (no re-ingest), with the
    clock frozen at the moment that ingest ended, so two chat-only runs
    differ only by `student` and `env_extra`."""
    out_dir = Path(out_dir)
    data_dir = out_dir / "data"
    cmd = [str(CHILD_PY if CHILD_PY.exists() else sys.executable),
           str(HERE / "rollout.py"), "--world", str(world_path),
           "--out", str(out_dir), "--student", student, "--child"]
    env = child_env(data_dir, student)
    if chat_from is not None:
        import shutil
        if data_dir.exists():
            shutil.rmtree(data_dir)
        shutil.copytree(Path(chat_from) / "data", data_dir)
        calls = [json.loads(x) for x in (Path(chat_from) / "calls.jsonl")
                 .read_text().splitlines() if x]
        env["SPARROW1_CHAT_ONLY_AT"] = str(max(c["time"] for c in calls) + 60.0)
    data_dir.mkdir(parents=True, exist_ok=True)
    env.update(env_extra or {})
    t0 = time.time()
    with (out_dir / "rollout.log").open("w", encoding="utf-8") as lf:
        proc = subprocess.run(cmd, cwd=str(data_dir), env=env,
                              stdout=lf, stderr=subprocess.STDOUT,
                              timeout=timeout_s)
    summ = out_dir / "summary.json"
    if proc.returncode != 0 or not summ.is_file():
        tail = (out_dir / "rollout.log").read_text(encoding="utf-8")[-1500:]
        raise RuntimeError(f"rollout failed for {world_path.name} "
                           f"(exit {proc.returncode}):\n{tail}")
    s = json.loads(summ.read_text())
    log(f"[rollout] {Path(world_path).stem}: {s['calls']} calls, "
        f"{s['scenarios']} questions in {time.time() - t0:.0f}s")
    return s


# ======================================================== child process =====
class Recorder:
    """Wraps OllamaText.complete. `context` says which world element the
    driver is processing, so a call can be graded against its own key."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.context: dict = {"phase": "setup", "ref": ""}

    def install(self) -> None:
        from app.services import ollama_text as ot
        orig = self._orig = ot.OllamaText.complete
        rec = self

        def complete(self_, task, *, system, messages, max_tokens=1024,
                     schema=None, exemplars=""):
            injected = False
            if schema is not None:
                _, injected = ot.with_confidence(schema)
            row = {
                "id": f"c{len(rec.calls):05d}", "task": task,
                **rec.context, "model": self_.model,
                "system": ot._compose_system(system, "", schema=schema,
                                             injected=injected),
                "messages": [{"role": m.get("role", "user"),
                              "content": ot._flatten(m.get("content"))}
                             for m in messages],
                "schema": schema, "injected": injected,
                "max_tokens": max_tokens, "had_exemplars": bool(exemplars),
                "time": time.time(),
            }
            t0 = time.time()
            try:
                out = orig(self_, task, system=system, messages=messages,
                           max_tokens=max_tokens, schema=schema,
                           exemplars=exemplars)
            except Exception as exc:
                row.update(error=f"{type(exc).__name__}: {exc}",
                           secs=round(time.time() - t0, 2))
                rec.calls.append(row)
                raise
            row.update(output={k: out.get(k) for k in
                               ("text", "json", "confidence", "parse_ok",
                                "truncated")},
                       secs=round(time.time() - t0, 2))
            rec.calls.append(row)
            return out

        ot.OllamaText.complete = complete

    def uninstall(self) -> None:
        from app.services import ollama_text as ot
        if getattr(self, "_orig", None) is not None:
            ot.OllamaText.complete = self._orig
            self._orig = None


def _block_parent() -> None:
    from app.services import model_router as mr

    def _no_parent(self, task, **_kw):
        raise RuntimeError("sparrow1 rollout: parent model disabled")
    mr.ModelRouter._call_parent = _no_parent


def connectors_connected() -> None:
    """World items land as google/slack/notion connector items, but no
    connector is signed in inside a world's data dir, and the chat lane drops
    memories from connectors that are not active (agent_bridge memory
    provider -> connectors.session.blocked_source_prefixes). A real user
    whose Sparrow captured their mail has that mail connected, so the harness
    runs as if every connector is connected and on (tool access "auto")."""
    from app.services.connectors import session
    session.blocked_source_prefixes = lambda: ()


def _drain_text_lane(max_rounds: int = 25) -> int:
    from app.services.extractor import extractor
    facts = 0
    for _ in range(max_rounds):
        r = extractor.run_text_once()
        facts += int(r.get("facts") or 0)
        if not r.get("remaining"):
            break
    return facts


def _ask(question: str) -> dict:
    """The chat lane for a memory question, in the order agent_bridge and the
    orchestrator run it (planner -> grounding -> direct answer)."""
    from app.services import agent_bridge
    from browser_agent.llm import LLM
    goal, surface = question, None
    try:
        from app.services.agent_planner import planner
        steps = planner.compile(question).steps
        if steps:
            goal, surface = steps[0].to_goal_text(), steps[0].surface
    except Exception as exc:
        print(f"[rollout] planner skipped ({exc})")
    sink: dict = {}
    provider = agent_bridge._make_memory_provider(sink=sink)
    mem = provider(goal) if provider else ""
    ctx = (mem.rstrip() + "\n\n") if mem else ""
    answer = LLM().direct_answer(goal, ctx)
    return {"goal": goal, "surface": surface, "answer": answer,
            "block": sink.get("block", ""), "sources": sink.get("sources", [])}


def _child_main(world_path: Path, out_dir: Path) -> None:
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False   # never the repo .env
    sys.path.insert(0, str(HERE))
    import world as W

    wd = W.load(world_path)
    rec = Recorder()
    t0 = time.time()

    from types import SimpleNamespace
    from app.services.memory import memory
    from app.storage import get_store
    rec.install()
    _block_parent()
    connectors_connected()
    store = get_store()
    memory._ensure_store()
    memory._ensure_vectors()

    from datetime import datetime
    chat_only_at = os.environ.get("SPARROW1_CHAT_ONLY_AT")
    if chat_only_at:
        _chat_only(wd, rec, float(chat_only_at), out_dir, t0)
        return

    from app.services import onboarding
    rec.context = {"phase": "onboarding", "ref": ""}
    onboarding.ingest(W.onboarding_profile(wd), store=store)

    # Live capture mines an item minutes after it lands, so the extractor's
    # RIGHT NOW clock is the item's own time; "by Friday" in a week-old email
    # must resolve against that week. Pin clock.now_local per item.
    from app.services import clock
    from app.services.connectors import scheduler
    real_now_local = clock.now_local
    today = datetime.now()
    n_facts = 0
    for key, cid, item in W.connector_items(wd, today=today):
        rec.context = {"phase": "item", "ref": key}
        item["ts"] = min(float(item["ts"]), time.time() - 120)
        at = datetime.fromtimestamp(item["ts"] + 90)
        clock.now_local = lambda at=at: at
        try:
            scheduler.land_unseen(SimpleNamespace(id=cid), [item], store=store)
            n_facts += _drain_text_lane()
        finally:
            clock.now_local = real_now_local

    rec.context = {"phase": "people", "ref": ""}
    try:
        from app.services import person_adjudicator
        for _ in range(10):
            r = person_adjudicator.run_once(store, limit=20)
            if not r.get("checked"):
                break
    except Exception as exc:
        print(f"[rollout] person adjudication skipped ({exc})")
    from app.services import graph
    graph.rebuild(store, scope="full")

    from app.services import projects
    notes_out = []
    for n in wd["notes"]:
        rec.context = {"phase": "note", "ref": n["key"]}
        routed = projects.classify(n["text"], title=n.get("title", ""),
                                   store=store)
        notes_out.append({"key": n["key"], **routed})

    scen_out = _ask_all(wd, rec)
    _write(out_dir, wd, rec, scen_out, notes_out, n_facts, today, t0)


def _ask_all(wd: dict, rec: Recorder) -> list[dict]:
    scen_out = []
    for s in wd["scenarios"]:
        rec.context = {"phase": "scenario", "ref": s["key"]}
        try:
            r = _ask(s["question"])
        except Exception as exc:
            traceback.print_exc()
            r = {"error": f"{type(exc).__name__}: {exc}"}
        scen_out.append({"key": s["key"], **r})
    return scen_out


def _chat_only(wd: dict, rec: Recorder, at: float, out_dir: Path,
               t0: float) -> None:
    """Ask the questions against an already-ingested store, clock frozen."""
    from datetime import datetime
    from app.services import clock
    time.time = lambda: at
    clock.now_local = lambda: datetime.fromtimestamp(at)
    scen_out = _ask_all(wd, rec)
    _write(out_dir, wd, rec, scen_out, [], 0, datetime.fromtimestamp(at), t0)


def _write(out_dir: Path, wd: dict, rec: Recorder, scen_out: list[dict],
           notes_out: list[dict], n_facts: int, today, t0: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "calls.jsonl").open("w", encoding="utf-8") as f:
        for c in rec.calls:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with (out_dir / "scenarios.jsonl").open("w", encoding="utf-8") as f:
        for s in scen_out:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    (out_dir / "notes.json").write_text(json.dumps(notes_out, indent=1))
    by_task: dict[str, int] = {}
    for c in rec.calls:
        by_task[c["task"]] = by_task.get(c["task"], 0) + 1
    (out_dir / "summary.json").write_text(json.dumps({
        "world": wd["id"], "student": os.environ.get("QUILL_TEXT_LOCAL_MODEL"),
        "today": today.date().isoformat(),
        "calls": len(rec.calls), "by_task": by_task,
        "errors": sum(1 for c in rec.calls if c.get("error")),
        "facts_extracted": n_facts, "scenarios": len(scen_out),
        "secs": round(time.time() - t0, 1)}, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--world", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--student", required=True, help="Ollama tag under test")
    ap.add_argument("--child", action="store_true",
                    help="internal: run in THIS process (env already set)")
    args = ap.parse_args()
    if args.child:
        _child_main(args.world.resolve(), args.out.resolve())
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)                  # skip slow native-thread teardown
    run(args.world.resolve(), args.out.resolve(), args.student)


if __name__ == "__main__":
    main()
