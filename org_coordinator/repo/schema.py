"""Org Record Service tables (SQLAlchemy Core). Postgres 16 only.

Every tenant table carries org_id and is under FORCE ROW LEVEL SECURITY with
a policy on current_setting('app.org_id'). The service runs every request as
the NOLOGIN role `sparrow_app` (SET LOCAL ROLE inside the transaction), so the
policy applies even when the connection user owns the tables. Alembic owns
the DDL — this module is the shape the repo layer queries against, and the
migration is generated from it by hand (see migrations/versions/0001_*).
"""
from __future__ import annotations

from sqlalchemy import (BigInteger, Column, Double, ForeignKey,
                        Index, Integer, MetaData, PrimaryKeyConstraint, Table,
                        Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB

APP_ROLE = "sparrow_app"
metadata = MetaData()

orgs = Table(
    "orgs", metadata,
    Column("id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),          # == id; uniform RLS policy
    Column("name", Text, nullable=False),
    Column("rung", Text, nullable=False, server_default="connected"),
    Column("policy_json", JSONB, nullable=False, server_default="{}"),
    Column("policy_version", Integer, nullable=False, server_default="1"),
    Column("audit_seq", BigInteger, nullable=False, server_default="0"),
    Column("audit_head", Text, nullable=False,
           server_default="0" * 64),
    Column("created_at", Double, nullable=False),
)

members = Table(
    "members", metadata,
    Column("id", Text, primary_key=True),
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("node_id", Text),
    Column("email", Text, nullable=False),
    Column("display_name", Text),
    Column("role", Text, nullable=False),            # admin | member | viewer
    Column("status", Text, nullable=False),          # invited | active | departed
    Column("joined_at", Double),
    Column("left_at", Double),
    UniqueConstraint("org_id", "email", name="uq_members_org_email"),
)

invites = Table(
    "invites", metadata,
    Column("id", Text, primary_key=True),
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("member_id", Text, ForeignKey("members.id"), nullable=False),
    Column("secret_hash", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", Double, nullable=False),
    Column("expires_at", Double, nullable=False),
    Column("redeemed_at", Double),
)

node_credentials = Table(
    "node_credentials", metadata,
    Column("id", Text, primary_key=True),
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("member_id", Text, ForeignKey("members.id"), nullable=False),
    Column("node_id", Text, nullable=False),
    Column("secret_hash", Text, nullable=False),
    Column("created_at", Double, nullable=False),
    Column("last_used_at", Double),
    Column("revoked_at", Double),
)

scopes = Table(
    "scopes", metadata,
    Column("id", Text, primary_key=True),
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("kind", Text, nullable=False),            # org | team | deal | project | client
    Column("name", Text, nullable=False),
    Column("parent_id", Text, ForeignKey("scopes.id")),
    Column("external_ref", Text),
    Column("created_at", Double, nullable=False),
    Index("ix_scopes_parent", "org_id", "parent_id"),
)

scope_grants = Table(
    "scope_grants", metadata,
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("scope_id", Text, ForeignKey("scopes.id"), nullable=False),
    Column("member_id", Text, ForeignKey("members.id"), nullable=False),
    Column("permission", Text, nullable=False),      # read | propose | approve | admin
    Column("granted_by", Text, nullable=False),
    Column("created_at", Double, nullable=False),
    PrimaryKeyConstraint("scope_id", "member_id", "permission"),
)

records = Table(
    "records", metadata,
    Column("id", Text, primary_key=True),
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("scope_id", Text, ForeignKey("scopes.id"), nullable=False),
    Column("subject_ref", Text, nullable=False),
    Column("subject_label", Text),
    Column("predicate", Text, nullable=False),
    Column("record_key", Text, nullable=False, server_default=""),
    Column("kind", Text, nullable=False),
    Column("current_version", Integer, nullable=False),
    Column("created_at", Double, nullable=False),
    UniqueConstraint("org_id", "scope_id", "subject_ref", "predicate",
                     "record_key", name="uq_records_identity"),
)

# Append-only. A version is never rewritten; superseding one stamps its
# superseded_at (column-level UPDATE grant is all the app role has) and, when
# the new value starts later in valid time, inserts a `derived` row that keeps
# believing the old value for the interval before it.
record_versions = Table(
    "record_versions", metadata,
    Column("id", Text, primary_key=True),
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("record_id", Text, ForeignKey("records.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("derived_from", Text, ForeignKey("record_versions.id")),
    Column("value_json", JSONB, nullable=False),
    Column("payload_json", Text),                    # canonical text, re-hashable
    Column("payload_hash", Text),
    Column("claim_id", Text),
    Column("packet_id", Text),
    Column("approved_by", Text),
    Column("proposed_by", Text),
    Column("approved_via", Text),
    Column("valid_from", Double, nullable=False),
    Column("valid_to", Double),
    Column("recorded_at", Double, nullable=False),
    Column("superseded_at", Double),
    Index("ix_rv_record", "record_id", "version"),
    Index("ux_rv_payload_hash", "org_id", "payload_hash", unique=True,
          postgresql_where="payload_hash IS NOT NULL"),
)

record_evidence = Table(
    "record_evidence", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("record_version_id", Text, ForeignKey("record_versions.id"),
           nullable=False),
    Column("node_id", Text),
    Column("event_ref", BigInteger, nullable=False),
    Column("quote_hash", Text, nullable=False),
    Column("quote", Text),
    Column("source", Text),
    Column("t", Double),
    Column("evidence_status", Text, nullable=False, server_default="live"),
    Index("ix_re_node_event", "org_id", "node_id", "event_ref"),
)

forwarded_packets = Table(
    "forwarded_packets", metadata,
    Column("packet_id", Text, primary_key=True),
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("scope_id", Text, ForeignKey("scopes.id"), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("payload_hash", Text, nullable=False),
    Column("proposed_by", Text, nullable=False),
    Column("created_at", Double, nullable=False),
    Column("expires_at", Double, nullable=False),
    Column("state", Text, nullable=False, server_default="open"),
    Column("record_version_id", Text),
)

holds = Table(
    "holds", metadata,
    Column("id", Text, primary_key=True),
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("name", Text, nullable=False),
    Column("criteria_json", JSONB, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", Double, nullable=False),
    Column("released_at", Double),
)

audit_log = Table(
    "audit_log", metadata,
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("actor", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("object_ref", Text, nullable=False),
    Column("payload_hash", Text, nullable=False),
    Column("prev_hash", Text, nullable=False),
    Column("entry_hash", Text, nullable=False),
    Column("at", Double, nullable=False),
    PrimaryKeyConstraint("org_id", "seq"),
)

audit_anchors = Table(
    "audit_anchors", metadata,
    Column("org_id", Text, ForeignKey("orgs.id"), nullable=False),
    Column("day", Text, nullable=False),             # YYYY-MM-DD (UTC)
    Column("seq", BigInteger, nullable=False),
    Column("entry_hash", Text, nullable=False),
    Column("location", Text, nullable=False),
    Column("anchored_at", Double, nullable=False),
    PrimaryKeyConstraint("org_id", "day"),
)
