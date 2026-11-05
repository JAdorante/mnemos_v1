"""A reviewed assistant note becomes a project record, directly.

The extractor is the wrong route for these. It only promotes owned tasks and a
few typed facts, so a saved decision or requirement could never reach a record,
and it would rewrite text the user has just read and accepted. Here the claim
is built from the accepted note itself: the note's event is the evidence, and
the human who accepted it is the one who approved it. The usual gates still
apply: personal_only, the packet hash, a live session, the service's grant
check at submission, and the shared egress rule on delivery.

A member who can only propose has the packet forwarded to the scope's
approvers instead.
"""
from __future__ import annotations

import time
from typing import Any

from app.services.records import (claim_builder, claim_schemas, node_store,
                                  org_client, promotion, retention)
from app.services.records.canonical import claim_identity_hash, quote_hash

KIND = "project_note"
PREDICATE = "noted"


def subject_ref(project: str) -> str:
    return f"entity:{claim_builder._norm(project)}"


def publish(store, *, event_id: int, project: str, scope_id: str,
            value: dict[str, Any], source: str,
            replaces: dict[str, Any] | None = None,
            now: float | None = None) -> dict[str, Any]:
    """Claim, packet, then approve or forward. `replaces` is an existing record
    of the same scope; this note becomes its next version. Returns a `state`
    of recorded, queued or forwarded, or raises promotion.PromotionError."""
    now = float(now if now is not None else time.time())
    errors = claim_schemas.validate(KIND, value)
    if errors:
        raise promotion.PromotionError("invalid_value", "; ".join(errors))
    ev = claim_builder._event_row(store, event_id)
    if ev is None:
        raise promotion.PromotionError("no_evidence", "the note is gone")
    if replaces and replaces.get("scope_id") != scope_id:
        raise promotion.PromotionError("bad_replaces",
                                       "that record is in another project")
    subject = (replaces or {}).get("subject_ref") or subject_ref(project)
    label = (replaces or {}).get("project") or project
    c_hash = claim_identity_hash(KIND, subject, PREDICATE, value)
    span = value["text"]
    claim_source = retention.capture_source_of(ev["source"], ev["modality"],
                                               ev["meta"])
    claim_id = node_store.insert_claim(store, {
        "candidate_id": None, "kind": KIND, "subject_ref": subject,
        "subject_label": label, "predicate": PREDICATE, "value": value,
        "schema_version": claim_schemas.schema_version(KIND),
        "canonical_hash": c_hash, "confidence": 1.0,
        "evidence": [{"event_id": int(ev["id"]), "span": span,
                      "quote_hash": quote_hash(span), "speaker": "owner",
                      "t": float(ev["time"]), "source": claim_source,
                      "status": "live"}],
        "privacy_class": (ev.get("privacy_class")
                          or ev["meta"].get("privacy_class") or "internal"),
        "capture_source": claim_source,
        "consent_mode": ev.get("consent_mode") or ev["meta"].get("consent_mode"),
        "proposed_scope": scope_id, "status": "draft", "created_at": now,
        "expires_at": now + float(retention.policy()["claim_ttl_days"])
        * retention.DAY})
    node_store.update_claim(store, claim_id,
                            record_key=(replaces or {}).get("record_key") or c_hash)
    claim = node_store.get_claim(store, claim_id)
    why = claim_builder.personal_reason(claim)
    if why:
        node_store.update_claim(store, claim_id, personal_only=True,
                                status_reason=why)
        raise promotion.PromotionError("personal_only", why)

    minted = promotion.propose(store, claim_id, scope_id=scope_id, now=now)
    out = {"claim_id": claim_id, "packet_id": minted["packet_id"]}
    perms = org_client.cached_permissions(scope_id)
    if perms and not ({"approve", "admin"} & perms):
        fwd = promotion.forward(store, minted["packet_id"], source=source, now=now)
        return {**out, "ok": True, "state": "forwarded",
                "approvers": fwd.get("approvers")}
    done = promotion.decide(store, minted["packet_id"], "approve",
                            minted["payload_hash"], source=source,
                            approved_via="button", now=now)
    if not done.get("ok"):
        raise promotion.PromotionError(done.get("code") or "refused",
                                       done.get("detail") or "")
    if done.get("queued"):
        return {**out, "ok": True, "state": "queued"}
    return {**out, "ok": True, "state": "recorded",
            "record_id": done.get("record_id"),
            "record_version_id": done.get("record_version_id")}
