"""Org Coordinator FastAPI app — directory, goals, digest ingest, cascade, escalate.

Run:
    python -m org_coordinator.main
    # or: uvicorn org_coordinator.main:app --host 127.0.0.1 --port 8100
"""
from __future__ import annotations

import os
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from org_coordinator import cascade as cascade_mod
from org_coordinator import escalate as escalate_mod
from org_coordinator import rollup as rollup_mod
from org_coordinator import store
from org_coordinator.auth import hash_token, require_node

app = FastAPI(title="Sparrow Org Coordinator", version="0.1.0")


class RegisterIn(BaseModel):
    node_id: str = Field(..., min_length=2, max_length=64)
    display_name: str = ""
    role: str = "ic"
    reports_to: str = ""
    base_url: str = ""
    peer_id: str = ""


class GoalIn(BaseModel):
    title: str
    detail: str = ""
    horizon: str = ""
    priority: float = 0.8
    owner_role: str = "ceo"


class DigestIn(BaseModel):
    progress: list[str] = []
    blockers: list[str] = []
    asks: list[str] = []
    deps: list[str] = []
    summary: str = ""
    confidence: float = 0.5
    period: dict[str, Any] = {}
    force_strategic: bool = False


class EscalateIn(BaseModel):
    summary: str = ""
    text: str = ""
    blockers: list[str] = []
    force_strategic: bool = False


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    """Browser landing — API lives under /docs; nodes use /health + /register."""
    n = len(store.list_nodes())
    g = len([x for x in store.list_goals() if x.get("active")])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Sparrow Org Coordinator</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1.25rem;color:#1a1a1a}}
h1{{font-weight:600;font-size:1.5rem}} a{{color:#0b3d5c}} code{{background:#f0f0f0;padding:.1em .35em;border-radius:4px}}
.muted{{color:#666}} ul{{padding-left:1.2rem}}
</style></head><body>
<h1>Sparrow Org Coordinator</h1>
<p class="muted">Hybrid org intelligence — roles, goals, digests, escalation.
Raw employee memory stays on each Sparrow node.</p>
<p><b>{n}</b> nodes · <b>{g}</b> active goals</p>
<ul>
  <li><a href="/health">/health</a> — liveness JSON</li>
  <li><a href="/docs">/docs</a> — interactive API</li>
  <li><code>POST /register</code> — enroll a Sparrow node</li>
</ul>
<p class="muted">Register and operate from Sparrow at
<code>http://127.0.0.1:8000/org-network</code> (with
<code>QUILL_ORG_NETWORK=1</code>).</p>
</body></html>"""


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "org_coordinator",
            "nodes": len(store.list_nodes()),
            "goals": len([g for g in store.list_goals() if g.get("active")])}


@app.post("/register")
def register(body: RegisterIn) -> dict:
    role = (body.role or "ic").strip().lower()
    if role not in store.ROLES:
        raise HTTPException(400, f"role must be one of {sorted(store.ROLES)}")
    token = store.mint_token()
    node = store.upsert_node({
        "node_id": body.node_id.strip(),
        "display_name": (body.display_name or body.node_id).strip()[:120],
        "role": role,
        "reports_to": (body.reports_to or "").strip(),
        "base_url": (body.base_url or "").rstrip("/"),
        "peer_id": (body.peer_id or "").strip(),
        "token_sha256": hash_token(token),
    })
    # Return plaintext token once — node must store it.
    return {"ok": True, "node": {k: v for k, v in node.items()
                                 if k != "token_sha256"},
            "token": token}


@app.get("/directory")
def directory(node: dict = Depends(require_node)) -> dict:
    return {"ok": True, "me": {k: v for k, v in node.items()
                               if k != "token_sha256"},
            "nodes": [{k: v for k, v in n.items() if k != "token_sha256"}
                      for n in store.list_nodes().values()]}


@app.get("/goals")
def goals(node: dict = Depends(require_node)) -> dict:
    return {"ok": True, "goals": store.list_goals()}


@app.post("/goals")
def create_goal(body: GoalIn, node: dict = Depends(require_node)) -> dict:
    if node.get("role") not in ("ceo", "exec", "manager"):
        raise HTTPException(403, "only manager+ may create goals")
    g = store.add_goal(body.title, horizon=body.horizon, priority=body.priority,
                       owner_role=body.owner_role, detail=body.detail)
    return {"ok": True, "goal": g}


@app.post("/ingest/digest")
def ingest_digest(body: DigestIn, node: dict = Depends(require_node)) -> dict:
    row = store.append_digest({
        "node_id": node["node_id"],
        "display_name": node.get("display_name"),
        "role": node.get("role"),
        "progress": body.progress[:20],
        "blockers": body.blockers[:20],
        "asks": body.asks[:20],
        "deps": body.deps[:20],
        "summary": (body.summary or "")[:2000],
        "confidence": float(body.confidence),
        "period": body.period or {},
        "force_strategic": bool(body.force_strategic),
    })
    # Auto-escalate when digest looks strategic
    esc = None
    if body.force_strategic or body.blockers:
        esc = escalate_mod.route(node["node_id"], {
            "summary": body.summary,
            "blockers": body.blockers,
            "force_strategic": body.force_strategic,
        })
    # Build rollup for the manager if any
    manager = store.get_node(node.get("reports_to") or "")
    rollup = None
    if manager:
        since = time.time() - 48 * 3600
        team = store.digests_from_reports(manager["node_id"], since=since)
        rollup = rollup_mod.rollup_digests(
            team, team_label=f"reports of {manager.get('display_name')}")
    return {"ok": True, "digest_id": row["id"], "escalation": esc,
            "manager_rollup": rollup,
            "deliver_to": {
                "manager_node_id": (manager or {}).get("node_id"),
                "manager_base_url": (manager or {}).get("base_url"),
                "manager_peer_id": (manager or {}).get("peer_id"),
            }}


@app.get("/digests")
def list_digests(limit: int = 50, node: dict = Depends(require_node)) -> dict:
    # Managers see reports; others see own
    if node.get("role") in ("manager", "exec", "ceo"):
        rows = store.digests_from_reports(node["node_id"], limit=limit)
        if not rows:
            rows = store.digests_for(limit=limit)
    else:
        rows = store.digests_for(node["node_id"], limit=limit)
    return {"ok": True, "digests": rows}


@app.post("/cascade")
def cascade(node: dict = Depends(require_node)) -> dict:
    if node.get("role") not in ("ceo", "exec", "manager"):
        raise HTTPException(403, "only manager+ may cascade")
    return cascade_mod.cascade_down(node["node_id"])


@app.get("/priorities")
def priorities(node: dict = Depends(require_node)) -> dict:
    return cascade_mod.cascade_for_node(node)


@app.post("/escalate")
def escalate(body: EscalateIn, node: dict = Depends(require_node)) -> dict:
    return escalate_mod.route(node["node_id"], body.model_dump())


def main() -> None:
    import uvicorn
    host = os.environ.get("QUILL_ORG_COORD_HOST", "127.0.0.1")
    port = int(os.environ.get("QUILL_ORG_COORD_PORT", "8100"))
    uvicorn.run("org_coordinator.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
