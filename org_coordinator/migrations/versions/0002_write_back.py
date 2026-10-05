"""Records layer, Phase 2: write-back connectors, sync jobs, drift, roster.

Revision ID: 0002_write_back
Revises: 0001_records_layer
Create Date: 2026-10-02

`sync_queue` is the one table WITHOUT row-level security: the worker has to
find due work across every org before it can bind a tenant. It holds ids and
a due time only — nothing a tenant could learn anything from — and the
worker binds app.org_id from the row before touching any tenant table.
"""
from __future__ import annotations

from alembic import op

revision = "0002_write_back"
down_revision = "0001_records_layer"
branch_labels = None
depends_on = None

APP_ROLE = "sparrow_app"

DDL = r"""
ALTER TABLE members ADD COLUMN peer_url TEXT;
ALTER TABLE records ADD COLUMN drift_status TEXT;

CREATE TABLE connectors (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    kind          TEXT NOT NULL CHECK (kind IN ('hubspot', 'gdrive', 'webhook')),
    name          TEXT NOT NULL,
    config_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    secret_enc    TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_by    TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL
);

CREATE TABLE connector_mappings (
    id             TEXT PRIMARY KEY,
    org_id         TEXT NOT NULL REFERENCES orgs(id),
    connector_id   TEXT NOT NULL REFERENCES connectors(id),
    kind           TEXT NOT NULL,
    predicate      TEXT NOT NULL,
    op             TEXT NOT NULL,
    object_type    TEXT,
    field          TEXT,
    value_path     TEXT NOT NULL DEFAULT 'value',
    transform      TEXT NOT NULL DEFAULT 'identity',
    created_by     TEXT NOT NULL,
    created_at     DOUBLE PRECISION NOT NULL,
    CONSTRAINT uq_mapping UNIQUE (connector_id, kind, predicate)
);

CREATE TABLE sync_jobs (
    id                 TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES orgs(id),
    record_version_id  TEXT NOT NULL REFERENCES record_versions(id),
    connector_id       TEXT NOT NULL REFERENCES connectors(id),
    target_ref         TEXT NOT NULL,
    plan_json          JSONB NOT NULL,
    preview_json       JSONB,
    idempotency_key    TEXT NOT NULL,
    state              TEXT NOT NULL CHECK (state IN
                       ('pending', 'writing', 'verified', 'conflict', 'failed')),
    attempts           INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    external_version   TEXT,
    written_json       JSONB,
    proposed_by        TEXT,
    created_at         DOUBLE PRECISION NOT NULL,
    updated_at         DOUBLE PRECISION NOT NULL,
    verified_at        DOUBLE PRECISION,
    CONSTRAINT uq_sync_once UNIQUE (record_version_id, connector_id)
);
CREATE INDEX ix_sync_state ON sync_jobs (org_id, state);
CREATE INDEX ix_sync_verified ON sync_jobs (org_id, verified_at) WHERE state = 'verified';

CREATE TABLE sync_queue (
    job_id   TEXT PRIMARY KEY,
    org_id   TEXT NOT NULL,
    next_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX ix_sync_queue_due ON sync_queue (next_at);

-- Ids only, no RLS: lets the worker enumerate orgs for the nightly drift
-- sweep before it can bind a tenant. Filled at org bootstrap.
CREATE TABLE org_index (
    org_id  TEXT PRIMARY KEY
);
INSERT INTO org_index (org_id) SELECT id FROM orgs ON CONFLICT DO NOTHING;

CREATE TABLE drift_notices (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES orgs(id),
    record_id         TEXT NOT NULL REFERENCES records(id),
    sync_job_id       TEXT NOT NULL REFERENCES sync_jobs(id),
    scope_id          TEXT NOT NULL REFERENCES scopes(id),
    field             TEXT NOT NULL,
    written_value     JSONB,
    external_value    JSONB,
    detected_at       DOUBLE PRECISION NOT NULL,
    resolved_at       DOUBLE PRECISION,
    CONSTRAINT uq_drift_open UNIQUE (sync_job_id, field)
);

CREATE TABLE alerts (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id),
    kind        TEXT NOT NULL,
    object_ref  TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    acked_at    DOUBLE PRECISION
);
"""

TENANT_TABLES = ("connectors", "connector_mappings", "sync_jobs",
                 "drift_notices", "alerts")

APP_GRANTS = {
    "connectors": "SELECT, INSERT, UPDATE (name, config_json, secret_enc, status)",
    "connector_mappings": "SELECT, INSERT, DELETE",
    "sync_jobs": "SELECT, INSERT, UPDATE (state, attempts, last_error, external_version, written_json, updated_at, verified_at, preview_json)",
    "drift_notices": "SELECT, INSERT, UPDATE (resolved_at)",
    "alerts": "SELECT, INSERT, UPDATE (acked_at)",
}


def upgrade() -> None:
    op.execute(DDL)
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = current_setting('app.org_id', true)) "
            f"WITH CHECK (org_id = current_setting('app.org_id', true))")
        op.execute(f"GRANT {APP_GRANTS[table]} ON {table} TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE (next_at), DELETE ON sync_queue TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON org_index TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (peer_url) ON members TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (drift_status) ON records TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS alerts, drift_notices, org_index, sync_queue, "
               "sync_jobs, connector_mappings, connectors CASCADE")
    op.execute("ALTER TABLE records DROP COLUMN IF EXISTS drift_status")
    op.execute("ALTER TABLE members DROP COLUMN IF EXISTS peer_url")
