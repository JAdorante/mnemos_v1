"""claim_builder — turn gated fact_candidates into Tier 2 claims.

Reads candidates the extractor already accepted (status='accepted' means
fact_gate.gate_fact passed them) whose kind maps to a promotable claim kind:

  commitment (form=promise)   -> commitment, predicate 'owes', subject = promiser
  task with a named owner     -> commitment, predicate 'owes', subject = owner
  claim with subject/predicate/object (costs|priced_at|due_on)
                              -> fact, predicate as extracted

Meetings (commitment form=meeting), free-text claims and ownerless tasks are
not promotable: there is no subject a record could attach to. `decision`,
`status` and `field_update` have schemas but no extractor output yet — the
extractor prompt does not ask for them (records-layer follow-up).

Rules enforced here (spec, three-tier rules + capture sources):
  * no evidence (event id + verbatim span) -> no claim
  * privacy_class sensitive / never-send   -> personal_only, never proposable
  * ambient-only evidence                  -> personal_only unless every
                                              evidence speaker is the owner
  * screen evidence                        -> proposable only for status /
                                              field_update kinds
  * self_only consent evidence             -> only the owner's own statements

Dedup: single-valued predicates (a price, a stage) dedupe on subject +
predicate — a different value marks both claims `conflicting`. Multi-valued
predicates ('owes', 'decided': one person owes many things) dedupe on the
value too, so two different promises are two claims, not a conflict. That is
a deliberate narrowing of the spec's subject+predicate rule.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from app.services.records import claim_schemas, node_store, retention
from app.services.records.canonical import claim_identity_hash, quote_hash

MULTI_VALUED = frozenset({"owes", "decided"})
FACT_PREDICATES = frozenset({"costs", "priced_at", "due_on"})
_PRIVACY_ORDER = ("public", "internal", "personal", "sensitive", "never-send")
_CONSENT_ORDER = (None, "notified", "consented", "self_only")
_SELF_WORDS = frozenset({"me", "i", "myself", "self", "speaker", "user"})
BATCH = 200


def enabled() -> bool:
    return (os.environ.get("QUILL_RECORDS") or "0").strip().lower() in (
        "1", "on", "true", "yes")


def auto_propose_min_conf() -> float:
    try:
        return float(os.environ.get("QUILL_CLAIM_PROPOSE_MIN_CONF", "0.7"))
    except ValueError:
        return 0.7


def _norm(name: str) -> str:
    from app.services.entity_alias import normalize
    return normalize(name or "")


def _is_self(name: str) -> bool:
    return (name or "").strip().lower() in _SELF_WORDS


def _entity_id(store, name: str) -> int | None:
    """entity_alias.resolve steps 1-3 (exact, confirmed alias, normalized
    name/alias) without step 4: the embedding probe would load MiniLM for
    every unresolved subject and can only ever propose, never bind."""
    try:
        eid = store.find_entity_exact(name)
        if eid:
            return int(eid)
        norm = _norm(name)
        if not norm:
            return None
        eid = store.find_entity_by_alias_norm(norm)
        if eid:
            return int(eid)
        for e in store.all_entities():
            names = [e.get("name") or ""] + list(e.get("aliases") or [])
            if any(_norm(n) == norm for n in names if n):
                return int(e["id"])
    except Exception:
        return None
    return None


def resolve_subject(store, name: str, *, prefer: str = "person") -> tuple[str, str] | None:
    """(subject_ref, label) — org-portable, read-only resolution.

    Node-local row ids mean nothing on another node, so the ref is the
    normalized canonical name of what the name binds to. Never mints a node,
    never records an alias (record=False)."""
    name = (name or "").strip()
    if not name:
        return None
    if _is_self(name):
        return ("self", "me")
    order = ("person", "entity") if prefer == "person" else ("entity", "person")
    for kind in order:
        if kind == "person":
            try:
                pid = store.find_person_exact(name)
            except Exception:
                pid = None
            if pid:
                return (f"person:{_norm(name)}", name)
        else:
            eid = _entity_id(store, name)
            if eid:
                label = name
                try:
                    with store._lock:
                        r = store._conn.execute(
                            "SELECT name FROM entities WHERE id = ?",
                            (int(eid),)).fetchone()
                    label = (r["name"] if r else None) or name
                except Exception:
                    pass
                return (f"entity:{_norm(label)}", label)
    norm = _norm(name)
    return (f"{prefer}:{norm}", name) if norm else None


def map_candidate(cand: dict[str, Any]) -> dict[str, Any] | None:
    """Kind mapping only — no store access. None = not promotable."""
    try:
        p = json.loads(cand.get("payload_json") or "{}")
    except ValueError:
        return None
    kind = cand.get("kind")
    text = (p.get("text") or "").strip()
    if kind == "commitment":
        if (p.get("form") or "promise") != "promise" or not text:
            return None
        owner = (p.get("from_person") or "").strip()
        if not owner:
            return None
        return {"kind": "commitment", "subject_name": owner, "prefer": "person",
                "predicate": "owes",
                "value": {"text": text, "owner": owner,
                          "counterparty": (p.get("to_person") or "").strip(),
                          "due": _due(p.get("due"))}}
    if kind == "task":
        owner = (p.get("owner") or "").strip()
        if not owner or not text:
            return None
        return {"kind": "commitment", "subject_name": owner, "prefer": "person",
                "predicate": "owes",
                "value": {"text": text, "owner": owner, "counterparty": "",
                          "due": _due(p.get("due"))}}
    if kind == "claim":
        pred = (p.get("predicate") or "").strip()
        subj = (p.get("subject") or "").strip()
        obj = (p.get("object") or "").strip()
        if pred not in FACT_PREDICATES or not subj or not obj or not text:
            return None
        return {"kind": "fact", "subject_name": subj, "prefer": "entity",
                "predicate": pred, "value": {"text": text, "value": obj}}
    return None


def _due(raw: Any) -> str:
    s = (str(raw or "")).strip()
    import re
    return s if re.match(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$", s) else ""


def _event_row(store, event_id: int | None) -> dict | None:
    if not event_id:
        return None
    with store._lock:
        r = store._conn.execute(
            "SELECT id, time, source, modality, meta, privacy_class, "
            "consent_mode FROM events WHERE id = ?", (int(event_id),)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["meta"] = json.loads(d.get("meta") or "{}")
    except ValueError:
        d["meta"] = {}
    return d


def _strictest(values: list, order: tuple) -> Any:
    best, rank = None, -1
    for v in values:
        i = order.index(v) if v in order else len(order) - 1
        if i > rank:
            best, rank = v, i
    return best


def _scope_suggestion(subject_label: str) -> tuple[str | None, str | None]:
    """(scope_id, target_hint) from the node's cached scope list. A scope whose
    name matches the subject wins; else the member's only proposable scope."""
    try:
        from app.services.records import org_client
        scopes = org_client.cached_scopes()
    except Exception:
        return None, None
    usable = [s for s in scopes
              if {"propose", "approve", "admin"} & set(s.get("permissions") or [])]
    want = _norm(subject_label)
    for s in usable:
        if want and _norm(s.get("name") or "") == want:
            return s["id"], s.get("external_ref")
    # The org root is almost never where a record belongs; an admin holds it
    # on top of their team, which must not make the team ambiguous.
    narrow = [s for s in usable if s.get("kind") != "org"] or usable
    if len(narrow) == 1:
        return narrow[0]["id"], narrow[0].get("external_ref")
    return None, None


def build_one(store, cand: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Build (or merge) the claim for one candidate. Returns an outcome dict."""
    now = float(now if now is not None else time.time())
    if node_store.claim_for_candidate(store, int(cand["id"])):
        return {"outcome": "exists"}
    mapped = map_candidate(cand)
    if mapped is None:
        return {"outcome": "not_promotable"}
    span = (cand.get("source_span") or "").strip()
    ev_row = _event_row(store, cand.get("source_event_id"))
    if not span or ev_row is None:
        return {"outcome": "no_evidence"}       # rule 1: no evidence, no claim
    errors = claim_schemas.validate(mapped["kind"], mapped["value"])
    if errors:
        return {"outcome": "invalid", "errors": errors}
    subject = resolve_subject(store, mapped["subject_name"],
                              prefer=mapped["prefer"])
    if subject is None:
        return {"outcome": "no_subject"}
    subject_ref, subject_label = subject

    meta = ev_row["meta"]
    source = retention.capture_source_of(ev_row["source"], ev_row["modality"],
                                         meta)
    speaker = (cand.get("speaker") or "").strip()
    evidence = [{
        "event_id": int(ev_row["id"]), "span": span,
        "quote_hash": quote_hash(span), "speaker": speaker,
        "t": float(ev_row["time"]), "source": source, "status": "live",
    }]
    privacy = ev_row.get("privacy_class") or meta.get("privacy_class") or "internal"
    consent = ev_row.get("consent_mode") or meta.get("consent_mode")
    value = mapped["value"]
    c_hash = claim_identity_hash(mapped["kind"], subject_ref,
                                 mapped["predicate"], value)

    # Dedup / conflict against open claims on the same subject+predicate.
    for other in node_store.open_claims_for(store, subject_ref,
                                            mapped["predicate"]):
        same = other["canonical_hash"] == c_hash
        if mapped["predicate"] in MULTI_VALUED and not same:
            continue
        if same:
            merged = other["evidence"] + [e for e in evidence if e not in
                                          other["evidence"]]
            node_store.update_claim(
                store, other["id"], evidence=merged,
                confidence=max(float(other["confidence"]),
                               float(cand.get("confidence") or 0.0)))
            _recheck_gates(store, other["id"])
            return {"outcome": "merged", "claim_id": other["id"]}
        # Conflicting single-valued claim: surface both together.
        claim_id = _insert(store, cand, mapped, subject_ref, subject_label,
                           value, c_hash, evidence, privacy, source, consent,
                           now, status="conflicting",
                           reason=f"conflicts with {other['id']}",
                           conflict_with=other["id"])
        try:
            node_store.transition(store, other["id"], "conflicting",
                                  reason=f"conflicts with {claim_id}",
                                  conflict_with=claim_id)
        except node_store.TransitionError:
            pass
        return {"outcome": "conflicting", "claim_id": claim_id,
                "conflict_with": other["id"]}

    claim_id = _insert(store, cand, mapped, subject_ref, subject_label, value,
                       c_hash, evidence, privacy, source, consent, now)
    claim = node_store.get_claim(store, claim_id)
    if (claim and not claim["personal_only"] and claim["proposed_scope"]
            and float(claim["confidence"]) >= auto_propose_min_conf()):
        try:
            from app.services.records import promotion
            promotion.propose(store, claim_id, scope_id=claim["proposed_scope"],
                              target_ref=claim.get("target_hint"),
                              actor="claim_builder")
        except Exception as exc:
            print(f"[claim_builder] auto-propose skipped ({exc}).")
    return {"outcome": "created", "claim_id": claim_id}


def personal_reason(claim: dict[str, Any]) -> str | None:
    """Why a claim must stay personal, or None when it may be proposed."""
    if claim.get("privacy_class") in ("sensitive", "never-send"):
        return f"privacy_class={claim['privacy_class']}"
    ev = claim.get("evidence") or []
    sources = [e.get("source") or "external" for e in ev]
    speakers_owner = bool(ev) and all(_is_self(e.get("speaker") or "")
                                      or (e.get("speaker") or "").lower()
                                      == "owner" for e in ev)
    if sources and all(s == "ambient" for s in sources) and not speakers_owner:
        return "ambient audio where the owner is not the speaker"
    if "screen" in sources and claim.get("kind") not in ("status", "field_update"):
        return "screen evidence proposes status/field_update only"
    if claim.get("consent_mode") == "self_only" and not speakers_owner:
        return "self_only capture may only record the owner's own statements"
    return None


def _insert(store, cand, mapped, subject_ref, subject_label, value, c_hash,
            evidence, privacy, source, consent, now, *, status: str = "draft",
            reason: str | None = None, conflict_with: str | None = None) -> str:
    pol = retention.policy()
    draft = {
        "candidate_id": int(cand["id"]), "kind": mapped["kind"],
        "subject_ref": subject_ref, "subject_label": subject_label,
        "predicate": mapped["predicate"], "value": value,
        "schema_version": claim_schemas.schema_version(mapped["kind"]),
        "canonical_hash": c_hash,
        "confidence": float(cand.get("confidence") or 0.0),
        "evidence": evidence, "privacy_class": privacy,
        "capture_source": source, "consent_mode": consent,
        "status": status, "status_reason": reason, "created_at": now,
        "expires_at": now + float(pol["claim_ttl_days"]) * retention.DAY,
    }
    why = personal_reason(draft)
    draft["personal_only"] = why is not None
    if why and status == "draft":
        draft["status_reason"] = why
    if not draft["personal_only"]:
        scope, hint = _scope_suggestion(subject_label)
        draft["proposed_scope"], draft["target_hint"] = scope, hint
    cid = node_store.insert_claim(store, draft)
    if conflict_with:
        node_store.update_claim(store, cid, conflict_with=conflict_with)
    return cid


def _recheck_gates(store, claim_id: str) -> None:
    claim = node_store.get_claim(store, claim_id)
    if not claim:
        return
    sources = [e.get("source") or "external" for e in claim["evidence"]]
    privacies = [claim["privacy_class"]]
    for e in claim["evidence"]:
        row = _event_row(store, e.get("event_id"))
        if row:
            privacies.append(row.get("privacy_class")
                             or row["meta"].get("privacy_class") or "internal")
    fields: dict[str, Any] = {
        "capture_source": retention.most_restrictive(sources),
        "privacy_class": _strictest(privacies, _PRIVACY_ORDER) or "internal",
    }
    why = personal_reason({**claim, **fields})
    fields["personal_only"] = why is not None
    node_store.update_claim(store, claim_id, **fields)


def run_once(store=None, *, limit: int = BATCH,
             now: float | None = None) -> dict[str, Any]:
    """Build claims for accepted candidates that have none yet."""
    if store is None:
        from app.storage import get_store
        store = get_store()
    with store._lock:
        rows = store._conn.execute(
            "SELECT fc.* FROM fact_candidates fc "
            "LEFT JOIN claims c ON c.candidate_id = fc.id "
            "WHERE fc.status = 'accepted' AND c.id IS NULL "
            "AND fc.kind IN ('commitment', 'task', 'claim') "
            "ORDER BY fc.id LIMIT ?", (int(limit),)).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        try:
            out = build_one(store, dict(r), now=now)
        except Exception as exc:
            print(f"[claim_builder] candidate {r['id']} skipped ({exc}).")
            out = {"outcome": "error"}
        counts[out["outcome"]] = counts.get(out["outcome"], 0) + 1
    expired = expire_stale(store, now=now)
    return {"ok": True, "scanned": len(rows), "outcomes": counts,
            "expired": expired}


def expire_stale(store, *, now: float | None = None) -> int:
    """Claims never promoted expire at claim_ttl_days."""
    now = float(now if now is not None else time.time())
    with store._lock:
        rows = store._conn.execute(
            "SELECT id FROM claims WHERE expires_at IS NOT NULL "
            "AND expires_at < ? AND status IN ('draft', 'proposed', "
            "'conflicting', 'edited')", (now,)).fetchall()
    n = 0
    for r in rows:
        try:
            node_store.transition(store, r["id"], "expired",
                                  reason="claim_ttl")
            for p in node_store.packets_for_claim(store, r["id"]):
                if p["state"] == "open":
                    node_store.update_packet(store, p["id"], state="expired")
            n += 1
        except node_store.TransitionError:
            continue
    return n
