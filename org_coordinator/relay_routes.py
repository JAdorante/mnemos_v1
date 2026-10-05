"""HTTP surface of the firm relay (fleet federation, Phase 4).

Node (Bearer node token from POST /register):
    POST /relay/enroll            hand the relay this Sparrow's fleet URL and
                                  the inbound token it should forward with
    POST /relay/publish           one signed signal
    GET  /relay/topics            the topics this node may use
Admin (QUILL_RELAY_ADMIN_TOKEN):
    GET/PUT/DELETE /relay/admin/topics[/{name}]
    PUT  /relay/admin/nodes/{node_id}/group
    GET/PUT /relay/admin/blocked     subjects that may never be forwarded
    GET/PUT /relay/admin/kinds       signal kinds the relay will accept
    GET  /relay/admin/queue, POST /relay/admin/queue/drain
Compliance (QUILL_RELAY_COMPLIANCE_TOKEN), read-only:
    GET  /relay/compliance/feed?since=&limit=
    GET  /relay/compliance/verify
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from org_coordinator import relay, relay_log, store, topics
from org_coordinator.auth import require_node

router = APIRouter()


class EnrollIn(BaseModel):
    fleet_url: str
    inbound_token: str


@router.post("/relay/enroll")
def relay_enroll(body: EnrollIn, node: dict = Depends(require_node)) -> dict:
    try:
        row = relay.enroll(node["node_id"], body.fleet_url, body.inbound_token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True, **row, "topics": topics.topics_for(node["node_id"])}


@router.post("/relay/publish")
def relay_publish(body: dict, node: dict = Depends(require_node)):
    status, res = relay.publish(node, body)
    return JSONResponse(res, status_code=status)


@router.get("/relay/topics")
def relay_topics(node: dict = Depends(require_node)) -> dict:
    return {"ok": True, "topics": topics.topics_for(node["node_id"]),
            "group": topics.node_group(node["node_id"])}


# --- admin -------------------------------------------------------------------------
class TopicIn(BaseModel):
    members: list[str] = []
    groups: list[str] = []


class GroupIn(BaseModel):
    group: str | None = None


class BlockedIn(BaseModel):
    subjects: list[str] = []
    patterns: list[str] = []
    updated_by: str = ""


class KindsIn(BaseModel):
    kinds: dict
    updated_by: str = ""


@router.get("/relay/admin/topics", dependencies=[Depends(topics.require_admin)])
def admin_topics() -> dict:
    return {"ok": True, "topics": topics.list_topics()}


@router.put("/relay/admin/topics/{name}",
            dependencies=[Depends(topics.require_admin)])
def admin_topic_put(name: str, body: TopicIn) -> dict:
    unknown = [m for m in body.members if store.get_node(m) is None]
    if unknown:
        raise HTTPException(status_code=422,
                            detail=f"unknown node(s): {', '.join(unknown)}")
    try:
        row = topics.set_topic(name, members=body.members, groups=body.groups)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    relay_log.append("topic_updated", sender="admin", recipients=[],
                     topic=row["name"], members=row["members"],
                     groups=row["groups"])
    return {"ok": True, "topic": row}


@router.delete("/relay/admin/topics/{name}",
               dependencies=[Depends(topics.require_admin)])
def admin_topic_delete(name: str) -> dict:
    if not topics.delete_topic(name):
        raise HTTPException(status_code=404, detail="no such topic")
    relay_log.append("topic_deleted", sender="admin", recipients=[],
                     topic=name)
    return {"ok": True}


@router.put("/relay/admin/nodes/{node_id}/group",
            dependencies=[Depends(topics.require_admin)])
def admin_node_group(node_id: str, body: GroupIn) -> dict:
    try:
        row = topics.set_node_group(node_id, body.group)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such node") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    relay_log.append("group_updated", sender="admin", recipients=[], **row)
    return {"ok": True, **row}


@router.get("/relay/admin/blocked",
            dependencies=[Depends(topics.require_admin)])
def admin_blocked() -> dict:
    try:
        b = relay.load_blocked()
        return {"ok": True, "subjects": sorted(b.subjects),
                "patterns": list(b.patterns)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "subjects": [], "patterns": []}


@router.put("/relay/admin/blocked",
            dependencies=[Depends(topics.require_admin)])
def admin_blocked_put(body: BlockedIn) -> dict:
    try:
        return {"ok": True, **relay.save_blocked(
            body.subjects, body.patterns, updated_by=body.updated_by)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/relay/admin/kinds", dependencies=[Depends(topics.require_admin)])
def admin_kinds() -> dict:
    return {"ok": True, "kinds": relay.load_kinds()}


@router.put("/relay/admin/kinds", dependencies=[Depends(topics.require_admin)])
def admin_kinds_put(body: KindsIn) -> dict:
    try:
        return {"ok": True, "kinds": relay.save_kinds(
            body.kinds, updated_by=body.updated_by)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/relay/admin/queue", dependencies=[Depends(topics.require_admin)])
def admin_queue() -> dict:
    return {"ok": True, "queue": relay.queue()}


@router.post("/relay/admin/queue/drain",
             dependencies=[Depends(topics.require_admin)])
def admin_queue_drain() -> dict:
    return {"ok": True, **relay.drain(force=True)}


# --- compliance ---------------------------------------------------------------------
@router.get("/relay/compliance/feed",
            dependencies=[Depends(topics.require_compliance)])
def compliance_feed(since: int = Query(0, ge=0),
                    limit: int = Query(500, ge=1, le=5000)) -> dict:
    rows = relay_log.read(since, limit)
    return {"ok": True, "rows": rows,
            "cursor": rows[-1]["seq"] if rows else since}


@router.get("/relay/compliance/verify",
            dependencies=[Depends(topics.require_compliance)])
def compliance_verify() -> dict:
    return relay_log.verify_chain()
