"""Org Record Service business logic. No SQLAlchemy here — only Repo calls
inside `db.tenant(org_id)` transactions.

Permission model: admin > approve > propose > read, inherited DOWN the scope
tree (a grant on a parent applies to every child). An org admin (members.role
'admin') holds admin on every scope. Every check runs here at request time;
the grant list a node caches is for its UI only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.services.records.canonical import (CanonicalError, canonical_hash,
                                            canonical_json)
from org_coordinator.records import audit, tokens

PERMISSIONS = ("read", "propose", "approve", "admin")
_IMPLIES = {"admin": {"admin", "approve", "propose", "read"},
            "approve": {"approve", "propose", "read"},
            "propose": {"propose", "read"},
            "read": {"read"}}
ROLES = ("admin", "member", "viewer")
SCOPE_KINDS = ("org", "team", "deal", "project", "client")
RUNGS = ("connected", "sealed", "perimeter")
PAYLOAD_VERSION = 1
# Retention ranges (spec, Retention policy). The node clamps to the same.
POLICY_RANGES = {"capture_ttl_days": (7, 365), "claim_ttl_days": (30, 365),
                 "audio_ttl_days": (0, 365), "ambient_audio_ttl_days": (0, 365)}
DEFAULT_POLICY = {"retention": {"capture_ttl_days": 30, "claim_ttl_days": 90,
                                "audio_ttl_days": 7,
                                "ambient_audio_ttl_days": 1,
                                "record_ttl_days": None,
                                "quote_in_records": False},
                  "holds": []}


class ServiceError(Exception):
    """An answer the caller can act on: HTTP status + stable error code."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Principal:
    org_id: str
    member_id: str
    node_id: str
    role: str


def _now(now: float | None) -> float:
    return float(now if now is not None else time.time())


# ------------------------------------------------------------ permissions --
def effective_permissions(repo, member: dict, scope_id: str) -> set[str]:
    if member.get("status") != "active":
        return set()
    if member.get("role") == "admin":
        return set(_IMPLIES["admin"])
    chain = repo.ancestors(scope_id)
    if not chain:
        return set()
    out: set[str] = set()
    for g in repo.grants_for_member(member["id"]):
        if g["scope_id"] in chain:
            out |= _IMPLIES.get(g["permission"], set())
    if member.get("role") == "viewer":
        out &= {"read"}
    return out


def _require(repo, principal: Principal, scope_id: str, perm: str) -> dict:
    member = repo.get_member(principal.member_id)
    if member is None or member["status"] != "active":
        raise ServiceError(403, "member_inactive")
    if repo.get_scope(scope_id) is None:
        raise ServiceError(404, "scope_not_found")
    if perm not in effective_permissions(repo, member, scope_id):
        raise ServiceError(403, f"lacks_{perm}",
                           f"member lacks {perm} on scope {scope_id}")
    return member


def readable_scopes(repo, member: dict) -> list[str]:
    return [s["id"] for s in repo.list_scopes()
            if "read" in effective_permissions(repo, member, s["id"])]


# -------------------------------------------------------------- bootstrap --
def bootstrap_org(db, *, name: str, admin_email: str, rung: str = "connected",
                  now: float | None = None) -> dict[str, Any]:
    """Create an org, its root scope, and an admin member with an invite the
    admin's node redeems. Operator CLI only — never exposed over HTTP."""
    now = _now(now)
    if rung not in RUNGS:
        raise ServiceError(400, "bad_rung")
    org_id = tokens.new_id("org")
    with db.owner(org_id) as repo:
        repo.insert_org(name=name, rung=rung, policy=DEFAULT_POLICY, now=now)
    with db.tenant(org_id) as repo:
        member_id = tokens.new_id("mem")
        repo.insert_member(id=member_id, email=admin_email.strip().lower(),
                           role="admin", status="invited")
        scope_id = tokens.new_id("scp")
        repo.insert_scope(id=scope_id, kind="org", name=name, parent_id=None,
                          external_ref=None, created_at=now)
        code = _mint_invite(repo, member_id, created_by="bootstrap", now=now)
        repo.index_org()
        audit.append(repo, "bootstrap", "org.create", org_id,
                     {"name": name, "rung": rung, "admin": member_id}, at=now)
    return {"org_id": org_id, "admin_member_id": member_id,
            "root_scope_id": scope_id, "invite_code": code}


def _mint_invite(repo, member_id: str, *, created_by: str, now: float) -> str:
    invite_id, secret = tokens.new_id("inv"), tokens.new_secret()
    repo.insert_invite(id=invite_id, member_id=member_id,
                       secret_hash=tokens.hash_secret(secret),
                       created_by=created_by, created_at=now,
                       expires_at=now + tokens.INVITE_TTL_S)
    return tokens.compose(repo.org_id, invite_id, secret)


def create_invite(db, principal: Principal, org_id: str, *, email: str,
                  role: str = "member", display_name: str = "",
                  now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    if org_id != principal.org_id:
        raise ServiceError(403, "org_mismatch")
    if role not in ROLES:
        raise ServiceError(400, "bad_role")
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ServiceError(400, "bad_email")
    with db.tenant(org_id) as repo:
        _require_org_admin(repo, principal)
        member = repo.member_by_email(email)
        if member and member["status"] == "active":
            raise ServiceError(409, "already_member")
        if member is None:
            member = repo.insert_member(id=tokens.new_id("mem"), email=email,
                                        display_name=display_name or None,
                                        role=role, status="invited")
        code = _mint_invite(repo, member["id"], created_by=principal.member_id,
                            now=now)
        audit.append(repo, principal.member_id, "member.invite", member["id"],
                     {"email_hash": canonical_hash(email), "role": role}, at=now)
    return {"member_id": member["id"], "invite_code": code,
            "expires_at": now + tokens.INVITE_TTL_S}


def _require_org_admin(repo, principal: Principal) -> dict:
    member = repo.get_member(principal.member_id)
    if not member or member["status"] != "active" or member["role"] != "admin":
        raise ServiceError(403, "lacks_admin")
    return member


def join(db, *, invite_code: str, node_id: str, label: str = "",
         now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    try:
        org_id, invite_id, secret = tokens.split(invite_code)
    except tokens.TokenError as exc:
        raise ServiceError(400, "bad_invite") from exc
    if not (node_id or "").strip():
        raise ServiceError(400, "bad_node_id")
    with db.tenant(org_id) as repo:
        inv = repo.get_invite_for_update(invite_id)
        if inv is None or not tokens.secret_matches(secret, inv["secret_hash"]):
            raise ServiceError(403, "bad_invite")
        if inv["redeemed_at"] is not None:
            raise ServiceError(409, "invite_used")
        if now >= float(inv["expires_at"]):
            raise ServiceError(410, "invite_expired")
        member = repo.get_member(inv["member_id"])
        if member is None or member["status"] == "departed":
            raise ServiceError(403, "member_inactive")
        repo.redeem_invite(invite_id, now)
        repo.update_member(member["id"], status="active", node_id=node_id,
                           joined_at=now,
                           display_name=member.get("display_name") or label or None)
        cred_id, cred_secret = tokens.new_id("crd"), tokens.new_secret()
        repo.insert_credential(id=cred_id, member_id=member["id"],
                               node_id=node_id,
                               secret_hash=tokens.hash_secret(cred_secret),
                               created_at=now)
        audit.append(repo, member["id"], "member.join", member["id"],
                     {"node_id": node_id, "invite": invite_id}, at=now)
    return {"org_id": org_id, "member_id": member["id"],
            "role": member["role"],
            "credential": tokens.compose(org_id, cred_id, cred_secret)}


def issue_token(db, credential: str, *, now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    try:
        org_id, cred_id, secret = tokens.split(credential)
    except tokens.TokenError as exc:
        raise ServiceError(401, "bad_credential") from exc
    with db.tenant(org_id) as repo:
        cred = repo.get_credential(cred_id)
        if cred is None or not tokens.secret_matches(secret, cred["secret_hash"]):
            raise ServiceError(401, "bad_credential")
        if cred["revoked_at"] is not None:
            raise ServiceError(401, "credential_revoked")
        member = repo.get_member(cred["member_id"])
        if member is None or member["status"] != "active":
            raise ServiceError(401, "member_inactive")
        repo.touch_credential(cred_id, now)
    token, exp = tokens.sign({"org": org_id, "sub": member["id"],
                              "node": cred["node_id"], "cred": cred_id},
                             now=now)
    return {"access_token": token, "expires_at": exp, "member_id": member["id"]}


def authenticate(db, bearer: str | None, *, now: float | None = None) -> Principal:
    if not bearer:
        raise ServiceError(401, "auth_required")
    try:
        claims = tokens.verify(bearer, now=now)
    except tokens.TokenError as exc:
        raise ServiceError(401, "bad_token", str(exc)) from exc
    org_id, member_id = claims.get("org"), claims.get("sub")
    with db.tenant(org_id) as repo:
        member = repo.get_member(member_id)
        cred = repo.get_credential(claims.get("cred") or "")
        # Re-checked on every call: departure and revocation take effect at
        # the next request, not when the 15-minute token runs out.
        if member is None or member["status"] != "active":
            raise ServiceError(401, "member_inactive")
        if cred is None or cred["revoked_at"] is not None:
            raise ServiceError(401, "credential_revoked")
    return Principal(org_id=org_id, member_id=member_id,
                     node_id=claims.get("node") or "", role=member["role"])


# ----------------------------------------------------------------- scopes --
def _scope_view(repo, member: dict, s: dict) -> dict[str, Any]:
    return {"id": s["id"], "kind": s["kind"], "name": s["name"],
            "parent_id": s["parent_id"], "external_ref": s["external_ref"],
            "permissions": sorted(effective_permissions(repo, member, s["id"]))}


def heartbeat(db, principal: Principal, *, peer_url: str | None = None,
              now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    with db.tenant(principal.org_id) as repo:
        member = repo.get_member(principal.member_id)
        if peer_url is not None and peer_url != member.get("peer_url"):
            repo.set_peer_url(principal.member_id, peer_url[:300] or None)
        org = repo.get_org()
        scopes = [_scope_view(repo, member, s) for s in repo.list_scopes()]
        policy = dict(org["policy_json"] or {})
        policy["holds"] = [{"id": h["id"], "name": h["name"],
                            "criteria": h["criteria_json"]}
                           for h in repo.active_holds()]
        forwarded = [{"packet_id": f["packet_id"], "state": f["state"],
                      "record_version_id": f["record_version_id"]}
                     for f in repo.forwarded_by(principal.member_id)]
        sync_out = [{"job_id": j["id"], "state": j["state"],
                     "packet_id": j["packet_id"], "claim_id": j["claim_id"],
                     "record_version_id": j["record_version_id"],
                     "connector_id": j["connector_id"],
                     "target": j["target_ref"], "error": j["last_error"]}
                    for j in repo.sync_outcomes_for(principal.member_id,
                                                    now - 7 * 86400.0)]
        approve_scopes = [s["id"] for s in scopes
                          if {"approve", "admin"} & set(s["permissions"])]
        drift = [_drift_view(repo, d) for d in repo.open_drift(approve_scopes)]
        roster = _roster(repo, member, scopes)
    return {"org_id": principal.org_id, "member_id": principal.member_id,
            "role": member["role"], "rung": org["rung"],
            "policy_version": org["policy_version"], "policy": policy,
            "scopes": [s for s in scopes if s["permissions"]],
            "forwarded": forwarded, "sync": sync_out, "drift": drift,
            "roster": roster}


def _drift_view(repo, d: dict) -> dict[str, Any]:
    rec = repo.get_record(d["record_id"]) or {}
    return {"id": d["id"], "record_id": d["record_id"],
            "scope_id": d["scope_id"], "field": d["field"],
            "written": d["written_value"], "external": d["external_value"],
            "detected_at": d["detected_at"],
            "subject_ref": rec.get("subject_ref"),
            "subject_label": rec.get("subject_label"),
            "predicate": rec.get("predicate"), "kind": rec.get("kind")}


def _roster(repo, member: dict, scopes: list[dict]) -> list[dict[str, Any]]:
    """Team scopes this member reads, with who else reads them — the source
    for the node's read-only org groups in team_layer."""
    people = {m["id"]: m for m in repo.list_members()
              if m["status"] == "active"}
    out = []
    for s in scopes:
        if s["kind"] != "team" or "read" not in s["permissions"]:
            continue
        team = []
        for m in people.values():
            if "read" in effective_permissions(repo, m, s["id"]):
                team.append({"member_id": m["id"],
                             "display_name": m.get("display_name") or m["email"],
                             "peer_url": m.get("peer_url")})
        out.append({"scope_id": s["id"], "name": s["name"], "members": team})
    return out


def list_scopes(db, principal: Principal, org_id: str) -> list[dict]:
    if org_id != principal.org_id:
        raise ServiceError(403, "org_mismatch")
    with db.tenant(org_id) as repo:
        member = repo.get_member(principal.member_id)
        return [v for v in (_scope_view(repo, member, s)
                            for s in repo.list_scopes()) if v["permissions"]]


def create_scope(db, principal: Principal, org_id: str, *, kind: str,
                 name: str, parent_id: str | None,
                 external_ref: str | None = None,
                 now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    if org_id != principal.org_id:
        raise ServiceError(403, "org_mismatch")
    if kind not in SCOPE_KINDS or kind == "org":
        raise ServiceError(400, "bad_scope_kind")
    if not (name or "").strip():
        raise ServiceError(400, "bad_name")
    with db.tenant(org_id) as repo:
        if not parent_id:
            raise ServiceError(400, "parent_required")
        _require(repo, principal, parent_id, "admin")
        sid = tokens.new_id("scp")
        s = repo.insert_scope(id=sid, kind=kind, name=name.strip(),
                              parent_id=parent_id, external_ref=external_ref,
                              created_at=now)
        audit.append(repo, principal.member_id, "scope.create", sid,
                     {"kind": kind, "parent_id": parent_id,
                      "external_ref": external_ref}, at=now)
        member = repo.get_member(principal.member_id)
        return _scope_view(repo, member, s)


def update_scope(db, principal: Principal, org_id: str, scope_id: str,
                 changes: dict[str, Any], *, now: float | None = None) -> dict:
    now = _now(now)
    if org_id != principal.org_id:
        raise ServiceError(403, "org_mismatch")
    allowed = {k: v for k, v in changes.items()
               if k in ("name", "external_ref", "parent_id")}
    with db.tenant(org_id) as repo:
        _require(repo, principal, scope_id, "admin")
        if "parent_id" in allowed:
            new_parent = allowed["parent_id"]
            if not new_parent or repo.get_scope(new_parent) is None:
                raise ServiceError(400, "bad_parent")
            if scope_id in repo.ancestors(new_parent):
                raise ServiceError(400, "scope_cycle")
            _require(repo, principal, new_parent, "admin")
        if allowed:
            repo.update_scope(scope_id, **allowed)
            audit.append(repo, principal.member_id, "scope.update", scope_id,
                         allowed, at=now)
        member = repo.get_member(principal.member_id)
        return _scope_view(repo, member, repo.get_scope(scope_id))


def list_grants(db, principal: Principal, scope_id: str) -> list[dict]:
    with db.tenant(principal.org_id) as repo:
        _require(repo, principal, scope_id, "admin")
        return repo.grants_for_scope(scope_id)


def set_grant(db, principal: Principal, scope_id: str, *, member_id: str,
              permission: str, revoke: bool = False,
              now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    if permission not in PERMISSIONS:
        raise ServiceError(400, "bad_permission")
    with db.tenant(principal.org_id) as repo:
        _require(repo, principal, scope_id, "admin")
        target = repo.get_member(member_id)
        if target is None:
            raise ServiceError(404, "member_not_found")
        if revoke:
            n = repo.delete_grant(scope_id, member_id, permission)
            action = "grant.revoke"
        else:
            if target["role"] == "viewer" and permission != "read":
                raise ServiceError(400, "viewer_read_only")
            repo.insert_grant(scope_id=scope_id, member_id=member_id,
                              permission=permission,
                              granted_by=principal.member_id, created_at=now)
            n, action = 1, "grant.add"
        audit.append(repo, principal.member_id, action,
                     f"{scope_id}:{member_id}", {"permission": permission},
                     at=now)
    return {"ok": True, "changed": n}


# ---------------------------------------------------------------- packets --
_REQUIRED_PAYLOAD = ("v", "packet_id", "org_id", "scope_id", "subject_ref",
                     "kind", "predicate", "value", "valid_from", "evidence",
                     "expires_at", "claim")


def _check_payload(payload: dict[str, Any], payload_hash: str,
                   principal: Principal, now: float) -> None:
    if not isinstance(payload, dict):
        raise ServiceError(422, "invalid_payload", "payload must be an object")
    try:
        actual = canonical_hash(payload)
    except CanonicalError as exc:
        raise ServiceError(422, "invalid_payload", str(exc)) from exc
    # Order matters: a tampered body is reported as tampering even when it is
    # also malformed or stale.
    if actual != (payload_hash or ""):
        raise ServiceError(409, "payload_hash_mismatch",
                           "recomputed hash differs from the approved one")
    missing = [k for k in _REQUIRED_PAYLOAD if k not in payload]
    if missing:
        raise ServiceError(422, "invalid_payload", f"missing {missing}")
    if payload["v"] != PAYLOAD_VERSION:
        raise ServiceError(422, "invalid_payload", "unknown payload version")
    if payload["org_id"] != principal.org_id:
        raise ServiceError(403, "org_mismatch")
    try:
        expires = float(payload["expires_at"])
    except (TypeError, ValueError) as exc:
        raise ServiceError(422, "invalid_payload", "expires_at") from exc
    if now >= expires:
        raise ServiceError(410, "packet_expired")
    ev = payload.get("evidence")
    if not isinstance(ev, list) or not ev or not all(
            isinstance(e, dict) and e.get("event_ref") is not None
            and e.get("quote_hash") for e in ev):
        raise ServiceError(422, "no_evidence",
                           "a record needs at least one evidence pointer")


def _record_view(repo, version: dict, *, idempotent: bool) -> dict[str, Any]:
    return {"record_id": version["record_id"],
            "record_version_id": version["id"],
            "version": version["version"], "idempotent": idempotent}


def submit_packet(db, principal: Principal, body: dict[str, Any], *,
                  now: float | None = None) -> dict[str, Any]:
    """Write one approved packet as a record version. Idempotent on
    payload_hash: a resubmission returns the version it already produced,
    including when two submissions of the same packet race."""
    from org_coordinator.repo import Conflict
    try:
        return _submit_once(db, principal, body, now=now)
    except Conflict:
        with db.tenant(principal.org_id) as repo:
            prior = repo.version_by_payload_hash(body.get("payload_hash") or "")
            if prior is None:
                raise
            return _record_view(repo, prior, idempotent=True)


def _submit_once(db, principal: Principal, body: dict[str, Any], *,
                 now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    payload = body.get("payload")
    payload_hash = body.get("payload_hash") or ""
    approved_via = body.get("approved_via")
    if approved_via not in ("button", "typed"):
        raise ServiceError(422, "bad_approved_via")
    with db.tenant(principal.org_id) as repo:
        # Idempotency before the TTL check: a packet that recorded and is
        # resubmitted after expiry still gets its version back.
        if isinstance(payload, dict):
            try:
                if canonical_hash(payload) == payload_hash:
                    prior = repo.version_by_payload_hash(payload_hash)
                    if prior is not None:
                        return _record_view(repo, prior, idempotent=True)
            except CanonicalError:
                pass
        _check_payload(payload, payload_hash, principal, now)
        scope_id = payload["scope_id"]
        # Grant is checked at SUBMISSION time, not at mint time.
        if repo.get_scope(scope_id) is None:
            raise ServiceError(404, "scope_not_found")
        member = repo.get_member(principal.member_id)
        if "approve" not in effective_permissions(repo, member, scope_id):
            raise ServiceError(403, "approver_lacks_grant",
                               "approver lacks approve on the scope")
        from org_coordinator.records import sync
        sync.check_preview(repo, payload)
        version = _write_version(repo, principal, payload, payload_hash,
                                 approved_via=approved_via, now=now)
        sync.enqueue(repo, version_id=version["id"], payload=payload,
                     payload_hash=payload_hash, version_no=version["version"],
                     proposed_by=payload.get("proposed_by"), now=now)
        fwd = repo.get_forwarded(payload["packet_id"])
        if fwd is not None:
            repo.close_forwarded(payload["packet_id"], "recorded",
                                 version["id"])
        audit.append(repo, principal.member_id, "packet.submit",
                     payload["packet_id"], payload_hash=payload_hash, at=now)
        audit.append(repo, principal.member_id, "record.version.create",
                     version["id"], payload_hash=payload_hash, at=now)
        return _record_view(repo, version, idempotent=False)


def _write_version(repo, principal: Principal, payload: dict, payload_hash: str,
                   *, approved_via: str, now: float) -> dict[str, Any]:
    scope_id = payload["scope_id"]
    key = str(payload.get("record_key") or "")
    rec = repo.record_for_update(scope_id, payload["subject_ref"],
                                 payload["predicate"], key)
    if rec is None:
        repo.insert_record(id=tokens.new_id("rec"), scope_id=scope_id,
                           subject_ref=payload["subject_ref"],
                           subject_label=payload.get("subject_label"),
                           predicate=payload["predicate"], record_key=key,
                           kind=payload["kind"], created_at=now)
        rec = repo.record_for_update(scope_id, payload["subject_ref"],
                                     payload["predicate"], key)
    version_no = int(rec["current_version"]) + 1
    valid_from = float(payload["valid_from"])
    valid_to = payload.get("valid_to")
    # Bi-temporal supersede: stop believing every currently-believed row; any
    # part of an old row's valid interval that precedes the new value is
    # re-asserted as a derived row, so "what was true on day D" still answers
    # from the old value for days before the change.
    believed = repo.believed_versions(rec["id"], now)
    repo.supersede([b["id"] for b in believed], now)
    for b in believed:
        if float(b["valid_from"]) < valid_from:
            end = valid_from if b["valid_to"] is None else min(
                float(b["valid_to"]), valid_from)
            repo.insert_version(
                id=tokens.new_id("rv"), record_id=rec["id"],
                version=b["version"], derived_from=b["id"],
                value_json=b["value_json"], payload_json=None,
                payload_hash=None, claim_id=b["claim_id"],
                packet_id=b["packet_id"], approved_by=b["approved_by"],
                proposed_by=b["proposed_by"], approved_via=b["approved_via"],
                valid_from=float(b["valid_from"]), valid_to=end,
                recorded_at=now, superseded_at=None)
    vid = tokens.new_id("rv")
    repo.insert_version(
        id=vid, record_id=rec["id"], version=version_no, derived_from=None,
        value_json=payload["value"], payload_json=canonical_json(payload),
        payload_hash=payload_hash,
        claim_id=(payload.get("claim") or {}).get("id"),
        packet_id=payload["packet_id"], approved_by=principal.member_id,
        proposed_by=payload.get("proposed_by"), approved_via=approved_via,
        valid_from=valid_from,
        valid_to=float(valid_to) if valid_to is not None else None,
        recorded_at=now, superseded_at=None)
    repo.set_current_version(rec["id"], version_no)
    # A newly approved value answers any open drift question on this record,
    # and an approved acknowledgement (payload.resolves_drift) answers the
    # drift notices it names — on whichever records they sit.
    ack = {str(x) for x in (payload.get("resolves_drift") or [])}
    cleared: set[str] = set()
    for d in repo.open_drift():
        if d["record_id"] == rec["id"] or d["id"] in ack:
            repo.resolve_drift(d["id"], now)
            cleared.add(d["record_id"])
    still = {d["record_id"] for d in repo.open_drift()}
    for rid in cleared | {rec["id"]}:
        if rid not in still:
            repo.set_record_drift(rid, None)
    quote_ok = bool(payload.get("include_quote"))
    repo.insert_evidence([{
        "record_version_id": vid, "node_id": e.get("node_id"),
        "event_ref": int(e["event_ref"]), "quote_hash": e["quote_hash"],
        "quote": (e.get("quote") if quote_ok else None),
        "source": e.get("source"), "t": e.get("t"),
        "evidence_status": e.get("status") or "live"}
        for e in payload["evidence"]])
    return repo.get_version(vid)


def forward_packet(db, principal: Principal, body: dict[str, Any], *,
                   now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    payload, payload_hash = body.get("payload"), body.get("payload_hash") or ""
    with db.tenant(principal.org_id) as repo:
        _check_payload(payload, payload_hash, principal, now)
        scope_id = payload["scope_id"]
        _require(repo, principal, scope_id, "propose")
        repo.insert_forwarded(packet_id=payload["packet_id"],
                              scope_id=scope_id,
                              payload_json=canonical_json(payload),
                              payload_hash=payload_hash,
                              proposed_by=principal.member_id,
                              created_at=now,
                              expires_at=float(payload["expires_at"]))
        approvers = repo.members_with_permission(repo.ancestors(scope_id),
                                                 ("approve", "admin"))
        audit.append(repo, principal.member_id, "packet.forward",
                     payload["packet_id"], payload_hash=payload_hash, at=now)
    return {"forwarded": True, "approvers": len(approvers)}


def forwarded_for(db, principal: Principal, *,
                  now: float | None = None) -> list[dict[str, Any]]:
    import json
    now = _now(now)
    with db.tenant(principal.org_id) as repo:
        member = repo.get_member(principal.member_id)
        scope_ids = [s["id"] for s in repo.list_scopes()
                     if "approve" in effective_permissions(repo, member, s["id"])]
        rows = repo.list_forwarded(scope_ids, now)
    return [{"packet_id": r["packet_id"], "scope_id": r["scope_id"],
             "payload": json.loads(r["payload_json"]),
             "payload_hash": r["payload_hash"],
             "proposed_by": r["proposed_by"], "expires_at": r["expires_at"]}
            for r in rows if r["proposed_by"] != principal.member_id]


# ---------------------------------------------------------------- records --
def query_records(db, principal: Principal, *, scope: str | None = None,
                  subject: str | None = None, predicate: str | None = None,
                  as_of: float | None = None, known_at: float | None = None,
                  limit: int = 200, now: float | None = None) -> list[dict]:
    now = _now(now)
    with db.tenant(principal.org_id) as repo:
        member = repo.get_member(principal.member_id)
        readable = readable_scopes(repo, member)
        if scope:
            if scope not in readable:
                raise ServiceError(403, "lacks_read")
            readable = [s for s in readable if scope in repo.ancestors(s)]
        return repo.query_records(
            scope_ids=readable, subject=subject, predicate=predicate,
            as_of=float(as_of if as_of is not None else now),
            known_at=float(known_at if known_at is not None else now),
            limit=min(int(limit), 1000))


def _readable_record(repo, principal: Principal, record_id: str) -> dict:
    rec = repo.get_record(record_id)
    if rec is None:
        raise ServiceError(404, "record_not_found")
    member = repo.get_member(principal.member_id)
    if "read" not in effective_permissions(repo, member, rec["scope_id"]):
        raise ServiceError(404, "record_not_found")   # don't leak existence
    return rec


def record_versions(db, principal: Principal, record_id: str) -> dict[str, Any]:
    with db.tenant(principal.org_id) as repo:
        rec = _readable_record(repo, principal, record_id)
        return {"record": rec, "versions": [
            {k: v for k, v in row.items() if k != "payload_json"}
            for row in repo.versions(record_id)]}


def provenance(db, principal: Principal, record_id: str,
               version: int | None = None) -> dict[str, Any]:
    import json
    with db.tenant(principal.org_id) as repo:
        rec = _readable_record(repo, principal, record_id)
        rows = [r for r in repo.versions(record_id) if r["derived_from"] is None]
        if version is not None:
            rows = [r for r in rows if int(r["version"]) == int(version)]
        if not rows:
            raise ServiceError(404, "version_not_found")
        v = rows[-1]
        payload = json.loads(v["payload_json"]) if v["payload_json"] else {}
        evidence = repo.evidence_for_version(v["id"])
    return {
        "record": {"id": rec["id"], "scope_id": rec["scope_id"],
                   "subject_ref": rec["subject_ref"],
                   "predicate": rec["predicate"]},
        "version": {"id": v["id"], "version": v["version"],
                    "value": v["value_json"], "valid_from": v["valid_from"],
                    "valid_to": v["valid_to"], "recorded_at": v["recorded_at"],
                    "superseded_at": v["superseded_at"]},
        "packet": {"id": v["packet_id"], "payload_hash": v["payload_hash"],
                   "approved_by": v["approved_by"],
                   "proposed_by": v["proposed_by"],
                   "approved_via": v["approved_via"],
                   "approved_at": v["recorded_at"],
                   "preview": payload.get("preview")},
        "claim": payload.get("claim") or {"id": v["claim_id"]},
        "evidence": [{"node_id": e["node_id"], "event_ref": e["event_ref"],
                      "quote_hash": e["quote_hash"], "quote": e["quote"],
                      "source": e["source"], "t": e["t"],
                      "evidence_status": e["evidence_status"]}
                     for e in evidence],
    }


def evidence_expired(db, principal: Principal, body: dict[str, Any], *,
                     now: float | None = None) -> dict[str, Any]:
    """A node reports expired event refs; matching evidence flips to expired.
    A node can only speak for itself."""
    now = _now(now)
    refs = [int(x) for x in (body.get("event_refs") or [])][:5000]
    with db.tenant(principal.org_id) as repo:
        n = repo.mark_evidence_expired(principal.node_id, refs) if refs else 0
        audit.append(repo, principal.member_id, "evidence.expired",
                     principal.node_id, {"n_refs": len(refs), "n_marked": n},
                     at=now)
    return {"ok": True, "marked": n}


# ----------------------------------------------------------------- policy --
def get_policy(db, principal: Principal, org_id: str) -> dict[str, Any]:
    if org_id != principal.org_id:
        raise ServiceError(403, "org_mismatch")
    with db.tenant(org_id) as repo:
        org = repo.get_org()
    return {"rung": org["rung"], "policy": org["policy_json"],
            "policy_version": org["policy_version"]}


def put_policy(db, principal: Principal, org_id: str, body: dict[str, Any], *,
               now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    if org_id != principal.org_id:
        raise ServiceError(403, "org_mismatch")
    retention = dict((body.get("policy") or {}).get("retention") or {})
    for k, (lo, hi) in POLICY_RANGES.items():
        if k in retention:
            try:
                v = float(retention[k])
            except (TypeError, ValueError) as exc:
                raise ServiceError(422, "bad_policy", k) from exc
            if not lo <= v <= hi:
                raise ServiceError(422, "bad_policy", f"{k} outside {lo}..{hi}")
    cap = float(retention.get("capture_ttl_days",
                              DEFAULT_POLICY["retention"]["capture_ttl_days"]))
    for k in ("audio_ttl_days", "ambient_audio_ttl_days"):
        if k in retention and float(retention[k]) > cap:
            raise ServiceError(422, "bad_policy", f"{k} > capture_ttl_days")
    rtl = retention.get("record_ttl_days")
    if rtl is not None and float(rtl) < 365:
        raise ServiceError(422, "bad_policy", "record_ttl_days null or >= 365")
    rung = body.get("rung")
    if rung is not None and rung not in RUNGS:
        raise ServiceError(422, "bad_rung")
    with db.tenant(org_id) as repo:
        _require_org_admin(repo, principal)
        merged = dict(repo.get_org()["policy_json"] or {})
        merged["retention"] = {**DEFAULT_POLICY["retention"],
                               **(merged.get("retention") or {}), **retention}
        org = repo.set_policy(merged, rung=rung)
        audit.append(repo, principal.member_id, "policy.change", org_id,
                     {"policy": merged, "rung": org["rung"]}, at=now)
    return {"rung": org["rung"], "policy": org["policy_json"],
            "policy_version": org["policy_version"]}


# ------------------------------------------------------------------ audit --
def read_audit(db, principal: Principal, *, t0: float | None = None,
               t1: float | None = None, action: str | None = None,
               after_seq: int = 0, limit: int = 500) -> list[dict]:
    with db.tenant(principal.org_id) as repo:
        _require_org_admin(repo, principal)
        return repo.audit_entries(after_seq=after_seq, limit=min(limit, 5000),
                                  action=action, t0=t0, t1=t1)


def verify_chain(db, org_id: str) -> dict[str, Any]:
    with db.tenant(org_id) as repo:
        anchors = repo.anchors()
        result = audit.verify(repo.iter_audit(), anchors=anchors)
    files = audit.read_anchor_files(org_id)
    if result["ok"] and files:
        by_seq = {int(a["seq"]): a for a in anchors}
        for f in files:
            db_anchor = by_seq.get(int(f.get("seq", -1)))
            if db_anchor is None or db_anchor["entry_hash"] != f.get("entry_hash"):
                return {"ok": False, "reason": "anchor_file_mismatch",
                        "file": f.get("day") or f.get("file")}
        result["anchor_files"] = len(files)
    return result


# ------------------------------------------------------- phase 2: preview --
def preview_packet(db, principal: Principal, payload: dict[str, Any], *,
                   now: float | None = None) -> dict[str, Any]:
    """The external changes this record would make, for the node to embed in
    the payload before it hashes it. Needs `propose` on the scope."""
    from org_coordinator import connectors as conn_mod
    from org_coordinator.records import sync
    if not isinstance(payload, dict) or payload.get("org_id") != principal.org_id:
        raise ServiceError(403, "org_mismatch")
    with db.tenant(principal.org_id) as repo:
        _require(repo, principal, payload.get("scope_id") or "", "propose")
        try:
            return {"preview": sync.previews(repo, payload)}
        except conn_mod.TransientError as exc:
            raise ServiceError(503, "preview_unavailable", str(exc)) from exc
        except conn_mod.ConnectorError as exc:
            raise ServiceError(422, "preview_failed", str(exc)) from exc


# ---------------------------------------------------- phase 2: connectors --
def _connector_view(row: dict) -> dict[str, Any]:
    return {k: row[k] for k in ("id", "kind", "name", "config_json", "status",
                                "created_by", "created_at")} | {
        "has_secret": bool(row.get("secret_enc"))}


def create_connector(db, principal: Principal, org_id: str, *, kind: str,
                     name: str, config: dict, secret: dict,
                     now: float | None = None) -> dict[str, Any]:
    from org_coordinator import connectors as conn_mod
    from org_coordinator.connectors import secrets as sec
    now = _now(now)
    if org_id != principal.org_id:
        raise ServiceError(403, "org_mismatch")
    if kind not in conn_mod.KINDS:
        raise ServiceError(400, "bad_connector_kind")
    with db.tenant(org_id) as repo:
        _require_org_admin(repo, principal)
        cid = tokens.new_id("con")
        try:
            sealed = sec.seal(secret or {}, org_id=org_id, connector_id=cid)
        except sec.SecretsError as exc:
            raise ServiceError(500, "secrets_unavailable", str(exc)) from exc
        row = repo.insert_connector(id=cid, kind=kind, name=name.strip()[:120],
                                    config_json=config or {}, secret_enc=sealed,
                                    status="active",
                                    created_by=principal.member_id,
                                    created_at=now)
        try:
            conn_mod.build(row)          # refuse a config that cannot work
        except conn_mod.ConnectorError as exc:
            raise ServiceError(422, "bad_connector_config", str(exc)) from exc
        audit.append(repo, principal.member_id, "connector.connect", cid,
                     {"kind": kind, "config": config or {}}, at=now)
        return _connector_view(row)


def list_connectors(db, principal: Principal, org_id: str) -> list[dict]:
    if org_id != principal.org_id:
        raise ServiceError(403, "org_mismatch")
    with db.tenant(org_id) as repo:
        _require_org_admin(repo, principal)
        return [_connector_view(r) for r in repo.list_connectors()]


def set_connector_status(db, principal: Principal, connector_id: str,
                         status: str, *, now: float | None = None) -> dict:
    now = _now(now)
    if status not in ("active", "disabled"):
        raise ServiceError(400, "bad_status")
    with db.tenant(principal.org_id) as repo:
        _require_org_admin(repo, principal)
        if repo.get_connector(connector_id) is None:
            raise ServiceError(404, "connector_not_found")
        repo.update_connector(connector_id, status=status)
        audit.append(repo, principal.member_id,
                     "connector.disconnect" if status == "disabled"
                     else "connector.connect", connector_id, {}, at=now)
        return _connector_view(repo.get_connector(connector_id))


def add_mapping(db, principal: Principal, connector_id: str,
                body: dict[str, Any], *, now: float | None = None) -> dict:
    from org_coordinator import connectors as conn_mod
    from org_coordinator.connectors.mapping import MappingError, validate_row
    now = _now(now)
    row = {"kind": str(body.get("kind") or ""),
           "predicate": str(body.get("predicate") or ""),
           "op": str(body.get("op") or ""),
           "object_type": body.get("object_type"),
           "field": body.get("field"),
           "value_path": str(body.get("value_path") or "value"),
           "transform": str(body.get("transform") or "identity")}
    if not row["kind"] or not row["predicate"]:
        raise ServiceError(400, "bad_mapping", "kind and predicate required")
    try:
        validate_row(row)
    except MappingError as exc:
        raise ServiceError(400, "bad_mapping", str(exc)) from exc
    with db.tenant(principal.org_id) as repo:
        _require_org_admin(repo, principal)
        con = repo.get_connector(connector_id)
        if con is None:
            raise ServiceError(404, "connector_not_found")
        if row["op"] not in conn_mod.DEFAULT_OPS[con["kind"]]:
            raise ServiceError(400, "bad_mapping",
                               f"{con['kind']} supports {conn_mod.DEFAULT_OPS[con['kind']]}")
        if row["op"] == "set_property" and not row["field"]:
            raise ServiceError(400, "bad_mapping", "set_property needs a field")
        mid = tokens.new_id("map")
        repo.insert_mapping(id=mid, connector_id=connector_id,
                            created_by=principal.member_id, created_at=now,
                            **row)
        audit.append(repo, principal.member_id, "connector.mapping.add",
                     connector_id, row, at=now)
        return {"mappings": repo.mappings(connector_id)}


def list_mappings(db, principal: Principal, connector_id: str) -> list[dict]:
    with db.tenant(principal.org_id) as repo:
        _require_org_admin(repo, principal)
        if repo.get_connector(connector_id) is None:
            raise ServiceError(404, "connector_not_found")
        return repo.mappings(connector_id)


def delete_mapping(db, principal: Principal, mapping_id: str, *,
                   now: float | None = None) -> dict:
    now = _now(now)
    with db.tenant(principal.org_id) as repo:
        _require_org_admin(repo, principal)
        n = repo.delete_mapping(mapping_id)
        if n:
            audit.append(repo, principal.member_id, "connector.mapping.delete",
                         mapping_id, {}, at=now)
    return {"ok": True, "deleted": n}


def list_sync_jobs(db, principal: Principal, *, state: str | None = None,
                   limit: int = 200) -> list[dict]:
    with db.tenant(principal.org_id) as repo:
        _require_org_admin(repo, principal)
        return repo.list_sync_jobs(state=state, limit=min(limit, 1000))


def list_alerts(db, principal: Principal) -> list[dict]:
    with db.tenant(principal.org_id) as repo:
        _require_org_admin(repo, principal)
        return repo.list_alerts()


def ack_alert(db, principal: Principal, alert_id: str, *,
              now: float | None = None) -> dict:
    now = _now(now)
    with db.tenant(principal.org_id) as repo:
        _require_org_admin(repo, principal)
        return {"ok": True, "acked": repo.ack_alert(alert_id, now)}


# --------------------------------------------- phase 2: peer answer rule --
def _tokens(text: str) -> set[str]:
    import re
    stop = {"the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "is",
            "are", "what", "whats", "what's", "who", "when", "did", "does",
            "do", "we", "our", "it", "that", "this", "with", "about", "status"}
    return {w for w in re.findall(r"[a-z0-9$]{2,}", (text or "").lower())
            if w not in stop}


def answerable(db, principal: Principal, *, asker_member_id: str,
               question: str, limit: int = 3,
               now: float | None = None) -> list[dict[str, Any]]:
    """Records that answer `question` and that BOTH the responder and the
    asker may read. The responder's node answers from these and cites them
    instead of reaching into personal memory."""
    import json
    now = _now(now)
    want = _tokens(question)
    if not want:
        return []
    with db.tenant(principal.org_id) as repo:
        me = repo.get_member(principal.member_id)
        asker = repo.get_member(asker_member_id)
        if asker is None or asker["status"] != "active":
            return []
        shared = sorted(set(readable_scopes(repo, me)) &
                        set(readable_scopes(repo, asker)))
        rows = repo.query_records(scope_ids=shared, subject=None,
                                  predicate=None, as_of=now, known_at=now,
                                  limit=500)
    scored = []
    for r in rows:
        hay = _tokens(" ".join([r.get("subject_label") or "",
                                r.get("subject_ref") or "",
                                (r.get("predicate") or "").replace(".", " "),
                                json.dumps(r.get("value_json") or {})]))
        hits = len(want & hay)
        if hits and hits >= max(1, (len(want) + 1) // 2):
            scored.append((hits, r["recorded_at"], r))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [{"record_id": r["record_id"], "version": r["version"],
             "scope_id": r["scope_id"], "subject_label": r["subject_label"],
             "subject_ref": r["subject_ref"], "predicate": r["predicate"],
             "kind": r["kind"], "value": r["value_json"],
             "valid_from": r["valid_from"], "recorded_at": r["recorded_at"]}
            for _h, _t, r in scored[:limit]]
