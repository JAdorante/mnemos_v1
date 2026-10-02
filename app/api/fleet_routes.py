"""Fleet federation HTTP surface (see services/fleet/ and
docs/fleet-federation.md).

Agent-facing (per-agent Bearer, exempt from the LAN gate because they
authenticate themselves; see api_auth._PREFIX_EXEMPT):
    POST /fleet/publish     publish one signal
    GET  /fleet/stream      Server-Sent Events, filtered by registered topics
    GET  /fleet/signals     catch-up after a reconnect

Owner-facing (LAN gate as usual; never callable with an agent token):
    GET  /fleet/status, /fleet/schema
    GET/POST /fleet/agents, DELETE /fleet/agents/{name}
    GET/PUT  /fleet/routes
    GET  /fleet/offers, POST /fleet/offers/{id}/edit, /fleet/offers/{id}/decide
    POST /fleet/relay/register, /fleet/outbox/drain

Relay-facing: POST /peer/ask with kind="signal" lands in `peer_signal` below
(wired from routes.py so /peer/ask stays one route).
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.services.fleet import envelope as env
from app.services.fleet import feed, inbound, ingress, registry
from app.services.fleet import relay_client, router as fleet_router, state

router = APIRouter()

HEARTBEAT_S = 15.0


def _agent(authorization: str | None) -> dict:
    agent = registry.authenticate(authorization)
    if agent is None:
        raise HTTPException(status_code=401,
                            detail="invalid or missing fleet agent token")
    return agent


def _need_enabled() -> None:
    if not settings.fleet.enabled:
        raise HTTPException(status_code=404, detail="fleet federation is off")


def _owner(request: Request) -> None:
    """Owner-only routes. An agent token is refused outright so a local agent
    cannot rewrite its own routing rules or approve its own offers; with
    QUILL_FLEET_OWNER_AUTH=1 the owner must also present the API token or an
    unlocked session even from loopback."""
    if registry.authenticate(request.headers.get("authorization")) is not None:
        raise HTTPException(status_code=403,
                            detail="agent tokens cannot manage the fleet")
    if os.environ.get("QUILL_FLEET_OWNER_AUTH") in ("1", "true", "True"):
        from app.services.api_auth import request_authorized
        if not request_authorized(request):
            raise HTTPException(status_code=401, detail="owner auth required")


# --- agent-facing ------------------------------------------------------------------
@router.post("/fleet/publish")
def fleet_publish(body: dict, authorization: str | None = Header(None)):
    _need_enabled()
    agent = _agent(authorization)
    try:
        return ingress.publish(agent, body)
    except ingress.PublishError as exc:
        return JSONResponse(exc.as_dict(), status_code=exc.status)


def _topics(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def _reader(authorization: str | None) -> dict:
    _need_enabled()
    agent = _agent(authorization)
    if not registry.can_read(agent):
        raise HTTPException(status_code=403,
                            detail="this agent is not registered to read")
    return agent


@router.get("/fleet/signals")
def fleet_signals(topic: str | None = Query(None),
                  since: int = Query(0, ge=0),
                  limit: int = Query(500, ge=1, le=2000),
                  authorization: str | None = Header(None)) -> dict:
    agent = _reader(authorization)
    items = feed.catch_up(agent, _topics(topic), since, limit)
    return {"ok": True, "signals": items,
            "cursor": items[-1]["seq"] if items else since}


def _sse(item: dict) -> str:
    return (f"id: {item['seq']}\nevent: signal\n"
            f"data: {json.dumps(item, separators=(',', ':'))}\n\n")


@router.get("/fleet/stream")
async def fleet_stream(request: Request,
                       topics: str | None = Query(None),
                       since: int = Query(0, ge=0),
                       max_s: float = Query(0.0, ge=0.0, le=3600.0),
                       authorization: str | None = Header(None),
                       last_event_id: str | None = Header(None)):
    """SSE. Replays anything after `since` (or the Last-Event-ID a browser or
    client sends on reconnect), then streams live. The generator checks for a
    disconnect at least once a second and stops at `max_s` (or an hour), so it
    can never outlive its client the way an unbounded loop does."""
    agent = _reader(authorization)
    wanted = _topics(topics)
    try:
        cursor = max(since, int(last_event_id or 0))
    except ValueError:
        cursor = since
    loop = asyncio.get_running_loop()
    sid, q = feed.subscribe(loop)
    limit_s = max_s or 3600.0

    async def gen():
        last = cursor
        started = time.monotonic()
        beat = started
        try:
            yield ": fleet stream open\n\n"
            for item in feed.catch_up(agent, wanted, last, 2000):
                last = max(last, item["seq"])
                yield _sse(item)
            while True:
                if time.monotonic() - started >= limit_s:
                    break
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if time.monotonic() - beat >= HEARTBEAT_S:
                        beat = time.monotonic()
                        yield ": keepalive\n\n"
                    continue
                if item["seq"] <= last or not feed.deliverable(item, agent,
                                                               wanted):
                    continue
                last = item["seq"]
                yield _sse(item)
        finally:
            feed.unsubscribe(sid)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# --- relay-facing ----------------------------------------------------------------------
def peer_signal(authorization: str | None, body) -> JSONResponse:
    """POST /peer/ask with kind="signal". Authenticated by the relay's inbound
    token, never a peer pairing token, and never answered."""
    status, res = inbound.handle(authorization, body)
    return JSONResponse(res, status_code=status)


# --- owner-facing -------------------------------------------------------------------------
@router.get("/fleet/status")
def fleet_status(request: Request) -> dict:
    _owner(request)
    relay = state.relay()
    return {
        "ok": True, "enabled": settings.fleet.enabled,
        "agents": registry.list_agents(),
        "relay": {"url": state.relay_url(), "node_id": relay.get("node_id"),
                  "registered": bool(relay.get("token"))},
        "rules": fleet_router.load_rules(),
        "pending_offers": len(fleet_router.list_offers("pending")),
        "outbox": len(relay_client.outbox()),
    }


@router.get("/fleet/schema")
def fleet_schema() -> dict:
    return env.SIGNAL_SCHEMA


class AgentIn(BaseModel):
    name: str
    topics: list[str]
    role: str = "both"


@router.get("/fleet/agents")
def fleet_agents(request: Request) -> dict:
    _owner(request)
    return {"ok": True, "agents": registry.list_agents()}


@router.post("/fleet/agents")
def fleet_agent_register(body: AgentIn, request: Request) -> dict:
    _owner(request)
    _need_enabled()
    try:
        rec = registry.register_agent(body.name, body.topics, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True, "agent": rec,
            "note": "Store this token now; it is not shown again."}


@router.delete("/fleet/agents/{name}")
def fleet_agent_revoke(name: str, request: Request) -> dict:
    _owner(request)
    if not registry.revoke_agent(name):
        raise HTTPException(status_code=404, detail="no such agent")
    return {"ok": True}


class RulesIn(BaseModel):
    rules: list[dict]


@router.get("/fleet/routes")
def fleet_routes_get(request: Request) -> dict:
    _owner(request)
    return {"ok": True, "rules": fleet_router.load_rules()}


@router.put("/fleet/routes")
def fleet_routes_put(body: RulesIn, request: Request) -> dict:
    _owner(request)
    try:
        return {"ok": True, "rules": fleet_router.save_rules(body.rules)}
    except fleet_router.RouteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/fleet/offers")
def fleet_offers(request: Request, status: str | None = Query(None)) -> dict:
    _owner(request)
    return {"ok": True, "offers": fleet_router.list_offers(status)}


@router.post("/fleet/offers/{offer_id}/edit")
def fleet_offer_edit(offer_id: str, body: dict, request: Request) -> dict:
    _owner(request)
    try:
        return {"ok": True, "offer": fleet_router.edit_offer(offer_id, body)}
    except (fleet_router.RouteError, env.SignalError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class DecideIn(BaseModel):
    approve: bool
    sha256: str = ""


@router.post("/fleet/offers/{offer_id}/decide")
def fleet_offer_decide(offer_id: str, body: DecideIn,
                       request: Request) -> dict:
    _owner(request)
    try:
        return {"ok": True, "offer": fleet_router.decide_offer(
            offer_id, body.approve, sha256=body.sha256)}
    except fleet_router.RouteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


class RelayIn(BaseModel):
    relay_url: str = ""
    node_id: str
    display_name: str = ""
    fleet_url: str = ""


@router.post("/fleet/relay/register")
def fleet_relay_register(body: RelayIn, request: Request) -> dict:
    _owner(request)
    _need_enabled()
    res = relay_client.register(body.relay_url, body.node_id,
                                display_name=body.display_name,
                                fleet_url=body.fleet_url)
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res)
    return res


@router.post("/fleet/outbox/drain")
def fleet_outbox_drain(request: Request) -> dict:
    _owner(request)
    return {"ok": True, **relay_client.drain_outbox()}
