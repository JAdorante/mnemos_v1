"""Node-side data access for the records layer (claims, promotion packets,
the node audit log, the outbox). DDL lives in Store._migrate_records.

Every function takes the store explicitly; nothing caches one, so tests can
hand in a temp store without touching the process singleton.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.perception.schemas import new_ulid
from app.services.records.canonical import (GENESIS_HASH, audit_entry_hash,
                                             canonical_hash)

# Claim states (spec, "States"). Terminal states never transition again,
# except failed -> approved for a retry.
CLAIM_STATES = ("draft", "proposed", "conflicting", "edited", "approved",
                "recorded", "rejected", "discarded", "expired", "failed")
OPEN_CLAIM_STATES = ("draft", "proposed", "conflicting", "edited")
_TRANSITIONS = {
    "draft": {"proposed", "discarded", "expired", "conflicting"},
    "proposed": {"approved", "edited", "rejected", "expired", "conflicting",
                 "draft"},
    "conflicting": {"proposed", "rejected", "discarded", "expired"},
    "edited": {"approved", "rejected", "expired"},
    "approved": {"recorded", "failed"},
    "failed": {"approved"},
    "recorded": set(),
    "rejected": set(),
    "discarded": set(),
    "expired": set(),
}


class TransitionError(ValueError):
    pass


def _loads(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _claim_row(r) -> dict[str, Any]:
    d = dict(r)
    d["value"] = _loads(d.pop("value_json", None), {})
    d["evidence"] = _loads(d.pop("evidence_json", None), [])
    d["personal_only"] = bool(d.get("personal_only"))
    return d


def _packet_row(r) -> dict[str, Any]:
    d = dict(r)
    d["payload"] = _loads(d.get("payload_json"), {})
    d["include_quote"] = bool(d.get("include_quote"))
    return d


# --------------------------------------------------------------- claims ----
def insert_claim(store, claim: dict[str, Any]) -> str:
    now = float(claim.get("created_at") or time.time())
    cid = claim.get("id") or new_ulid(int(now * 1000))
    with store._lock:
        store._conn.execute(
            """
            INSERT INTO claims
                (id, candidate_id, kind, subject_ref, subject_label, predicate,
                 value_json, schema_version, canonical_hash, confidence, evidence_json,
                 privacy_class, capture_source, consent_mode, personal_only,
                 proposed_scope, target_hint, status, status_reason,
                 created_at, updated_at, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (cid, claim.get("candidate_id"), claim["kind"],
             claim["subject_ref"], claim.get("subject_label"),
             claim["predicate"],
             json.dumps(claim["value"], ensure_ascii=False),
             claim["schema_version"], claim["canonical_hash"],
             float(claim["confidence"]),
             json.dumps(claim["evidence"], ensure_ascii=False),
             claim["privacy_class"], claim["capture_source"],
             claim.get("consent_mode"), int(bool(claim.get("personal_only"))),
             claim.get("proposed_scope"), claim.get("target_hint"),
             claim.get("status") or "draft", claim.get("status_reason"),
             now, now, claim.get("expires_at")))
        store._conn.commit()
    return cid


def get_claim(store, claim_id: str) -> dict[str, Any] | None:
    with store._lock:
        r = store._conn.execute(
            "SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    return _claim_row(r) if r else None


def claim_for_candidate(store, candidate_id: int) -> dict[str, Any] | None:
    with store._lock:
        r = store._conn.execute(
            "SELECT * FROM claims WHERE candidate_id = ?",
            (int(candidate_id),)).fetchone()
    return _claim_row(r) if r else None


def open_claims_for(store, subject_ref: str, predicate: str) -> list[dict]:
    marks = ",".join("?" for _ in OPEN_CLAIM_STATES)
    with store._lock:
        rows = store._conn.execute(
            f"SELECT * FROM claims WHERE subject_ref = ? AND predicate = ? "
            f"AND status IN ({marks}) ORDER BY created_at",
            (subject_ref, predicate, *OPEN_CLAIM_STATES)).fetchall()
    return [_claim_row(r) for r in rows]


def list_claims(store, *, status: str | None = None, scope: str | None = None,
                kind: str | None = None, limit: int = 200) -> list[dict]:
    sql, args = "SELECT * FROM claims WHERE 1=1", []
    if status:
        statuses = [s for s in status.split(",") if s]
        sql += f" AND status IN ({','.join('?' for _ in statuses)})"
        args.extend(statuses)
    if scope:
        sql += " AND proposed_scope = ?"
        args.append(scope)
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY proposed_scope, subject_ref, created_at DESC LIMIT ?"
    args.append(int(limit))
    with store._lock:
        rows = store._conn.execute(sql, args).fetchall()
    return [_claim_row(r) for r in rows]


def update_claim(store, claim_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "value" in fields:
        fields["value_json"] = json.dumps(fields.pop("value"), ensure_ascii=False)
    if "evidence" in fields:
        fields["evidence_json"] = json.dumps(fields.pop("evidence"),
                                             ensure_ascii=False)
    if "personal_only" in fields:
        fields["personal_only"] = int(bool(fields["personal_only"]))
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with store._lock:
        store._conn.execute(f"UPDATE claims SET {cols} WHERE id = ?",
                            (*fields.values(), claim_id))
        store._conn.commit()


def transition(store, claim_id: str, to: str, *, reason: str | None = None,
               **extra: Any) -> dict[str, Any]:
    """Move a claim along the state machine; refuses an illegal edge."""
    claim = get_claim(store, claim_id)
    if claim is None:
        raise TransitionError(f"no claim {claim_id}")
    frm = claim["status"]
    if to != frm and to not in _TRANSITIONS.get(frm, set()):
        raise TransitionError(f"claim {claim_id}: {frm} -> {to} not allowed")
    fields: dict[str, Any] = {"status": to, **extra}
    if reason is not None:
        fields["status_reason"] = reason
    if to in ("approved", "rejected", "discarded", "recorded", "expired"):
        fields.setdefault("decided_at", time.time())
    update_claim(store, claim_id, **fields)
    return {**claim, **fields}


# -------------------------------------------------------------- packets ----
def insert_packet(store, packet: dict[str, Any]) -> str:
    now = float(packet.get("created_at") or time.time())
    pid = packet.get("id") or new_ulid(int(now * 1000))
    with store._lock:
        store._conn.execute(
            """
            INSERT INTO promotion_packets
                (id, claim_id, payload_json, payload_hash, scope_id, target_ref,
                 include_quote, state, expires_at, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (pid, packet["claim_id"], packet["payload_json"],
             packet["payload_hash"], packet["scope_id"],
             packet.get("target_ref"), int(bool(packet.get("include_quote"))),
             packet.get("state") or "open", float(packet["expires_at"]), now))
        store._conn.commit()
    return pid


def get_packet(store, packet_id: str) -> dict[str, Any] | None:
    with store._lock:
        r = store._conn.execute(
            "SELECT * FROM promotion_packets WHERE id = ?",
            (packet_id,)).fetchone()
    return _packet_row(r) if r else None


def packets_for_claim(store, claim_id: str) -> list[dict]:
    with store._lock:
        rows = store._conn.execute(
            "SELECT * FROM promotion_packets WHERE claim_id = ? "
            "ORDER BY created_at", (claim_id,)).fetchall()
    return [_packet_row(r) for r in rows]


def open_packet_for_claim(store, claim_id: str) -> dict | None:
    rows = [p for p in packets_for_claim(store, claim_id)
            if p["state"] == "open"]
    return rows[-1] if rows else None


def update_packet(store, packet_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with store._lock:
        store._conn.execute(
            f"UPDATE promotion_packets SET {cols} WHERE id = ?",
            (*fields.values(), packet_id))
        store._conn.commit()


# ------------------------------------------------------------ audit log ----
def audit(store, actor: str, action: str, object_ref: str,
          payload: dict[str, Any] | None = None, *,
          at: float | None = None) -> dict[str, Any]:
    """Append one metadata-only entry. Callers never put content in payload."""
    at = float(at if at is not None else time.time())
    payload = payload or {}
    p_hash = canonical_hash(payload)
    with store._lock:
        last = store._conn.execute(
            "SELECT seq, entry_hash FROM node_audit_log "
            "ORDER BY seq DESC LIMIT 1").fetchone()
        seq = (int(last["seq"]) + 1) if last else 1
        prev = last["entry_hash"] if last else GENESIS_HASH
        e_hash = audit_entry_hash(prev, seq, actor, action, object_ref, p_hash, at)
        store._conn.execute(
            "INSERT INTO node_audit_log (seq, actor, action, object_ref, "
            "payload_hash, payload_json, prev_hash, entry_hash, at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, actor, action, object_ref, p_hash,
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             prev, e_hash, at))
        store._conn.commit()
    return {"seq": seq, "entry_hash": e_hash, "prev_hash": prev,
            "payload_hash": p_hash, "at": at}


def audit_entries(store, *, action: str | None = None,
                  limit: int = 100) -> list[dict]:
    sql, args = "SELECT * FROM node_audit_log", []
    if action:
        sql += " WHERE action = ?"
        args.append(action)
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(int(limit))
    with store._lock:
        rows = store._conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = _loads(d.pop("payload_json", None), {})
        out.append(d)
    return out


def verify_audit_chain(store) -> dict[str, Any]:
    with store._lock:
        rows = store._conn.execute(
            "SELECT * FROM node_audit_log ORDER BY seq").fetchall()
    prev = GENESIS_HASH
    for i, r in enumerate(rows, start=1):
        if int(r["seq"]) != i or r["prev_hash"] != prev:
            return {"ok": False, "bad_seq": int(r["seq"]), "n": len(rows)}
        want = audit_entry_hash(prev, i, r["actor"], r["action"], r["object_ref"],
                          r["payload_hash"], r["at"])
        if want != r["entry_hash"]:
            return {"ok": False, "bad_seq": i, "n": len(rows)}
        payload = _loads(r["payload_json"], None)
        if payload is not None and canonical_hash(payload) != r["payload_hash"]:
            return {"ok": False, "bad_seq": i, "n": len(rows)}
        prev = r["entry_hash"]
    return {"ok": True, "n": len(rows), "head": prev}


# --------------------------------------------------------------- outbox ----
def enqueue(store, kind: str, ref: str, body: dict[str, Any], *,
            now: float | None = None) -> int:
    now = float(now if now is not None else time.time())
    with store._lock:
        existing = store._conn.execute(
            "SELECT id FROM records_outbox WHERE kind = ? AND ref = ? "
            "AND done_at IS NULL", (kind, ref)).fetchone()
        if existing:
            return int(existing["id"])
        cur = store._conn.execute(
            "INSERT INTO records_outbox (kind, ref, body_json, next_at, "
            "created_at) VALUES (?,?,?,?,?)",
            (kind, ref, json.dumps(body, ensure_ascii=False), now, now))
        store._conn.commit()
        return int(cur.lastrowid)


def due_outbox(store, *, now: float | None = None, limit: int = 50) -> list[dict]:
    now = float(now if now is not None else time.time())
    with store._lock:
        rows = store._conn.execute(
            "SELECT * FROM records_outbox WHERE done_at IS NULL "
            "AND next_at <= ? ORDER BY id LIMIT ?", (now, int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["body"] = _loads(d.pop("body_json", None), {})
        out.append(d)
    return out


def outbox_done(store, row_id: int) -> None:
    with store._lock:
        store._conn.execute(
            "UPDATE records_outbox SET done_at = ? WHERE id = ?",
            (time.time(), int(row_id)))
        store._conn.commit()


def outbox_retry(store, row_id: int, error: str, *, delay_s: float) -> None:
    with store._lock:
        store._conn.execute(
            "UPDATE records_outbox SET attempts = attempts + 1, last_error = ?, "
            "next_at = ? WHERE id = ?",
            (error[:500], time.time() + delay_s, int(row_id)))
        store._conn.commit()
