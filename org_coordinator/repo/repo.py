"""Repository over one tenant-bound connection. Returns plain dicts.

Every query here also filters on org_id explicitly; RLS is the backstop, not
the filter. Nothing above this module imports SQLAlchemy.
"""
from __future__ import annotations

from typing import Any, Iterable, Iterator

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.engine import Connection

from org_coordinator.repo.schema import (audit_anchors, audit_log,
                                         forwarded_packets, holds, invites,
                                         members, node_credentials, orgs,
                                         record_evidence, record_versions,
                                         records, scope_grants, scopes)


class Conflict(Exception):
    """A unique constraint fired: someone else wrote the same row first."""


def _row(r) -> dict[str, Any] | None:
    return dict(r._mapping) if r is not None else None


def _rows(rs) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in rs]


class Repo:
    def __init__(self, conn: Connection, org_id: str) -> None:
        self.conn = conn
        self.org_id = org_id

    # ---------------------------------------------------------------- orgs --
    def insert_org(self, *, name: str, rung: str, policy: dict,
                   now: float) -> dict:
        self.conn.execute(insert(orgs).values(
            id=self.org_id, org_id=self.org_id, name=name, rung=rung,
            policy_json=policy, created_at=now))
        return self.get_org()

    def get_org(self) -> dict | None:
        return _row(self.conn.execute(
            select(orgs).where(orgs.c.id == self.org_id)).first())

    def set_policy(self, policy: dict, *, rung: str | None = None) -> dict:
        values: dict[str, Any] = {"policy_json": policy,
                                  "policy_version": orgs.c.policy_version + 1}
        if rung:
            values["rung"] = rung
        self.conn.execute(update(orgs).where(orgs.c.id == self.org_id)
                          .values(**values))
        return self.get_org()

    def lock_audit_head(self) -> tuple[int, str]:
        r = self.conn.execute(
            select(orgs.c.audit_seq, orgs.c.audit_head)
            .where(orgs.c.id == self.org_id)
            # FOR NO KEY UPDATE: every insert referencing orgs holds FOR KEY
            # SHARE on this row, which plain FOR UPDATE would deadlock against.
            .with_for_update(key_share=True)).first()
        if r is None:
            raise LookupError("org not visible in this tenant")
        return int(r.audit_seq), str(r.audit_head)

    def append_audit(self, entry: dict) -> None:
        self.conn.execute(insert(audit_log).values(org_id=self.org_id, **entry))
        self.conn.execute(update(orgs).where(orgs.c.id == self.org_id).values(
            audit_seq=entry["seq"], audit_head=entry["entry_hash"]))

    def audit_entries(self, *, after_seq: int = 0, limit: int = 500,
                      action: str | None = None, t0: float | None = None,
                      t1: float | None = None) -> list[dict]:
        q = select(audit_log).where(audit_log.c.org_id == self.org_id,
                                    audit_log.c.seq > after_seq)
        if action:
            q = q.where(audit_log.c.action == action)
        if t0 is not None:
            q = q.where(audit_log.c.at >= t0)
        if t1 is not None:
            q = q.where(audit_log.c.at < t1)
        return _rows(self.conn.execute(q.order_by(audit_log.c.seq)
                                       .limit(limit)))

    def iter_audit(self, batch: int = 5000) -> Iterator[dict]:
        last = 0
        while True:
            rows = self.audit_entries(after_seq=last, limit=batch)
            if not rows:
                return
            yield from rows
            last = int(rows[-1]["seq"])

    def insert_anchor(self, **row: Any) -> None:
        self.conn.execute(insert(audit_anchors).values(org_id=self.org_id, **row))

    def anchors(self) -> list[dict]:
        return _rows(self.conn.execute(
            select(audit_anchors).where(audit_anchors.c.org_id == self.org_id)
            .order_by(audit_anchors.c.day)))

    # ------------------------------------------------------------- members --
    def insert_member(self, **row: Any) -> dict:
        self.conn.execute(insert(members).values(org_id=self.org_id, **row))
        return self.get_member(row["id"])

    def get_member(self, member_id: str) -> dict | None:
        return _row(self.conn.execute(select(members).where(
            members.c.org_id == self.org_id, members.c.id == member_id)).first())

    def member_by_email(self, email: str) -> dict | None:
        return _row(self.conn.execute(select(members).where(
            members.c.org_id == self.org_id,
            func.lower(members.c.email) == email.lower())).first())

    def update_member(self, member_id: str, **values: Any) -> None:
        self.conn.execute(update(members).where(
            members.c.org_id == self.org_id, members.c.id == member_id)
            .values(**values))

    def list_members(self) -> list[dict]:
        return _rows(self.conn.execute(select(members).where(
            members.c.org_id == self.org_id).order_by(members.c.email)))

    # ------------------------------------------------- invites / credentials --
    def insert_invite(self, **row: Any) -> None:
        self.conn.execute(insert(invites).values(org_id=self.org_id, **row))

    def get_invite_for_update(self, invite_id: str) -> dict | None:
        return _row(self.conn.execute(select(invites).where(
            invites.c.org_id == self.org_id, invites.c.id == invite_id)
            .with_for_update()).first())

    def redeem_invite(self, invite_id: str, now: float) -> None:
        self.conn.execute(update(invites).where(
            invites.c.org_id == self.org_id, invites.c.id == invite_id)
            .values(redeemed_at=now))

    def insert_credential(self, **row: Any) -> None:
        self.conn.execute(insert(node_credentials).values(org_id=self.org_id,
                                                          **row))

    def get_credential(self, cred_id: str) -> dict | None:
        return _row(self.conn.execute(select(node_credentials).where(
            node_credentials.c.org_id == self.org_id,
            node_credentials.c.id == cred_id)).first())

    def touch_credential(self, cred_id: str, now: float) -> None:
        self.conn.execute(update(node_credentials).where(
            node_credentials.c.org_id == self.org_id,
            node_credentials.c.id == cred_id).values(last_used_at=now))

    def revoke_credentials(self, member_id: str, now: float) -> int:
        r = self.conn.execute(update(node_credentials).where(
            node_credentials.c.org_id == self.org_id,
            node_credentials.c.member_id == member_id,
            node_credentials.c.revoked_at.is_(None)).values(revoked_at=now))
        return int(r.rowcount or 0)

    # -------------------------------------------------------------- scopes --
    def insert_scope(self, **row: Any) -> dict:
        self.conn.execute(insert(scopes).values(org_id=self.org_id, **row))
        return self.get_scope(row["id"])

    def get_scope(self, scope_id: str) -> dict | None:
        return _row(self.conn.execute(select(scopes).where(
            scopes.c.org_id == self.org_id, scopes.c.id == scope_id)).first())

    def list_scopes(self) -> list[dict]:
        return _rows(self.conn.execute(select(scopes).where(
            scopes.c.org_id == self.org_id).order_by(scopes.c.created_at)))

    def update_scope(self, scope_id: str, **values: Any) -> None:
        self.conn.execute(update(scopes).where(
            scopes.c.org_id == self.org_id, scopes.c.id == scope_id)
            .values(**values))

    def ancestors(self, scope_id: str) -> list[str]:
        """scope_id first, then each parent up to the root."""
        rs = self.conn.execute(text("""
            WITH RECURSIVE up(id, parent_id, depth) AS (
                SELECT id, parent_id, 0 FROM scopes
                 WHERE org_id = :org AND id = :sid
                UNION ALL
                SELECT s.id, s.parent_id, up.depth + 1 FROM scopes s
                  JOIN up ON s.id = up.parent_id
                 WHERE s.org_id = :org AND up.depth < 64
            )
            SELECT id FROM up ORDER BY depth
        """), {"org": self.org_id, "sid": scope_id})
        return [r.id for r in rs]

    # -------------------------------------------------------------- grants --
    def insert_grant(self, **row: Any) -> None:
        self.conn.execute(text("""
            INSERT INTO scope_grants (org_id, scope_id, member_id, permission,
                                      granted_by, created_at)
            VALUES (:org, :scope_id, :member_id, :permission, :granted_by,
                    :created_at)
            ON CONFLICT DO NOTHING
        """), {"org": self.org_id, **row})

    def delete_grant(self, scope_id: str, member_id: str,
                     permission: str) -> int:
        r = self.conn.execute(scope_grants.delete().where(
            scope_grants.c.org_id == self.org_id,
            scope_grants.c.scope_id == scope_id,
            scope_grants.c.member_id == member_id,
            scope_grants.c.permission == permission))
        return int(r.rowcount or 0)

    def delete_member_grants(self, member_id: str) -> int:
        r = self.conn.execute(scope_grants.delete().where(
            scope_grants.c.org_id == self.org_id,
            scope_grants.c.member_id == member_id))
        return int(r.rowcount or 0)

    def grants_for_scope(self, scope_id: str) -> list[dict]:
        return _rows(self.conn.execute(select(scope_grants).where(
            scope_grants.c.org_id == self.org_id,
            scope_grants.c.scope_id == scope_id)))

    def grants_for_member(self, member_id: str) -> list[dict]:
        return _rows(self.conn.execute(select(scope_grants).where(
            scope_grants.c.org_id == self.org_id,
            scope_grants.c.member_id == member_id)))

    def members_with_permission(self, scope_ids: list[str],
                                permissions: Iterable[str]) -> list[str]:
        rs = self.conn.execute(select(scope_grants.c.member_id).distinct()
                               .where(scope_grants.c.org_id == self.org_id,
                                      scope_grants.c.scope_id.in_(scope_ids),
                                      scope_grants.c.permission.in_(
                                          list(permissions))))
        return [r.member_id for r in rs]

    # ------------------------------------------------------------- records --
    def record_for_update(self, scope_id: str, subject_ref: str,
                          predicate: str, record_key: str) -> dict | None:
        return _row(self.conn.execute(select(records).where(
            records.c.org_id == self.org_id, records.c.scope_id == scope_id,
            records.c.subject_ref == subject_ref,
            records.c.predicate == predicate,
            records.c.record_key == record_key).with_for_update()).first())

    def insert_record(self, **row: Any) -> None:
        self.conn.execute(text("""
            INSERT INTO records (id, org_id, scope_id, subject_ref,
                subject_label, predicate, record_key, kind, current_version,
                created_at)
            VALUES (:id, :org, :scope_id, :subject_ref, :subject_label,
                    :predicate, :record_key, :kind, 0, :created_at)
            ON CONFLICT ON CONSTRAINT uq_records_identity DO NOTHING
        """), {"org": self.org_id, **row})

    def set_current_version(self, record_id: str, version: int) -> None:
        self.conn.execute(update(records).where(
            records.c.org_id == self.org_id, records.c.id == record_id)
            .values(current_version=version))

    def get_record(self, record_id: str) -> dict | None:
        return _row(self.conn.execute(select(records).where(
            records.c.org_id == self.org_id,
            records.c.id == record_id)).first())

    def version_by_payload_hash(self, payload_hash: str) -> dict | None:
        return _row(self.conn.execute(select(record_versions).where(
            record_versions.c.org_id == self.org_id,
            record_versions.c.payload_hash == payload_hash)).first())

    def insert_version(self, **row: Any) -> None:
        from sqlalchemy.exc import IntegrityError
        try:
            with self.conn.begin_nested():
                self.conn.execute(insert(record_versions).values(
                    org_id=self.org_id, **row))
        except IntegrityError as exc:
            raise Conflict(str(exc.orig)) from exc

    def believed_versions(self, record_id: str, known_at: float) -> list[dict]:
        rv = record_versions
        return _rows(self.conn.execute(select(rv).where(
            rv.c.org_id == self.org_id, rv.c.record_id == record_id,
            rv.c.recorded_at <= known_at,
            or_(rv.c.superseded_at.is_(None), rv.c.superseded_at > known_at))
            .order_by(rv.c.valid_from)))

    def supersede(self, version_ids: list[str], now: float) -> None:
        if version_ids:
            self.conn.execute(update(record_versions).where(
                record_versions.c.org_id == self.org_id,
                record_versions.c.id.in_(version_ids)).values(superseded_at=now))

    def versions(self, record_id: str) -> list[dict]:
        rv = record_versions
        return _rows(self.conn.execute(select(rv).where(
            rv.c.org_id == self.org_id, rv.c.record_id == record_id)
            .order_by(rv.c.recorded_at, rv.c.version, rv.c.valid_from)))

    def get_version(self, version_id: str) -> dict | None:
        return _row(self.conn.execute(select(record_versions).where(
            record_versions.c.org_id == self.org_id,
            record_versions.c.id == version_id)).first())

    def query_records(self, *, scope_ids: list[str], subject: str | None,
                      predicate: str | None, as_of: float,
                      known_at: float, limit: int = 200) -> list[dict]:
        """Bi-temporal: the version believed at `known_at` whose valid
        interval contains `as_of`, per record, within readable scopes."""
        if not scope_ids:
            return []
        r, rv = records, record_versions
        q = (select(r.c.id.label("record_id"), r.c.scope_id, r.c.subject_ref,
                    r.c.subject_label, r.c.predicate, r.c.record_key, r.c.kind,
                    r.c.current_version, rv.c.id.label("version_id"),
                    rv.c.version, rv.c.value_json, rv.c.valid_from,
                    rv.c.valid_to, rv.c.recorded_at, rv.c.superseded_at,
                    rv.c.approved_by, rv.c.payload_hash)
             .select_from(r.join(rv, rv.c.record_id == r.c.id))
             .where(r.c.org_id == self.org_id, rv.c.org_id == self.org_id,
                    r.c.scope_id.in_(scope_ids),
                    rv.c.recorded_at <= known_at,
                    or_(rv.c.superseded_at.is_(None),
                        rv.c.superseded_at > known_at),
                    rv.c.valid_from <= as_of,
                    or_(rv.c.valid_to.is_(None), rv.c.valid_to > as_of)))
        if subject:
            q = q.where(r.c.subject_ref == subject)
        if predicate:
            q = q.where(r.c.predicate == predicate)
        q = q.order_by(r.c.subject_ref, r.c.predicate, rv.c.version.desc())
        rows = _rows(self.conn.execute(q.limit(limit * 4)))
        seen: set[str] = set()
        out = []
        for row in rows:
            if row["record_id"] in seen:
                continue
            seen.add(row["record_id"])
            out.append(row)
        return out[:limit]

    def insert_evidence(self, rows: list[dict]) -> None:
        if rows:
            self.conn.execute(insert(record_evidence),
                              [{"org_id": self.org_id, **r} for r in rows])

    def evidence_for_version(self, version_id: str) -> list[dict]:
        return _rows(self.conn.execute(select(record_evidence).where(
            record_evidence.c.org_id == self.org_id,
            record_evidence.c.record_version_id == version_id)
            .order_by(record_evidence.c.id)))

    def mark_evidence_expired(self, node_id: str, event_refs: list[int]) -> int:
        r = self.conn.execute(update(record_evidence).where(
            record_evidence.c.org_id == self.org_id,
            record_evidence.c.node_id == node_id,
            record_evidence.c.event_ref.in_(event_refs),
            record_evidence.c.evidence_status != "expired")
            .values(evidence_status="expired"))
        return int(r.rowcount or 0)

    # ----------------------------------------------------------- forwarded --
    def insert_forwarded(self, **row: Any) -> None:
        self.conn.execute(text("""
            INSERT INTO forwarded_packets (packet_id, org_id, scope_id,
                payload_json, payload_hash, proposed_by, created_at, expires_at)
            VALUES (:packet_id, :org, :scope_id, :payload_json, :payload_hash,
                    :proposed_by, :created_at, :expires_at)
            ON CONFLICT (packet_id) DO NOTHING
        """), {"org": self.org_id, **row})

    def get_forwarded(self, packet_id: str) -> dict | None:
        return _row(self.conn.execute(select(forwarded_packets).where(
            forwarded_packets.c.org_id == self.org_id,
            forwarded_packets.c.packet_id == packet_id)).first())

    def list_forwarded(self, scope_ids: list[str], now: float) -> list[dict]:
        if not scope_ids:
            return []
        fp = forwarded_packets
        return _rows(self.conn.execute(select(fp).where(
            fp.c.org_id == self.org_id, fp.c.scope_id.in_(scope_ids),
            fp.c.state == "open", fp.c.expires_at > now)
            .order_by(fp.c.created_at)))

    def forwarded_by(self, member_id: str) -> list[dict]:
        fp = forwarded_packets
        return _rows(self.conn.execute(select(fp).where(
            fp.c.org_id == self.org_id, fp.c.proposed_by == member_id)
            .order_by(fp.c.created_at)))

    def close_forwarded(self, packet_id: str, state: str,
                        version_id: str | None) -> None:
        self.conn.execute(update(forwarded_packets).where(
            forwarded_packets.c.org_id == self.org_id,
            forwarded_packets.c.packet_id == packet_id)
            .values(state=state, record_version_id=version_id))

    # --------------------------------------------------------------- holds --
    def active_holds(self) -> list[dict]:
        return _rows(self.conn.execute(select(holds).where(
            holds.c.org_id == self.org_id, holds.c.released_at.is_(None))))


__all__ = ["Conflict", "Repo"]
