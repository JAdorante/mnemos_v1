"""Org Record Service HTTP API (spec, "API surface" — Phase 1 subset).

Mounted on the coordinator app only when QUILL_ORG_DATABASE_URL is set.
Errors are `{"detail": {"code": ..., "message": ...}}` with a stable code per
refusal, so a node can tell a tampered packet from an expired one from a
missing grant without parsing prose.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from org_coordinator.records import service
from org_coordinator.records.service import Principal, ServiceError

router = APIRouter(tags=["records"])
_db = None


def set_database(db) -> None:
    global _db
    _db = db


def get_db():
    global _db
    if _db is None:
        from org_coordinator.repo import Database
        _db = Database()
    return _db


def error_response(exc: ServiceError) -> JSONResponse:
    return JSONResponse({"detail": {"code": exc.code, "message": exc.message}},
                        status_code=exc.status)


def install_error_handler(app) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(_req: Request, exc: ServiceError):
        return error_response(exc)


def principal(authorization: str | None = Header(None)) -> Principal:
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    return service.authenticate(get_db(), bearer)


class JoinIn(BaseModel):
    invite_code: str = Field(..., min_length=10, max_length=300)
    node_id: str = Field(..., min_length=2, max_length=120)
    label: str = ""


class TokenIn(BaseModel):
    credential: str = Field(..., min_length=10, max_length=300)


class InviteIn(BaseModel):
    email: str
    role: str = "member"
    display_name: str = ""


class ScopeIn(BaseModel):
    kind: str
    name: str
    parent_id: str
    external_ref: str | None = None


class GrantIn(BaseModel):
    member_id: str
    permission: str
    revoke: bool = False


class PacketIn(BaseModel):
    packet_id: str = ""
    payload: dict[str, Any]
    payload_hash: str
    approved_via: str | None = None
    approved_at: float | None = None


class ExpiredIn(BaseModel):
    node_id: str | None = None
    event_refs: list[int] = []
    expired_at: float | None = None


@router.post("/join")
def join(body: JoinIn) -> dict:
    return service.join(get_db(), invite_code=body.invite_code,
                        node_id=body.node_id, label=body.label)


@router.post("/auth/token")
def auth_token(body: TokenIn) -> dict:
    return service.issue_token(get_db(), body.credential)


@router.get("/nodes/heartbeat")
def heartbeat(p: Principal = Depends(principal)) -> dict:
    return service.heartbeat(get_db(), p)


@router.post("/orgs/{org_id}/invites")
def invite(org_id: str, body: InviteIn, p: Principal = Depends(principal)) -> dict:
    return service.create_invite(get_db(), p, org_id, email=body.email,
                                 role=body.role, display_name=body.display_name)


@router.get("/orgs/{org_id}/scopes")
def scopes(org_id: str, p: Principal = Depends(principal)) -> dict:
    return {"scopes": service.list_scopes(get_db(), p, org_id)}


@router.post("/orgs/{org_id}/scopes")
def create_scope(org_id: str, body: ScopeIn,
                 p: Principal = Depends(principal)) -> dict:
    return service.create_scope(get_db(), p, org_id, kind=body.kind,
                                name=body.name, parent_id=body.parent_id,
                                external_ref=body.external_ref)


@router.patch("/orgs/{org_id}/scopes/{scope_id}")
def patch_scope(org_id: str, scope_id: str, body: dict[str, Any],
                p: Principal = Depends(principal)) -> dict:
    return service.update_scope(get_db(), p, org_id, scope_id, body)


@router.get("/scopes/{scope_id}/grants")
def grants(scope_id: str, p: Principal = Depends(principal)) -> dict:
    return {"grants": service.list_grants(get_db(), p, scope_id)}


@router.post("/scopes/{scope_id}/grants")
def set_grant(scope_id: str, body: GrantIn,
              p: Principal = Depends(principal)) -> dict:
    return service.set_grant(get_db(), p, scope_id, member_id=body.member_id,
                             permission=body.permission, revoke=body.revoke)


@router.post("/packets")
def submit(body: PacketIn, p: Principal = Depends(principal)) -> dict:
    return service.submit_packet(get_db(), p, body.model_dump())


@router.post("/packets/forward")
def forward(body: PacketIn, p: Principal = Depends(principal)) -> dict:
    return service.forward_packet(get_db(), p, body.model_dump())


@router.get("/packets/forwarded")
def forwarded(p: Principal = Depends(principal)) -> dict:
    return {"packets": service.forwarded_for(get_db(), p)}


@router.get("/records")
def records(scope: str | None = None, subject: str | None = None,
            predicate: str | None = None, as_of: float | None = None,
            known_at: float | None = None, limit: int = 200,
            p: Principal = Depends(principal)) -> dict:
    return {"records": service.query_records(
        get_db(), p, scope=scope, subject=subject, predicate=predicate,
        as_of=as_of, known_at=known_at, limit=limit)}


@router.get("/records/{record_id}/versions")
def versions(record_id: str, p: Principal = Depends(principal)) -> dict:
    return service.record_versions(get_db(), p, record_id)


@router.get("/records/{record_id}/provenance")
def provenance(record_id: str, version: int | None = None,
               p: Principal = Depends(principal)) -> dict:
    return service.provenance(get_db(), p, record_id, version)


@router.post("/evidence/expired")
def evidence_expired(body: ExpiredIn, p: Principal = Depends(principal)) -> dict:
    return service.evidence_expired(get_db(), p, body.model_dump())


@router.get("/orgs/{org_id}/policy")
def get_policy(org_id: str, p: Principal = Depends(principal)) -> dict:
    return service.get_policy(get_db(), p, org_id)


@router.put("/orgs/{org_id}/policy")
def put_policy(org_id: str, body: dict[str, Any],
               p: Principal = Depends(principal)) -> dict:
    return service.put_policy(get_db(), p, org_id, body)


@router.get("/audit")
def audit_log(from_: float | None = Query(None, alias="from"),
              to: float | None = None,
              action: str | None = None, after_seq: int = 0, limit: int = 500,
              p: Principal = Depends(principal)) -> dict:
    return {"entries": service.read_audit(get_db(), p, t0=from_, t1=to,
                                          action=action, after_seq=after_seq,
                                          limit=limit)}
