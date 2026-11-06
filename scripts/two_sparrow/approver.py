"""Stub human approver + peer-channel flow watcher for the two-Sparrow test.

    python scripts/two_sparrow/approver.py --run run-2026-11-06-a [--seed 7]

Runs until killed. Every few seconds, on both seats:

  * inbound peer asks waiting on "the human"  (GET /peer/asks)
  * the agent's pending approval packet       (GET /approvals/state)
  * proposed records with an open packet      (GET /claims, only when joined)

and applies the scripted decision table — approve 70%, deny 20%, and leave
10% unanswered forever (those test whether the asker nags, re-plans or
stalls). Decisions go through the hash-bound path (packet id + payload_hash);
records decisions use the live-session cookie because bearer callers cannot
approve a record. Every decision and every peer round trip observed
(GET /peer/answers, both directions) is appended to flow.jsonl with run_id.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

APPROVE, DENY, SIT = "approve", "deny", "sit"


def decide(rng: random.Random) -> str:
    x = rng.random()
    return APPROVE if x < 0.70 else DENY if x < 0.90 else SIT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--interval", type=float, default=4.0)
    args = ap.parse_args()
    log = lib.FlowLog(lib.run_dir(args.run) / "flow.jsonl", args.run)
    a, b = lib.seats(log)
    rng = random.Random(args.seed)
    decided: dict[str, str] = {}      # "<seat>:<kind>:<id>" -> decision
    seen_answers: set[str] = set()
    log.write("approver.start", seed=args.seed)
    print(f"approver on {a.name}+{b.name}, run {args.run}", flush=True)
    while True:
        for s in (a, b):
            # 1. inbound peer asks waiting on the human
            try:
                asks = s.get("/peer/asks").get("asks") or []
            except Exception as exc:  # noqa: BLE001
                log.write("approver.error", seat=s.name, where="peer/asks", error=str(exc))
                asks = []
            for ask in asks:
                key = f"{s.name}:peer_ask:{ask.get('id')}"
                if key in decided:
                    continue
                d = decide(rng)
                decided[key] = d
                t0 = time.time()
                resp = None
                try:
                    if d == APPROVE:
                        resp = s.post("/peer/asks/approve", {"id": ask["id"]}, timeout=300)
                    elif d == DENY:
                        resp = s.post("/peer/asks/deny", {"id": ask["id"]})
                except Exception as exc:  # noqa: BLE001
                    resp = {"error": str(exc)}
                log.write("approval", seat=s.name, kind="peer_ask", id=ask.get("id"), decision=d,
                          peer=ask.get("peer_name"), topic=ask.get("topic"), ask_kind=ask.get("kind"),
                          question=ask.get("question"), latency_ms=int((time.time() - t0) * 1000),
                          response=(resp or {}).get("status") or (resp or {}).get("ok"),
                          answer=((resp or {}).get("answer") or "")[:2000])
                print(f"[{s.name}] peer ask {ask.get('id')} ({ask.get('topic')}) -> {d}", flush=True)
            # 2. the agent's pending approval packet
            try:
                st = s.get("/approvals/state")
            except Exception as exc:  # noqa: BLE001
                log.write("approver.error", seat=s.name, where="approvals/state", error=str(exc))
                st = {}
            if st.get("pending"):
                pid = st.get("packet_id")
                key = f"{s.name}:packet:{pid or st.get('sig')}"
                if key not in decided:
                    d = decide(rng)
                    decided[key] = d
                    resp = None
                    try:
                        if d == SIT:
                            pass
                        elif pid and st.get("payload_hash"):
                            resp = s.post(f"/approval/{pid}/decide",
                                          {"payload_hash": st["payload_hash"],
                                           "decision": "approve" if d == APPROVE else "cancel",
                                           "approved_via": "button"})
                        else:
                            resp = s.call("POST", "/approvals/resolve",
                                          form={"accept": "1" if d == APPROVE else "0", "as_json": "1"})
                    except lib.HttpError as exc:
                        resp = {"error": str(exc), "status": exc.status}
                    log.write("approval", seat=s.name, kind="packet", id=pid, decision=d,
                              payload_hash=st.get("payload_hash"), summary=st.get("summary"),
                              intent=st.get("intent"), outbound=st.get("outbound"), steps=st.get("steps"),
                              payload=(st.get("payload") or "")[:2000], response=resp)
                    print(f"[{s.name}] packet {pid} '{st.get('summary')}' outbound={st.get('outbound')} -> {d}", flush=True)
            # 3. proposed records awaiting a human (only when the seat is in an org)
            try:
                cl = s.get("/claims?status=proposed&limit=50")
            except Exception:  # noqa: BLE001
                cl = {}
            if cl.get("joined"):
                for c in cl.get("claims") or []:
                    pk = c.get("open_packet") or {}
                    if not pk.get("id"):
                        continue
                    key = f"{s.name}:record:{pk['id']}"
                    if key in decided:
                        continue
                    d = decide(rng)
                    decided[key] = d
                    resp = None
                    if d != SIT:
                        try:
                            resp = s.post(f"/packets/{pk['id']}/decide",
                                          {"decision": "approve" if d == APPROVE else "reject",
                                           "payload_hash": pk.get("payload_hash"), "approved_via": "button"},
                                          live=True)
                        except lib.HttpError as exc:
                            resp = {"error": str(exc), "status": exc.status}
                    log.write("approval", seat=s.name, kind="record", id=pk["id"], claim_id=c.get("id"),
                              decision=d, payload_hash=pk.get("payload_hash"), text=(c.get("text") or "")[:500],
                              response=resp)
            # 4. flow watcher: every peer round trip this seat has sent
            try:
                answers = s.get("/peer/answers").get("answers") or []
            except Exception:  # noqa: BLE001
                answers = []
            for ans in answers:
                aid = ans.get("ask_id")
                status = ans.get("status")
                key = f"{s.name}:{aid}:{status}"
                if key in seen_answers:
                    continue
                seen_answers.add(key)
                log.write("peer.flow", seat=s.name, direction="outbound", ask_id=aid, status=status,
                          to=ans.get("peer_name") or ans.get("peer_id"), kind=ans.get("kind"),
                          question=ans.get("question"), answer=(ans.get("answer") or "")[:3000],
                          claims=ans.get("claims"), sources=ans.get("sources"), hop=ans.get("hop"),
                          asked_at=ans.get("asked_at") or ans.get("created_at"),
                          answered_at=ans.get("answered_at"), raw_keys=sorted(ans.keys()))
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
