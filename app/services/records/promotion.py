"""Promotion — a claim becomes a record only through a hash-bound human approval.

The action_packets contract (plan 0.4) applied to writes into the org record:

  propose   mint a promotion packet: payload_json is the EXACT record body the
            Org Record Service will write; payload_hash = sha256 of its
            canonical JSON; TTL 7 days.
  decide    approve / edit / reject. Approve requires a live human session
            (source == LIVE_SESSION, approved_via button|typed) and the
            payload_hash the human saw; a drifted or stale hash is refused.
            Edit closes the packet as `edited` and mints a new one with a new
            hash. Retrieved memory, peer answers and agent steps never reach
            approve: anything but LIVE_SESSION goes through
            trust.source_can_authorize, which is False for every source.
  deliver   submit payload + hash to the service, which recomputes the hash and
            refuses on mismatch, expiry, or a missing `approve` grant. If the
            service is down the submit waits in the outbox; packet TTL still
            applies.

Every transition writes a metadata-only node audit entry; every terminal
verdict feeds the learning loop (learning_store) keyed by claim kind.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.perception.schemas import new_ulid
from app.services.records import claim_schemas, node_store, org_client, retention
from app.services.records.claim_builder import MULTI_VALUED
from app.services.records.canonical import (canonical_hash, canonical_json,
                                            claim_identity_hash)

PACKET_TTL_S = 7 * 86400.0
PAYLOAD_VERSION = 1
LIVE_SESSION = "human.session"
APPROVED_VIA = ("button", "typed")
QUOTE_SOURCES = ("meeting", "web", "external", "document")


class PromotionError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.message = code, message


def _audit(store, actor: str, action: str, ref: str, **payload: Any) -> None:
    try:
        node_store.audit(store, actor, action, ref, payload)
    except Exception as exc:
        print(f"[promotion] audit {action} skipped ({exc}).")


def _org_subject(subject_ref: str, member: str | None) -> str:
    return f"member:{member}" if subject_ref == "self" and member else subject_ref


def quote_allowed(claim: dict[str, Any]) -> bool:
    """Org policy allows quotes AND the claim's source may carry one."""
    return (bool(retention.policy().get("quote_in_records"))
            and claim.get("capture_source") in QUOTE_SOURCES)


def build_payload(claim: dict[str, Any], *, packet_id: str, scope_id: str,
                  target_ref: str | None, include_quote: bool,
                  minted_at: float, expires_at: float) -> dict[str, Any]:
    """The exact record body. Everything the service writes is in here, so the
    hash covers it — scope, value, evidence pointers, and any quote."""
    m = org_client.membership()
    member = m.get("member_id")
    evidence = []
    for e in claim.get("evidence") or []:
        item = {"node_id": m.get("node_id"), "event_ref": int(e["event_id"]),
                "quote_hash": e["quote_hash"], "t": e.get("t"),
                "source": e.get("source"),
                "status": e.get("status") or "live"}
        if include_quote:
            item["quote"] = e.get("span") or ""
        evidence.append(item)
    times = [float(e["t"]) for e in claim.get("evidence") or [] if e.get("t")]
    return {
        "v": PAYLOAD_VERSION,
        "packet_id": packet_id,
        "org_id": m.get("org_id"),
        "scope_id": scope_id,
        "target_ref": target_ref,
        "subject_ref": _org_subject(claim["subject_ref"], member),
        "subject_label": claim.get("subject_label"),
        "kind": claim["kind"],
        "schema_version": claim["schema_version"],
        "predicate": claim["predicate"],
        # Multi-valued predicates (one person owes many things) key the
        # record on the value too; single-valued ones version in place.
        "record_key": (claim["canonical_hash"]
                       if claim["predicate"] in MULTI_VALUED else ""),
        "value": claim["value"],
        "valid_from": min(times) if times else minted_at,
        "claim": {"id": claim["id"], "canonical_hash": claim["canonical_hash"],
                  "confidence": float(claim["confidence"]),
                  "capture_source": claim["capture_source"],
                  "candidate_id": claim.get("candidate_id")},
        "evidence": evidence,
        "include_quote": bool(include_quote),
        "proposed_by": member,
        "minted_at": minted_at,
        "expires_at": expires_at,
        "preview": None,     # connector field diff lands here in Phase 2
    }


def _mint(store, claim: dict[str, Any], *, scope_id: str,
          target_ref: str | None, include_quote: bool,
          now: float) -> dict[str, Any]:
    pid = new_ulid(int(now * 1000))
    expires = now + PACKET_TTL_S
    payload = build_payload(claim, packet_id=pid, scope_id=scope_id,
                            target_ref=target_ref, include_quote=include_quote,
                            minted_at=now, expires_at=expires)
    p_hash = canonical_hash(payload)
    node_store.insert_packet(store, {
        "id": pid, "claim_id": claim["id"],
        "payload_json": canonical_json(payload), "payload_hash": p_hash,
        "scope_id": scope_id, "target_ref": target_ref,
        "include_quote": include_quote, "expires_at": expires,
        "created_at": now})
    return {"packet_id": pid, "payload": payload, "payload_hash": p_hash,
            "expires_at": expires}


def propose(store, claim_id: str, *, scope_id: str,
            target_ref: str | None = None, include_quote: bool = False,
            actor: str = "user", now: float | None = None) -> dict[str, Any]:
    now = float(now if now is not None else time.time())
    if not org_client.joined():
        raise PromotionError("not_joined", "this node is not in an org")
    claim = node_store.get_claim(store, claim_id)
    if claim is None:
        raise PromotionError("no_claim")
    if claim["personal_only"]:
        raise PromotionError("personal_only", claim.get("status_reason") or "")
    if claim["status"] not in ("draft", "conflicting", "proposed"):
        raise PromotionError("bad_state", claim["status"])
    if not scope_id:
        raise PromotionError("no_scope")
    if include_quote and not quote_allowed(claim):
        raise PromotionError("quote_not_allowed")
    if not [e for e in claim["evidence"] if e.get("event_id")]:
        raise PromotionError("no_evidence")
    # Re-proposing replaces the open packet: one live packet per claim.
    for p in node_store.packets_for_claim(store, claim_id):
        if p["state"] == "open":
            node_store.update_packet(store, p["id"], state="superseded")
    loser = claim.get("conflict_with") if claim["status"] == "conflicting" else None
    node_store.transition(store, claim_id, "proposed", reason=None,
                          proposed_scope=scope_id, target_hint=target_ref)
    claim = node_store.get_claim(store, claim_id)
    minted = _mint(store, claim, scope_id=scope_id, target_ref=target_ref,
                   include_quote=include_quote, now=now)
    if loser:
        # The reviewer picked this side of a conflict; the other closes.
        try:
            node_store.transition(store, loser, "rejected",
                                  reason=f"reviewer picked {claim_id}")
            _learn(store, node_store.get_claim(store, loser), "rejected")
        except node_store.TransitionError:
            pass
    _audit(store, actor, "packet.mint", minted["packet_id"],
           claim_id=claim_id, scope_id=scope_id,
           payload_hash=minted["payload_hash"])
    return {"ok": True, "claim_id": claim_id, **minted}


def _require_live_human(source: str, approved_via: str | None,
                        meta: dict | None = None) -> None:
    if source != LIVE_SESSION:
        from app.services.trust import source_can_authorize
        if not source_can_authorize(source, meta):
            raise PromotionError("not_live_session",
                                 f"source {source!r} cannot approve")
    if approved_via not in APPROVED_VIA:
        raise PromotionError("bad_approved_via", str(approved_via))


def decide(store, packet_id: str, decision: str, payload_hash: str, *,
           source: str, approved_via: str | None = None,
           value: dict | None = None, scope_id: str | None = None,
           target_ref: str | None = None, include_quote: bool | None = None,
           reason: str | None = None, meta: dict | None = None,
           now: float | None = None) -> dict[str, Any]:
    """approve | edit | reject one packet. `source` must be LIVE_SESSION."""
    now = float(now if now is not None else time.time())
    decision = (decision or "").strip().lower()
    if decision not in ("approve", "edit", "reject"):
        raise PromotionError("bad_decision", decision)
    # Every decision, including reject, is a human act: the gate is first.
    _require_live_human(source, approved_via, meta)
    packet = node_store.get_packet(store, packet_id)
    if packet is None:
        raise PromotionError("no_packet")
    if packet["state"] != "open":
        raise PromotionError("packet_closed", packet["state"])
    if (payload_hash or "") != packet["payload_hash"]:
        raise PromotionError("payload_hash_mismatch",
                             "the packet changed since it was shown")
    if canonical_hash(packet["payload"]) != packet["payload_hash"]:
        raise PromotionError("payload_hash_mismatch", "stored payload drifted")
    if now >= float(packet["expires_at"]):
        node_store.update_packet(store, packet_id, state="expired")
        raise PromotionError("packet_expired")
    claim = node_store.get_claim(store, packet["claim_id"])
    member = org_client.member_id()

    if decision == "reject":
        node_store.update_packet(store, packet_id, state="rejected",
                                 decision="reject", approved_via=approved_via,
                                 approved_by=member, approved_at=now)
        node_store.transition(store, claim["id"], "rejected",
                              reason=reason or "rejected by reviewer")
        _learn(store, claim, "rejected", reason=reason)
        _audit(store, member or "owner", "packet.reject", packet_id,
               payload_hash=packet["payload_hash"])
        return {"ok": True, "decision": "reject", "claim_status": "rejected"}

    if decision == "edit":
        new_value = value if value is not None else claim["value"]
        errors = claim_schemas.validate(claim["kind"], new_value)
        if errors:
            raise PromotionError("invalid_value", "; ".join(errors))
        quote = (packet["include_quote"] if include_quote is None
                 else bool(include_quote))
        if quote and not quote_allowed(claim):
            raise PromotionError("quote_not_allowed")
        node_store.update_packet(store, packet_id, state="edited",
                                 decision="edit", approved_via=approved_via,
                                 approved_by=member, approved_at=now)
        c_hash = claim_identity_hash(claim["kind"], claim["subject_ref"],
                                     claim["predicate"], new_value)
        node_store.transition(store, claim["id"], "edited", value=new_value,
                              canonical_hash=c_hash,
                              proposed_scope=scope_id or packet["scope_id"],
                              target_hint=target_ref or packet["target_ref"])
        claim = node_store.get_claim(store, claim["id"])
        minted = _mint(store, claim, scope_id=scope_id or packet["scope_id"],
                       target_ref=target_ref or packet["target_ref"],
                       include_quote=quote, now=now)
        _audit(store, member or "owner", "packet.edit", packet_id,
               new_packet=minted["packet_id"],
               payload_hash=minted["payload_hash"])
        return {"ok": True, "decision": "edit", "claim_status": "edited",
                **minted}

    # approve
    perms = org_client.cached_permissions(packet["scope_id"])
    if perms and not ({"approve", "admin"} & perms):
        raise PromotionError("approver_lacks_grant",
                             "you can propose here but not approve — forward it")
    node_store.update_packet(store, packet_id, state="approved",
                             decision="approve", approved_via=approved_via,
                             approved_by=member, approved_at=now)
    edited = claim["status"] == "edited"
    node_store.transition(store, claim["id"], "approved", reason=None)
    _learn(store, claim, "edited" if edited else "accepted")
    _audit(store, member or "owner", "packet.approve", packet_id,
           payload_hash=packet["payload_hash"], approved_via=approved_via)
    node_store.enqueue(store, "packet_submit", packet_id,
                       {"packet_id": packet_id}, now=now)
    try:
        delivered = deliver(store, packet_id, now=now)
    except org_client.OrgUnavailable as exc:
        return {"ok": True, "decision": "approve", "claim_status": "approved",
                "queued": True, "detail": str(exc)}
    except org_client.OrgRefused as exc:
        return {"ok": False, "decision": "approve", "claim_status": "failed",
                "code": exc.code, "detail": exc.detail}
    _mark_outbox_done(store, packet_id)
    return {"ok": True, "decision": "approve", **delivered}


def _mark_outbox_done(store, packet_id: str) -> None:
    for row in node_store.due_outbox(store, now=time.time() + 10 ** 9):
        if row["kind"] == "packet_submit" and row["ref"] == packet_id:
            node_store.outbox_done(store, row["id"])


def submission_body(packet: dict[str, Any]) -> dict[str, Any]:
    return {"packet_id": packet["id"], "payload": packet["payload"],
            "payload_hash": packet["payload_hash"],
            "approved_via": packet.get("approved_via"),
            "approved_at": packet.get("approved_at")}


def deliver(store, packet_id: str, *, now: float | None = None) -> dict[str, Any]:
    """Submit one approved packet. Raises OrgUnavailable to be retried;
    a refusal fails the claim and re-raises OrgRefused."""
    now = float(now if now is not None else time.time())
    packet = node_store.get_packet(store, packet_id)
    if packet is None or packet["state"] not in ("approved", "submitted"):
        return {"claim_status": None, "skipped": "not_approved"}
    claim = node_store.get_claim(store, packet["claim_id"])
    if now >= float(packet["expires_at"]):
        node_store.update_packet(store, packet_id, state="failed",
                                 last_error="packet_expired")
        node_store.transition(store, claim["id"], "failed",
                              reason="packet_expired before delivery")
        raise org_client.OrgRefused(410, "packet_expired", "TTL passed in outbox")
    node_store.update_packet(store, packet_id, state="submitted",
                             submitted_at=now,
                             submitted_hash=packet["payload_hash"],
                             submit_attempts=int(packet["submit_attempts"]) + 1)
    try:
        out = org_client.submit_packet(submission_body(packet))
    except org_client.OrgRefused as exc:
        node_store.update_packet(store, packet_id, state="failed",
                                 last_error=f"{exc.code}: {exc.detail}"[:500])
        node_store.transition(store, claim["id"], "failed",
                              reason=f"service refused: {exc.code}")
        _audit(store, "system", "packet.refused", packet_id, code=exc.code)
        raise
    except org_client.OrgUnavailable:
        node_store.update_packet(store, packet_id, state="approved")
        raise
    ref = str(out.get("record_version_id") or "")
    node_store.update_packet(store, packet_id, state="recorded", record_ref=ref)
    node_store.transition(store, claim["id"], "recorded", record_ref=ref)
    _audit(store, "system", "packet.recorded", packet_id,
           record_version_id=ref, idempotent=bool(out.get("idempotent")))
    return {"claim_status": "recorded", "record_version_id": ref,
            "record_id": out.get("record_id"),
            "idempotent": bool(out.get("idempotent"))}


def forward(store, packet_id: str, *, source: str,
            now: float | None = None) -> dict[str, Any]:
    """Owner holds only `propose`: route the packet to the scope's approvers.
    Only payload_json (pointers, never Tier 1 content) leaves the node."""
    if source != LIVE_SESSION:
        raise PromotionError("not_live_session")
    packet = node_store.get_packet(store, packet_id)
    if packet is None or packet["state"] != "open":
        raise PromotionError("packet_closed")
    out = org_client.forward_packet({"packet_id": packet_id,
                                     "payload": packet["payload"],
                                     "payload_hash": packet["payload_hash"]})
    node_store.update_packet(store, packet_id, state="forwarded")
    node_store.update_claim(store, packet["claim_id"],
                            status_reason="forwarded to scope approvers")
    _audit(store, org_client.member_id() or "owner", "packet.forward",
           packet_id, payload_hash=packet["payload_hash"])
    return {"ok": True, **out}


def approve_forwarded(packet: dict[str, Any], *, source: str,
                      approved_via: str, payload_hash: str,
                      now: float | None = None) -> dict[str, Any]:
    """An approver's node approves a packet forwarded to them. They saw the
    payload the service holds; the hash they saw must still match it."""
    now = float(now if now is not None else time.time())
    _require_live_human(source, approved_via)
    if payload_hash != packet.get("payload_hash") or \
            canonical_hash(packet.get("payload") or {}) != payload_hash:
        raise PromotionError("payload_hash_mismatch")
    return org_client.submit_packet({
        "packet_id": packet["packet_id"], "payload": packet["payload"],
        "payload_hash": payload_hash, "approved_via": approved_via,
        "approved_at": now})


def discard(store, claim_id: str, *, reason: str = "",
            actor: str = "owner") -> dict[str, Any]:
    claim = node_store.get_claim(store, claim_id)
    if claim is None:
        raise PromotionError("no_claim")
    try:
        node_store.transition(store, claim_id, "discarded",
                              reason=reason or "discarded by owner")
    except node_store.TransitionError as exc:
        raise PromotionError("bad_state", str(exc)) from exc
    for p in node_store.packets_for_claim(store, claim_id):
        if p["state"] == "open":
            node_store.update_packet(store, p["id"], state="superseded")
    _learn(store, claim, "dismissed", reason=reason)
    _audit(store, actor, "claim.discard", claim_id)
    return {"ok": True, "claim_status": "discarded"}


def retry(store, claim_id: str, *, now: float | None = None) -> dict[str, Any]:
    """failed -> approved again, when its packet is still inside TTL."""
    now = float(now if now is not None else time.time())
    claim = node_store.get_claim(store, claim_id)
    if claim is None or claim["status"] != "failed":
        raise PromotionError("bad_state")
    packets = [p for p in node_store.packets_for_claim(store, claim_id)
               if p["state"] == "failed" and p.get("decision") == "approve"]
    if not packets or now >= float(packets[-1]["expires_at"]):
        raise PromotionError("needs_reapproval",
                             "packet expired — propose and approve again")
    pid = packets[-1]["id"]
    node_store.update_packet(store, pid, state="approved", last_error=None)
    node_store.transition(store, claim_id, "approved", reason="retry")
    node_store.enqueue(store, "packet_submit", pid, {"packet_id": pid}, now=now)
    return {"ok": True, "packet_id": pid, "queued": True}


def _learn(store, claim: dict[str, Any] | None, verdict: str, *,
           reason: str | None = None) -> None:
    """Terminal verdicts -> learning_store, so extraction learns what gets
    accepted. Input is the evidence span (the extractor's input slice)."""
    if not claim:
        return
    try:
        from app.services import learning_store
        spans = " | ".join(e.get("span") or "" for e in claim.get("evidence") or [])
        target = json.dumps(claim.get("value") or {}, sort_keys=True,
                            ensure_ascii=False)
        learning_store.record(
            task_type=f"records.claim.{claim['kind']}",
            input_text=spans, local_output=target,
            final_target=target if verdict in ("accepted", "edited") else "",
            verdict=verdict, verdict_source="records.review",
            source_refs={"claim_id": claim["id"],
                         "candidate_id": claim.get("candidate_id"),
                         "reason": reason},
            store=store)
    except Exception as exc:
        print(f"[promotion] learning record skipped ({exc}).")


def precision(store) -> dict[str, Any]:
    """Approval precision per kind = approved / reviewed (readiness metric)."""
    with store._lock:
        rows = store._conn.execute(
            "SELECT kind, status, COUNT(*) AS n FROM claims GROUP BY kind, status"
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = out.setdefault(r["kind"], {"approved": 0, "rejected": 0})
        if r["status"] in ("approved", "recorded"):
            k["approved"] += int(r["n"])
        elif r["status"] == "rejected":
            k["rejected"] += int(r["n"])
        elif r["status"] == "failed":
            k["approved"] += int(r["n"])   # a human approved it; delivery failed
    for k in out.values():
        reviewed = k["approved"] + k["rejected"]
        k["reviewed"] = reviewed
        k["precision"] = round(k["approved"] / reviewed, 3) if reviewed else None
    return out
