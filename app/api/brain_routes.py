"""Brains: the 3D memory map's read side and the agent packet.

Every route reads the compiled brain tables (services/brains.py). Only the
packet route crosses the egress gate; it logs one row per request whether
the rung lets the text out or not. Auth is the app's session/token
middleware, the same checks the /graph routes get; the store is this seat's
own, never chosen by a request parameter.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

PACKET_RATE_PER_MIN = 30
_rate_lock = threading.Lock()
_rate: dict[str, deque] = {}


def _store():
    from app.services.memory import memory
    return memory._ensure_store()


def _raise(exc) -> None:
    from app.services import brains
    if isinstance(exc, brains.BrainError):
        raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))
    raise exc


def _rate_key(request: Request) -> str:
    host = request.client.host if request.client else "local"
    sess = request.cookies.get("session") or request.cookies.get("quill_session") or ""
    return f"{host}|{sess[:16]}"


def _rate_limited(request: Request) -> bool:
    now = time.monotonic()
    key = _rate_key(request)
    with _rate_lock:
        q = _rate.setdefault(key, deque())
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= PACKET_RATE_PER_MIN:
            return True
        q.append(now)
    return False


class PacketIn(BaseModel):
    scope: str = "brain"
    section: str | None = None
    include_pending: bool = False
    budget_tokens: int = Field(default=2000, ge=1)
    destination: str = "clipboard"


class GapIn(BaseModel):
    section: str
    question: str


class MoveIn(BaseModel):
    item_type: str
    item_id: str
    section: str


class HideIn(BaseModel):
    item_type: str
    item_id: str


class TypeIn(BaseModel):
    brain_type: str


@router.get("/brains")
def brains_list(type: str | None = None, limit: int = 200,
                since: str | None = None) -> dict[str, Any]:
    from app.services import brains
    if type and type not in brains.TYPES:
        raise HTTPException(status_code=422, detail=f"type must be one of {list(brains.TYPES)}")
    try:
        return brains.list_brains(_store(), btype=type, limit=limit, since=since)
    except Exception as exc:          # noqa: BLE001
        _raise(exc)


@router.post("/brains/rebuild")
def brains_rebuild(scope: str = "full") -> dict[str, Any]:
    """Recompile every brain (or only dirty ones). Same shape as /graph/rebuild."""
    from app.services import brains
    if scope not in ("full", "dirty"):
        raise HTTPException(status_code=422, detail="scope must be full or dirty")
    return {"ok": True, **brains.rebuild(_store(), scope=scope)}


@router.post("/brains/classify")
def brains_classify(limit: int = 200) -> dict[str, Any]:
    """Run the local backlog classifier over facts no rule placed. Local
    model only; reports unavailable (and writes nothing) when it is down."""
    from app.services import brain_classifier
    return brain_classifier.backlog_pass(_store(), limit=max(1, min(int(limit), 2000)))


@router.get("/brains/{brain_id}")
def brain_get(brain_id: str) -> dict[str, Any]:
    from app.services import brains
    try:
        return brains.get_brain(_store(), brain_id)
    except Exception as exc:          # noqa: BLE001
        _raise(exc)


@router.post("/brains/{brain_id}/packet")
def brain_packet(brain_id: str, body: PacketIn, request: Request):
    from fastapi.responses import JSONResponse
    from app.services import brains
    if _rate_limited(request):
        raise HTTPException(status_code=429, detail="30 packets a minute, then wait")
    try:
        res = brains.compile_packet(
            _store(), brain_id, scope=body.scope, section_key=body.section,
            include_pending=bool(body.include_pending),
            budget_tokens=int(body.budget_tokens), destination=body.destination)
    except Exception as exc:          # noqa: BLE001
        _raise(exc)
    if res.get("verdict") == "deny":
        return JSONResponse(status_code=403, content=res)
    return res


@router.post("/brains/{brain_id}/gaps")
def brain_add_gap(brain_id: str, body: GapIn) -> dict[str, Any]:
    from app.services import brains
    try:
        return brains.add_gap(_store(), brain_id, body.section, body.question)
    except Exception as exc:          # noqa: BLE001
        _raise(exc)


@router.post("/brains/{brain_id}/items/move")
def brain_move_item(brain_id: str, body: MoveIn) -> dict[str, Any]:
    from app.services import brains
    try:
        return brains.move_item(_store(), brain_id, body.item_type, body.item_id,
                                body.section)
    except Exception as exc:          # noqa: BLE001
        _raise(exc)


@router.post("/brains/{brain_id}/items/hide")
def brain_hide_item(brain_id: str, body: HideIn) -> dict[str, Any]:
    from app.services import brains
    try:
        return brains.hide_item(_store(), brain_id, body.item_type, body.item_id)
    except Exception as exc:          # noqa: BLE001
        _raise(exc)


@router.post("/brains/{brain_id}/type")
def brain_set_type(brain_id: str, body: TypeIn) -> dict[str, Any]:
    from app.services import brains
    try:
        return brains.set_type(_store(), brain_id, body.brain_type)
    except Exception as exc:          # noqa: BLE001
        _raise(exc)
