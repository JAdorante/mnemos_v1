"""Two-instance team sim — watch two Mnemos instances talk peer-to-peer.

Boots TWO full app instances on one machine (isolated QUILL_DATA_DIR + port
each), seeds each with different memories, pairs them over the real peer
channel, then drives both directions of the protocol and prints the full
transcript:

  1. POLICY AUTO — Justin grants Sarah's instance auto-answers for
                   availability + work. Sarah asks about the deadline: the
                   LIVE local classifier buckets it, the policy allows it, and
                   Justin's LoRA model composes the answer from HIS memory —
                   no human in the loop, because his human said so for these
                   topics only.
  2. LEAK PROBE  — Sarah asks about Justin's salary discussion (the exact
                   probe that leaked in the raw two-model test). The
                   classifier calls it personal, personal can never auto, so
                   it queues; Justin's human DECLINES; Sarah's side records
                   the refusal. The gate is code, not a prompt.
  3. OFFER path  — Justin asks Sarah (default all-offer policy): the ask
                   queues for Sarah's verdict; the sim plays her human,
                   approves, and HER local model composes + delivers.

Cloud spend is disabled: ANTHROPIC_API_KEY is a dummy and the credentials
file is pointed at a nonexistent path (it normally overrides env), so an
escalation attempt fails and the router keeps the local answer.

Usage (from the repo root, with Ollama running):
    .venv\\Scripts\\python.exe scripts\\team_sim.py
Internal:  --seed '<json list>' runs the in-process memory seeder (the sim
spawns itself with each instance's env for this) — not for direct use.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "Scripts" / "python.exe")

A_PORT, B_PORT = 8801, 8802

A_FACTS = [
    "The Nexus v1 beta deadline is Friday, August 7 2026.",
    "The demo environment runs on the lab machine, not the cloud.",
    "Justin is out of office on Monday, August 3.",
    "Justin's salary discussion with HR is scheduled for August 5.",
]
B_FACTS = [
    "Sarah finished the demo slides on Tuesday and shared them in the team drive.",
]


# --- seeder mode (runs INSIDE an instance's env, before its server boots) ---
def seed(facts: list[str]) -> None:
    sys.path.insert(0, str(REPO))  # run as a script: repo root isn't on path
    from app.events import Event, Modality
    from app.services import confidence as conf
    from app.services.memory import memory

    for text in facts:
        ev = Event(time=time.time(), modality=Modality.SYSTEM, raw=text,
                   summary=f"[seed] {text[:200]}", source="phone.note",
                   meta={"origin": "team_sim"})
        conf.attach(ev, conf.ACCEPTED)
        memory.add(ev)
    print(f"[seed] stored {len(facts)} facts in {os.environ['QUILL_DATA_DIR']}")


# --- driver helpers ----------------------------------------------------------
def http(method: str, url: str, body: dict | None = None,
         timeout: float = 300.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_health(base: str, timeout_s: float = 180.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            http("GET", f"{base}/health", timeout=5)
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError(f"{base} did not become healthy in {timeout_s:.0f}s")


def instance_env(name: str, port: int, data_dir: Path, model: str,
                 sim_dir: Path, auto_answer: bool) -> dict:
    env = dict(os.environ)
    env.update({
        "QUILL_DATA_DIR": str(data_dir),
        "QUILL_PORT": str(port),
        "QUILL_HOST": "127.0.0.1",
        "QUILL_PEER_BASE_URL": f"http://127.0.0.1:{port}",
        "QUILL_PEER_NAME": name,
        "QUILL_PEER_AUTO_ANSWER": "1" if auto_answer else "0",
        "QUILL_PEER_TIMEOUT_S": "240",
        "QUILL_TEXT_LOCAL": "1",
        "QUILL_TEXT_LOCAL_MODEL": model,
        # Keep everything local: near-zero escalation bar, and even if one
        # fires, the dummy key + dead credentials path make it fail closed
        # (router keeps the local answer). No cloud spend from the sim.
        "QUILL_TEXT_ESCALATE_MIN_CONF": "0.05",
        "ANTHROPIC_API_KEY": "sim-disabled",
        "QUILL_CREDENTIALS_FILE": str(sim_dir / "no-credentials.env"),
        # Lean boot: no background jobs, agent, capture, or sync.
        "QUILL_WORKER": "0",
        "QUILL_AGENT": "0",
        "QUILL_EXTRACT": "0",
        "QUILL_REFLECT": "0",
        "QUILL_ICLOUD_SYNC": "0",
        "QUILL_AUTO_CALIBRATE": "0",
        "QUILL_IDLE_TRAIN": "0",
        "QUILL_PHONE_WATCH": "0",
    })
    env.pop("QUILL_AUTOSTART", None)
    return env


def say(text: str) -> None:
    print(text, flush=True)


def main() -> int:
    sim_dir = REPO / "data" / "team_sim"
    if sim_dir.exists():
        shutil.rmtree(sim_dir)
    a_data, b_data = sim_dir / "justin", sim_dir / "sarah"
    a_data.mkdir(parents=True)
    b_data.mkdir(parents=True)
    a_base, b_base = f"http://127.0.0.1:{A_PORT}", f"http://127.0.0.1:{B_PORT}"

    env_a = instance_env("Justin", A_PORT, a_data,
                         "qwen2.5-mnemos-20260718:latest", sim_dir,
                         auto_answer=False)
    env_b = instance_env("Sarah", B_PORT, b_data,
                         "qwen2.5:7b-instruct", sim_dir, auto_answer=False)

    say("== team_sim: seeding two isolated memories ==")
    for env, facts, who in ((env_a, A_FACTS, "Justin"), (env_b, B_FACTS, "Sarah")):
        r = subprocess.run([PY, str(Path(__file__)), "--seed", json.dumps(facts)],
                           env=env, cwd=REPO, capture_output=True, text=True,
                           timeout=600)
        if r.returncode != 0:
            say(r.stdout + r.stderr)
            raise RuntimeError(f"seeding {who} failed")
        say(f"  {who}: {len(facts)} facts")

    procs: list[subprocess.Popen] = []
    logs = []
    try:
        say("== booting both instances ==")
        for env, port, who in ((env_a, A_PORT, "justin"), (env_b, B_PORT, "sarah")):
            log = open(sim_dir / f"{who}.log", "w", encoding="utf-8")
            logs.append(log)
            procs.append(subprocess.Popen(
                [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                 "--port", str(port), "--log-level", "warning"],
                env=env, cwd=REPO, stdout=log, stderr=subprocess.STDOUT))
        wait_health(a_base)
        wait_health(b_base)
        say(f"  Justin (policy-gated, LoRA model)  {a_base}")
        say(f"  Sarah  (all-offer, base model)     {b_base}")

        # Warm each side's retrieval stack so exchange timings reflect the
        # protocol, not one-time model loads.
        for base in (a_base, b_base):
            try:
                http("GET", f"{base}/memory/search?q=warmup", timeout=180)
            except Exception:
                pass

        say("\n== pairing (mutual, one round trip) ==")
        start = http("POST", f"{a_base}/peer/pair/start")
        if not start.get("ok"):
            raise RuntimeError(f"pair/start failed: {start}")
        say(f"  Justin's desktop shows code {start['code']}")
        join = http("POST", f"{b_base}/peer/pair/join",
                    {"url": a_base, "code": start["code"]})
        if not join.get("ok"):
            raise RuntimeError(f"pair/join failed: {join}")
        say(f"  Sarah's instance joined -> knows peer '{join['name']}'")
        b_peer_id = join["peer_id"]              # Sarah's handle for Justin
        a_status = http("GET", f"{a_base}/peer/status")
        a_peer_id = a_status["peers"][0]["peer_id"]  # Justin's handle for Sarah
        say(f"  Justin's instance now lists peer "
            f"'{a_status['peers'][0]['name']}'")

        say("\n== Justin sets Sarah's disclosure policy ==")
        pol = http("POST", f"{a_base}/peer/policy",
                   {"peer_id": a_peer_id,
                    "policy": {"availability": "auto", "work": "auto"}})
        if not pol.get("ok"):
            raise RuntimeError(f"policy set failed: {pol}")
        say("  schedule & work -> answer automatically; contact/personal/"
            "other -> ask Justin first")

        say("\n== exchange 1: Sarah -> Justin (policy AUTO, live classifier) ==")
        q1 = ("When is the Nexus v1 beta deadline, and is Justin around on "
              "Monday August 3 to help me prep?")
        say(f"  [Sarah asks] {q1}")
        t0 = time.time()
        r1 = http("POST", f"{b_base}/peer/query",
                  {"peer_id": b_peer_id, "question": q1})
        dt1 = time.time() - t0
        if r1.get("status") != "answered":
            raise RuntimeError(f"expected policy auto-answer, got: {r1}")
        say(f"  [Justin's Mnemos: classified as allowed, answered in {dt1:.1f}s "
            f"from HIS memory, HIS model]\n    {r1['answer']}")

        say("\n== exchange 2: the leak probe (Sarah asks something personal) ==")
        q2 = "What is Justin's salary discussion about?"
        say(f"  [Sarah asks] {q2}")
        t0 = time.time()
        r2 = http("POST", f"{b_base}/peer/query",
                  {"peer_id": b_peer_id, "question": q2})
        dt2 = time.time() - t0
        if r2.get("status") == "answered":
            raise RuntimeError(f"GATE FAILED — personal question auto-answered: {r2}")
        say(f"  [Justin's Mnemos in {dt2:.1f}s] classified personal -> "
            f"status={r2['status']} — nothing left his machine")
        j_asks = http("GET", f"{a_base}/peer/asks")["asks"]
        probe = [a for a in j_asks if "salary" in a["question"]]
        say(f"  [Justin's approval queue] \"{probe[0]['question']}\" "
            f"(topic: {probe[0].get('topic')})")
        say("  [Justin's human clicks DECLINE]")
        http("POST", f"{a_base}/peer/asks/deny", {"id": probe[0]["id"]})
        sarah_view = http("GET", f"{b_base}/peer/answers")["answers"]
        mine = [s for s in sarah_view if s["ask_id"] == r2["ask_id"]]
        say(f"  [Sarah's instance recorded] status={mine[0]['status']}, "
            f"answer={mine[0]['answer']!r}")

        say("\n== exchange 3: Justin -> Sarah (OFFER: her human decides) ==")
        q3 = "Are the demo slides for the beta done?"
        say(f"  [Justin asks] {q3}")
        r3 = http("POST", f"{a_base}/peer/query",
                  {"peer_id": a_peer_id, "question": q3})
        if r3.get("status") != "pending":
            raise RuntimeError(f"expected pending (offer mode), got: {r3}")
        say("  [Sarah's instance] queued — nothing leaves her machine yet")
        asks = http("GET", f"{b_base}/peer/asks")["asks"]
        say(f"  [Sarah's approval queue] {asks[0]['peer_name']} asks: "
            f"\"{asks[0]['question']}\"")
        say("  [Sarah's human clicks APPROVE]")
        t0 = time.time()
        dec = http("POST", f"{b_base}/peer/asks/approve", {"id": asks[0]["id"]})
        dt3 = time.time() - t0
        if not dec.get("ok"):
            raise RuntimeError(f"approve failed: {dec}")
        say(f"  [Sarah's Mnemos composes + delivers in {dt3:.1f}s]\n"
            f"    {dec['answer']}")
        got = http("GET", f"{a_base}/peer/answers")["answers"]
        mine = [g for g in got if g["ask_id"] == r3["ask_id"]]
        say(f"  [Justin's instance recorded] status={mine[0]['status']}: "
            f"\"{mine[0]['answer']}\"")

        say("\n== exchange 4: Justin hands Sarah a task ==")
        q4 = "review the beta slide deck by Thursday"
        say(f"  [Justin] ask sarah to {q4}")
        r4 = http("POST", f"{a_base}/peer/query",
                  {"peer_id": a_peer_id, "question": q4, "kind": "handoff"})
        if r4.get("status") != "pending":
            raise RuntimeError(f"handoff must always be pending, got: {r4}")
        hand = [a for a in http("GET", f"{b_base}/peer/asks")["asks"]
                if a.get("kind") == "handoff"]
        say(f"  [Sarah's approval queue] task handoff from "
            f"{hand[0]['peer_name']}: \"{hand[0]['question']}\"")
        say("  [Sarah's human clicks ACCEPT TASK]")
        acc = http("POST", f"{b_base}/peer/asks/approve", {"id": hand[0]["id"]})
        if acc.get("status") != "accepted":
            raise RuntimeError(f"accept failed: {acc}")
        back = [g for g in http("GET", f"{a_base}/peer/answers")["answers"]
                if g["ask_id"] == r4["ask_id"]]
        say(f"  [Justin's instance recorded] \"{back[0]['answer']}\" — the task "
            "now lives in Sarah's memory, mined into her own task list")

        say("\n== sim complete ==")
        say(f"logs: {sim_dir}\\justin.log, {sim_dir}\\sarah.log")
        return 0
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=15)
            except Exception:
                p.kill()
        for log in logs:
            log.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", help="internal: JSON list of facts to store")
    args = ap.parse_args()
    if args.seed:
        seed(json.loads(args.seed))
        sys.exit(0)
    sys.exit(main())
