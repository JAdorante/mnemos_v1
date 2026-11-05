#!/usr/bin/env python3
"""Restore gb10 seats whose org membership file was lost, e.g. zeroed by a crash.

For each seat: confirm its membership is really unreadable, have the org service
revoke that member's old credentials and mint a one-time invite, and redeem it
inside the seat. The member keeps their id, role and grants, and the node keeps
its node id, so existing records and evidence stay attributed to them.

    scripts/restore_org_membership.py --org ORG --seat user1=a@b.co --seat ...
    scripts/restore_org_membership.py --org ORG --seat user1=a@b.co --apply

Without --apply it only prints the plan. A healthy seat is always refused. No
credential or invite is printed, and the invite reaches the seat over stdin, so
it never shows up in a process list.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

ORG_CONTAINER = "sparrow-org-org-1"
SERVICE_URL = "http://sparrow-org:8100"

SEAT_STATE = r"""
import json
from app.services.records import org_client
p = org_client._path()
raw = p.read_bytes() if p.exists() else b""
print(json.dumps({"size": len(raw), "zeroed": bool(raw) and set(raw) == {0},
                  "joined": org_client.joined()}))
"""

SEAT_JOIN = r"""
import json
from app.services.records import org_client
if org_client.joined():
    raise SystemExit("refusing: this seat is already joined")
out = org_client.join({url!r}, {code!r}, {node!r})
print(json.dumps({{"member_id": out["member_id"], "role": out["role"],
                  "joined": org_client.joined(),
                  "scopes": len(org_client.cached_scopes())}}))
"""


def _run(args: list[str], stdin: str | None = None) -> str:
    done = subprocess.run(args, input=stdin, capture_output=True, text=True)
    if done.returncode:
        # Never echo stdin: it may hold the invite.
        raise SystemExit(f"{args[0]} {args[1]} {args[2]} failed: "
                         f"{done.stderr.strip()[-400:]}")
    return done.stdout


def _last_json(text: str) -> dict:
    """Seat code may log before its one line of output."""
    return json.loads(text.strip().splitlines()[-1])


def _seat(name: str, script: str) -> dict:
    return _last_json(_run(["docker", "exec", "-i", f"gb10-sparrow-{name}-1",
                            "python", "-"], stdin=script))


def _org(*args: str) -> str:
    return _run(["docker", "exec", ORG_CONTAINER, "python", "-m",
                 "org_coordinator.records", *args])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--org", required=True)
    ap.add_argument("--seat", action="append", required=True,
                    metavar="userN=EMAIL", help="seat and its member's email")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    members = {m["email"].lower(): m
               for m in json.loads(_org("members", "--org", args.org))}
    plan = []
    for pair in args.seat:
        name, _, email = pair.partition("=")
        member = members.get(email.strip().lower())
        state = _seat(name, SEAT_STATE)
        problem = None
        if member is None:
            problem = "no member with that email"
        elif member["status"] != "active":
            problem = f"member is {member['status']}"
        elif state["joined"]:
            problem = "seat is healthy, refusing to touch it"
        elif not state["zeroed"]:
            problem = "membership is not zeroed, look at it by hand"
        print(json.dumps({"seat": name, "email": email,
                          "member": (member or {}).get("id"),
                          "role": (member or {}).get("role"),
                          "node_id": (member or {}).get("node_id"),
                          "peer_url": (member or {}).get("peer_url"),
                          "file_bytes": state["size"],
                          "problem": problem}))
        plan.append((name, member, problem))
    if any(p for _, _, p in plan):
        print("Nothing changed: fix the problems above.", file=sys.stderr)
        return 1
    if not args.apply:
        print("Dry run: nothing changed. Add --apply to restore these seats.")
        return 0
    for name, member, _ in plan:
        issued = json.loads(_org("reissue", "--org", args.org,
                                 "--member", member["id"]))
        out = _seat(name, SEAT_JOIN.format(url=SERVICE_URL,
                                           code=issued["invite_code"],
                                           node=member["node_id"]))
        print(json.dumps({"seat": name, "revoked_credentials":
                          issued["revoked_credentials"], **out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
