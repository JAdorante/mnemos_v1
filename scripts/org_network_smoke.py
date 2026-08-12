"""Localhost smoke demo for the Org AI Network thin vertical slice.

Registers IC → manager → exec on a live Org Coordinator (default :8100),
creates a CEO goal, ships a digest with a strategic blocker, cascades
priorities, and prints the deliver_to / escalation targets.

Prereq (live server mode):
    python -m org_coordinator.main   # in another terminal
    set QUILL_ORG_NETWORK=1

Usage:
    python scripts/org_network_smoke.py              # in-process (default)
    python scripts/org_network_smoke.py --live       # hit running coordinator
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Isolate coordinator + node state under a temp dir for the smoke run.
_TMP = tempfile.mkdtemp(prefix="org_smoke_")
os.environ.setdefault("QUILL_ORG_COORD_DATA", str(Path(_TMP) / "coord"))
os.environ.setdefault("QUILL_ORG_STATE", str(Path(_TMP) / "node_state.json"))
os.environ.setdefault("QUILL_ORG_PRIORITIES", str(Path(_TMP) / "priorities.json"))
os.environ.setdefault("QUILL_ORG_ESCALATIONS", str(Path(_TMP) / "escalations.jsonl"))
os.environ.setdefault("QUILL_ORG_NETWORK", "1")
os.environ.setdefault("QUILL_ORG_COORDINATOR_URL",
                      os.environ.get("QUILL_ORG_COORDINATOR_URL",
                                     "http://127.0.0.1:8100"))

USE_INPROCESS = "--live" not in sys.argv


def _inprocess_client():
    from fastapi.testclient import TestClient
    from org_coordinator.main import app
    return TestClient(app)


def main() -> int:
    if USE_INPROCESS:
        client = _inprocess_client()

        def post(path, body=None, token=None):
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            r = client.post(path, json=body or {}, headers=headers)
            return r.status_code, r.json()

        def get(path, token=None):
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            r = client.get(path, headers=headers)
            return r.status_code, r.json()
    else:
        from urllib import request
        base = os.environ["QUILL_ORG_COORDINATOR_URL"].rstrip("/")

        def post(path, body=None, token=None):
            data = json.dumps(body or {}).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = request.Request(base + path, data=data, headers=headers,
                                  method="POST")
            with request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        def get(path, token=None):
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = request.Request(base + path, headers=headers, method="GET")
            with request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

    nodes = {}
    for nid, role, reports_to in (
        ("ceo1", "ceo", ""),
        ("exec1", "exec", "ceo1"),
        ("mgr1", "manager", "exec1"),
        ("ic1", "ic", "mgr1"),
    ):
        code, res = post("/register", {
            "node_id": nid, "display_name": nid, "role": role,
            "reports_to": reports_to,
            "base_url": f"http://127.0.0.1:800{len(nodes)}",
        })
        assert code == 200 and res.get("ok"), res
        nodes[nid] = res["token"]
        print(f"registered {nid} ({role})")

    code, g = post("/goals", {
        "title": "Ship product launch on schedule",
        "detail": "All teams prioritize launch blockers.",
        "horizon": "Q3",
        "priority": 0.95,
        "owner_role": "ceo",
    }, token=nodes["ceo1"])
    assert code == 200 and g.get("ok"), g
    print("goal:", g["goal"]["title"])

    code, d = post("/ingest/digest", {
        "summary": "Manufacturing delay threatens product launch",
        "progress": ["firmware freeze done"],
        "blockers": ["fab line down 3 weeks"],
        "asks": ["need alternate vendor approval"],
        "deps": ["supply chain"],
        "confidence": 0.85,
        "force_strategic": True,
        "period": {"hours": 24},
    }, token=nodes["ic1"])
    assert code == 200 and d.get("ok"), d
    print("digest deliver_to:", d.get("deliver_to"))
    print("escalation target:",
          (d.get("escalation") or {}).get("target", {}).get("node_id"))

    code, p = get("/priorities", token=nodes["ic1"])
    assert code == 200, p
    print("IC priority guidance:",
          (p.get("packet") or {}).get("guidance", "")[:120])

    assert d.get("manager_rollup"), "expected manager_rollup"
    print("manager rollup summary:",
          (d.get("manager_rollup") or {}).get("summary", "")[:160])
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
