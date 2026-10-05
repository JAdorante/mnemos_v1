"""Records layer — the node's review surface (spec, "API surface: Node").

  GET  /records                          review page (HTML)
  GET  /claims?status=&scope=&kind=      review queue, grouped scope -> subject
  GET  /claims/{id}                      claim + evidence + packets + current
                                         org record value (the diff baseline)
  POST /claims/{id}/propose              set scope/target, mint a packet
  POST /claims/{id}/discard              close without proposing
  POST /claims/{id}/retry                failed -> approved, inside packet TTL
  POST /packets/{id}/decide              approve | edit | reject — LIVE SESSION
  POST /packets/{id}/forward             owner holds only propose — LIVE SESSION
  GET  /records/forwarded                packets forwarded to me as approver
  POST /records/forwarded/{id}/approve   approve one of them — LIVE SESSION
  GET  /retention/status                 upcoming expiries, holds, last receipt
  GET  /records/membership, POST /records/join, POST /records/heartbeat
  GET  /records/precision                approval precision per kind

"Live session" (promotion.LIVE_SESSION) is decided HERE, from the request,
never from a body field: no Authorization header (bearer callers are scripts
and agents), the double-submit CSRF header only same-origin page JS can send,
and — whenever an owner account exists, which is every hosted seat — a valid
account session cookie. Anything else reaches promotion.decide with a
non-human source and is refused by trust.source_can_authorize.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.services.records import node_store, org_client, promotion, retention
from app.services.records.promotion import PromotionError

router = APIRouter()


def _store():
    from app.services.memory import memory
    return memory._ensure_store()


def live_session(request: Request) -> tuple[bool, str]:
    from app.services import account, api_auth
    if request.headers.get("authorization"):
        return False, "bearer callers cannot approve"
    if not api_auth.csrf_header_ok(request):
        return False, "missing same-origin CSRF header"
    site = (request.headers.get("sec-fetch-site") or "").lower()
    if site and site not in ("same-origin", "none"):
        return False, f"sec-fetch-site={site}"
    if account.exists():
        if not account.session_valid(request.cookies.get(api_auth.COOKIE_NAME)):
            return False, "no signed-in account session"
    return True, ""


def _source(request: Request) -> tuple[str, dict]:
    ok, why = live_session(request)
    return (promotion.LIVE_SESSION, {}) if ok else (
        "http.unverified", {"never_authorizes": True, "why": why})


def _err(exc: PromotionError, status: int = 409) -> JSONResponse:
    codes = {"not_live_session": 403, "bad_approved_via": 422,
             "no_claim": 404, "no_packet": 404, "invalid_value": 422,
             "approver_lacks_grant": 403, "not_joined": 412,
             "payload_hash_mismatch": 409, "packet_expired": 410}
    return JSONResponse({"ok": False, "code": exc.code,
                         "detail": exc.message},
                        status_code=codes.get(exc.code, status))


def _org_err(exc: Exception) -> JSONResponse:
    if isinstance(exc, org_client.OrgRefused):
        return JSONResponse({"ok": False, "code": exc.code,
                             "detail": exc.detail}, status_code=exc.status)
    return JSONResponse({"ok": False, "code": "org_unavailable",
                         "detail": str(exc)}, status_code=503)


class ProposeBody(BaseModel):
    scope_id: str
    target_ref: str | None = None
    include_quote: bool = False


class DecideBody(BaseModel):
    decision: str
    payload_hash: str
    approved_via: str = "button"
    confirm: str | None = None          # typed: first 8 chars of the hash
    value: dict[str, Any] | None = None
    scope_id: str | None = None
    target_ref: str | None = None
    include_quote: bool | None = None
    reason: str | None = None


class DiscardBody(BaseModel):
    reason: str = ""


class JoinBody(BaseModel):
    service_url: str
    invite_code: str


class ForwardedApproveBody(BaseModel):
    payload_hash: str
    approved_via: str = "button"
    confirm: str | None = None


def _typed_ok(body, payload_hash: str) -> bool:
    if body.approved_via != "typed":
        return True
    return (body.confirm or "").strip().lower() == payload_hash[:8].lower()


@router.get("/records", response_class=HTMLResponse)
def records_page() -> str:
    from app.api.records_page import RECORDS_PAGE
    return RECORDS_PAGE


@router.get("/claims")
def claims(status: str | None = "draft,proposed,conflicting,edited,approved,failed",
           scope: str | None = None, kind: str | None = None,
           limit: int = 200) -> dict:
    rows = node_store.list_claims(_store(), status=status, scope=scope,
                                  kind=kind, limit=min(limit, 1000))
    for r in rows:
        p = node_store.open_packet_for_claim(_store(), r["id"])
        r["open_packet"] = ({"id": p["id"], "payload_hash": p["payload_hash"],
                             "expires_at": p["expires_at"],
                             "include_quote": p["include_quote"]}
                            if p else None)
    groups: dict[str, dict[str, list]] = {}
    for r in rows:
        groups.setdefault(r.get("proposed_scope") or "personal", {}) \
              .setdefault(r["subject_ref"], []).append(r)
    return {"ok": True, "claims": rows, "groups": groups,
            "joined": org_client.joined(),
            "scopes": org_client.cached_scopes()}


@router.get("/claims/{claim_id}")
def claim_detail(claim_id: str):
    store = _store()
    claim = node_store.get_claim(store, claim_id)
    if claim is None:
        return JSONResponse({"ok": False, "code": "no_claim"}, status_code=404)
    for e in claim["evidence"]:
        tomb = retention.tombstone(store, int(e.get("event_id") or 0))
        if tomb:
            e["status"] = "expired"
    packets = node_store.packets_for_claim(store, claim_id)
    current = None
    if org_client.joined() and claim.get("proposed_scope"):
        subject = claim["subject_ref"]
        if subject == "self":
            subject = f"member:{org_client.member_id()}"
        try:
            current = org_client.current_record(claim["proposed_scope"],
                                                subject, claim["predicate"])
        except Exception as exc:
            current = {"error": str(exc)}
    open_packet = next((p for p in reversed(packets) if p["state"] == "open"),
                       None)
    return {"ok": True, "claim": claim, "packets": packets,
            "current_record": current,
            "quote_allowed": promotion.quote_allowed(claim),
            "preview": (open_packet or {}).get("payload", {}).get("preview")}


@router.post("/claims/{claim_id}/propose")
def propose(claim_id: str, body: ProposeBody):
    try:
        return promotion.propose(_store(), claim_id, scope_id=body.scope_id,
                                 target_ref=body.target_ref,
                                 include_quote=body.include_quote)
    except PromotionError as exc:
        return _err(exc)
    except Exception as exc:
        from app.services.records.node_store import TransitionError
        if isinstance(exc, TransitionError):
            return _err(PromotionError("bad_state", str(exc)))
        raise


@router.post("/claims/{claim_id}/discard")
def discard(claim_id: str, body: DiscardBody):
    try:
        return promotion.discard(_store(), claim_id, reason=body.reason)
    except PromotionError as exc:
        return _err(exc)


@router.post("/claims/{claim_id}/retry")
def retry(claim_id: str):
    try:
        return promotion.retry(_store(), claim_id)
    except PromotionError as exc:
        return _err(exc)


@router.post("/packets/{packet_id}/decide")
def decide(packet_id: str, body: DecideBody, request: Request):
    source, meta = _source(request)
    if not _typed_ok(body, body.payload_hash):
        return _err(PromotionError("typed_confirmation_mismatch"), 422)
    try:
        out = promotion.decide(
            _store(), packet_id, body.decision, body.payload_hash,
            source=source, meta=meta, approved_via=body.approved_via,
            value=body.value, scope_id=body.scope_id,
            target_ref=body.target_ref, include_quote=body.include_quote,
            reason=body.reason)
    except PromotionError as exc:
        return _err(exc)
    status = 200 if out.get("ok") else 409
    return JSONResponse(out, status_code=status)


@router.post("/packets/{packet_id}/forward")
def forward(packet_id: str, request: Request):
    source, _meta = _source(request)
    try:
        return promotion.forward(_store(), packet_id, source=source)
    except PromotionError as exc:
        return _err(exc)
    except (org_client.OrgRefused, org_client.OrgUnavailable) as exc:
        return _org_err(exc)


@router.get("/records/forwarded")
def forwarded():
    if not org_client.joined():
        return {"ok": True, "packets": []}
    try:
        return {"ok": True, "packets": org_client.forwarded_packets()}
    except (org_client.OrgRefused, org_client.OrgUnavailable) as exc:
        return _org_err(exc)


@router.post("/records/forwarded/{packet_id}/approve")
def approve_forwarded(packet_id: str, body: ForwardedApproveBody,
                      request: Request):
    source, meta = _source(request)
    if not _typed_ok(body, body.payload_hash):
        return _err(PromotionError("typed_confirmation_mismatch"), 422)
    try:
        packets = {p["packet_id"]: p for p in org_client.forwarded_packets()}
        packet = packets.get(packet_id)
        if packet is None:
            return _err(PromotionError("no_packet"))
        out = promotion.approve_forwarded(packet, source=source,
                                          approved_via=body.approved_via,
                                          payload_hash=body.payload_hash)
    except PromotionError as exc:
        return _err(exc)
    except (org_client.OrgRefused, org_client.OrgUnavailable) as exc:
        return _org_err(exc)
    try:
        node_store.audit(_store(), org_client.member_id() or "owner",
                         "packet.approve_forwarded", packet_id,
                         {"payload_hash": body.payload_hash})
    except Exception:
        pass
    return {"ok": True, **out}


@router.get("/retention/status")
def retention_status() -> dict:
    return retention.status(_store())


@router.get("/records/membership")
def membership() -> dict:
    m = org_client.membership()
    return {"ok": True, "joined": org_client.joined(),
            "org_id": m.get("org_id"), "member_id": m.get("member_id"),
            "service_url": m.get("service_url"),
            "scopes": m.get("scopes") or [],
            "heartbeat_at": m.get("heartbeat_at")}


@router.post("/records/join")
def join(body: JoinBody, request: Request):
    ok, why = live_session(request)
    if not ok:
        return JSONResponse({"ok": False, "code": "not_live_session",
                             "detail": why}, status_code=403)
    node_id = _node_id()
    try:
        out = org_client.join(body.service_url, body.invite_code, node_id,
                              store=_store())
    except (org_client.OrgRefused, org_client.OrgUnavailable) as exc:
        return _org_err(exc)
    node_store.audit(_store(), out.get("member_id") or "owner", "org.join",
                     out.get("org_id") or "", {"node_id": node_id})
    return {"ok": True, **out}


@router.post("/records/heartbeat")
def heartbeat():
    try:
        out = org_client.heartbeat(_store())
    except (org_client.OrgRefused, org_client.OrgUnavailable) as exc:
        return _org_err(exc)
    return {"ok": True, "scopes": len(out.get("scopes") or []),
            "policy_version": out.get("policy_version"),
            "reconciled": out.get("reconciled")}


@router.get("/records/precision")
def precision() -> dict:
    return {"ok": True, "by_kind": promotion.precision(_store()),
            "at": time.time()}


def _node_id() -> str:
    """Stable per-install id: the existing org-network node id if set, else a
    minted one persisted beside the membership file."""
    try:
        from app.services import org_client as legacy
        nid = legacy.node_id()
        if nid:
            return nid
    except Exception:
        pass
    import uuid
    from pathlib import Path

    from app.config import settings
    p = Path(settings.storage.data_dir) / "records_node_id"
    try:
        nid = p.read_text("utf-8").strip()
        if nid:
            return nid
    except OSError:
        pass
    nid = f"node-{uuid.uuid4().hex[:16]}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(nid, "utf-8")
    return nid
