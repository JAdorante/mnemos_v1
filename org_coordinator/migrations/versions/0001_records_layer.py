"""Records layer, Phase 1: org tier tables, RLS, app role, append-only grants.

Revision ID: 0001_records_layer
Revises:
Create Date: 2026-10-02

Explicit SQL rather than metadata.create_all: a migration must keep meaning
the same thing after org_coordinator/repo/schema.py moves on.
"""
from __future__ import annotations

from alembic import op

revision = "0001_records_layer"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "sparrow_app"

DDL = r"""
CREATE TABLE orgs (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL CHECK (org_id = id),
    name            TEXT NOT NULL,
    rung            TEXT NOT NULL DEFAULT 'connected'
                    CHECK (rung IN ('connected', 'sealed', 'perimeter')),
    policy_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    policy_version  INTEGER NOT NULL DEFAULT 1,
    audit_seq       BIGINT NOT NULL DEFAULT 0,
    audit_head      TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
    created_at      DOUBLE PRECISION NOT NULL
);

CREATE TABLE members (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    node_id       TEXT,
    email         TEXT NOT NULL,
    display_name  TEXT,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    status        TEXT NOT NULL CHECK (status IN ('invited', 'active', 'departed')),
    joined_at     DOUBLE PRECISION,
    left_at       DOUBLE PRECISION,
    CONSTRAINT uq_members_org_email UNIQUE (org_id, email)
);

CREATE TABLE invites (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL REFERENCES orgs(id),
    member_id    TEXT NOT NULL REFERENCES members(id),
    secret_hash  TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    created_at   DOUBLE PRECISION NOT NULL,
    expires_at   DOUBLE PRECISION NOT NULL,
    redeemed_at  DOUBLE PRECISION
);

CREATE TABLE node_credentials (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    member_id     TEXT NOT NULL REFERENCES members(id),
    node_id       TEXT NOT NULL,
    secret_hash   TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL,
    last_used_at  DOUBLE PRECISION,
    revoked_at    DOUBLE PRECISION
);

CREATE TABLE scopes (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    kind          TEXT NOT NULL CHECK (kind IN ('org', 'team', 'deal', 'project', 'client')),
    name          TEXT NOT NULL,
    parent_id     TEXT REFERENCES scopes(id),
    external_ref  TEXT,
    created_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX ix_scopes_parent ON scopes (org_id, parent_id);

CREATE TABLE scope_grants (
    org_id      TEXT NOT NULL REFERENCES orgs(id),
    scope_id    TEXT NOT NULL REFERENCES scopes(id),
    member_id   TEXT NOT NULL REFERENCES members(id),
    permission  TEXT NOT NULL CHECK (permission IN ('read', 'propose', 'approve', 'admin')),
    granted_by  TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (scope_id, member_id, permission)
);
CREATE INDEX ix_grants_member ON scope_grants (org_id, member_id);

CREATE TABLE records (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL REFERENCES orgs(id),
    scope_id         TEXT NOT NULL REFERENCES scopes(id),
    subject_ref      TEXT NOT NULL,
    subject_label    TEXT,
    predicate        TEXT NOT NULL,
    record_key       TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL,
    current_version  INTEGER NOT NULL,
    created_at       DOUBLE PRECISION NOT NULL,
    CONSTRAINT uq_records_identity UNIQUE (org_id, scope_id, subject_ref, predicate, record_key)
);

CREATE TABLE record_versions (
    id             TEXT PRIMARY KEY,
    org_id         TEXT NOT NULL REFERENCES orgs(id),
    record_id      TEXT NOT NULL REFERENCES records(id),
    version        INTEGER NOT NULL,
    derived_from   TEXT REFERENCES record_versions(id),
    value_json     JSONB NOT NULL,
    payload_json   TEXT,
    payload_hash   TEXT,
    claim_id       TEXT,
    packet_id      TEXT,
    approved_by    TEXT,
    proposed_by    TEXT,
    approved_via   TEXT,
    valid_from     DOUBLE PRECISION NOT NULL,
    valid_to       DOUBLE PRECISION,
    recorded_at    DOUBLE PRECISION NOT NULL,
    superseded_at  DOUBLE PRECISION
);
CREATE INDEX ix_rv_record ON record_versions (record_id, version);
CREATE UNIQUE INDEX ux_rv_payload_hash ON record_versions (org_id, payload_hash)
    WHERE payload_hash IS NOT NULL;

CREATE TABLE record_evidence (
    id                 BIGSERIAL PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES orgs(id),
    record_version_id  TEXT NOT NULL REFERENCES record_versions(id),
    node_id            TEXT,
    event_ref          BIGINT NOT NULL,
    quote_hash         TEXT NOT NULL,
    quote              TEXT,
    source             TEXT,
    t                  DOUBLE PRECISION,
    evidence_status    TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX ix_re_node_event ON record_evidence (org_id, node_id, event_ref);

CREATE TABLE forwarded_packets (
    packet_id          TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES orgs(id),
    scope_id           TEXT NOT NULL REFERENCES scopes(id),
    payload_json       TEXT NOT NULL,
    payload_hash       TEXT NOT NULL,
    proposed_by        TEXT NOT NULL,
    created_at         DOUBLE PRECISION NOT NULL,
    expires_at         DOUBLE PRECISION NOT NULL,
    state              TEXT NOT NULL DEFAULT 'open',
    record_version_id  TEXT
);

CREATE TABLE holds (
    id             TEXT PRIMARY KEY,
    org_id         TEXT NOT NULL REFERENCES orgs(id),
    name           TEXT NOT NULL,
    criteria_json  JSONB NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     DOUBLE PRECISION NOT NULL,
    released_at    DOUBLE PRECISION
);

CREATE TABLE audit_log (
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    seq           BIGINT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    object_ref    TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    prev_hash     TEXT NOT NULL,
    entry_hash    TEXT NOT NULL,
    at            DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (org_id, seq)
);

CREATE TABLE audit_anchors (
    org_id       TEXT NOT NULL REFERENCES orgs(id),
    day          TEXT NOT NULL,
    seq          BIGINT NOT NULL,
    entry_hash   TEXT NOT NULL,
    location     TEXT NOT NULL,
    anchored_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (org_id, day)
);
"""

TENANT_TABLES = ("orgs", "members", "invites", "node_credentials", "scopes",
                 "scope_grants", "records", "record_versions",
                 "record_evidence", "forwarded_packets", "holds", "audit_log",
                 "audit_anchors")

APP_GRANTS = {
    "orgs": "SELECT, UPDATE (policy_json, policy_version, audit_seq, audit_head, rung, name)",
    "members": "SELECT, INSERT, UPDATE (status, node_id, joined_at, left_at, role, display_name)",
    "invites": "SELECT, INSERT, UPDATE (redeemed_at)",
    "node_credentials": "SELECT, INSERT, UPDATE (last_used_at, revoked_at)",
    "scopes": "SELECT, INSERT, UPDATE (name, parent_id, external_ref, kind)",
    "scope_grants": "SELECT, INSERT, DELETE",
    "records": "SELECT, INSERT, UPDATE (current_version, subject_label)",
    "record_versions": "SELECT, INSERT, UPDATE (superseded_at)",
    "record_evidence": "SELECT, INSERT, UPDATE (evidence_status)",
    "forwarded_packets": "SELECT, INSERT, UPDATE (state, record_version_id)",
    "holds": "SELECT, INSERT, UPDATE (released_at)",
    "audit_log": "SELECT, INSERT",
    "audit_anchors": "SELECT, INSERT",
}


def upgrade() -> None:
    op.execute(f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN;
            END IF;
        END $$;
    """)
    op.execute(DDL)
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # current_setting(.., true) is NULL when unset, and NULL = x is never
        # true: a request that forgot to bind a tenant sees nothing.
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = current_setting('app.org_id', true)) "
            f"WITH CHECK (org_id = current_setting('app.org_id', true))")
        op.execute(f"GRANT {APP_GRANTS[table]} ON {table} TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE record_evidence_id_seq TO {APP_ROLE}")
    op.execute(f"GRANT {APP_ROLE} TO CURRENT_USER")


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
