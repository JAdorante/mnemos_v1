"""Team search: the pointer index.

Revision ID: 0004_pointer_index
Revises: 0003_project_search
Create Date: 2026-11-09

One row per item a member's Sparrow publishes on the Connected rung: owner,
tags, people, type, time and where it lives. The CHECK is the rule that keeps
pointers minimal, enforced where it cannot be skipped: a title, a snippet or
text match keys exist only on a pointer that names a shared project.
"""
from __future__ import annotations

from alembic import op

revision = "0004_pointer_index"
down_revision = "0003_project_search"
branch_labels = None
depends_on = None

APP_ROLE = "sparrow_app"

DDL = r"""
CREATE TABLE pointers (
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    owner_id      TEXT NOT NULL REFERENCES members(id),
    item_ref      TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN
                  ('email', 'meeting', 'message', 'document', 'note', 'item')),
    scope_id      TEXT REFERENCES scopes(id),
    tags          JSONB NOT NULL DEFAULT '[]',
    people        JSONB NOT NULL DEFAULT '[]',
    terms         JSONB NOT NULL DEFAULT '[]',
    text_terms    JSONB NOT NULL DEFAULT '[]',
    item_at       DOUBLE PRECISION NOT NULL,
    source        TEXT NOT NULL,
    title         TEXT,
    snippet       TEXT,
    refs          INTEGER NOT NULL DEFAULT 0,
    published_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (org_id, owner_id, item_ref),
    CONSTRAINT ck_pointers_text_only_when_shared CHECK (
        scope_id IS NOT NULL OR (title IS NULL AND snippet IS NULL
                                 AND text_terms = '[]'::jsonb))
);
CREATE INDEX ix_pointers_scope ON pointers (org_id, scope_id);
CREATE INDEX ix_pointers_terms ON pointers USING GIN (terms);
CREATE INDEX ix_pointers_text_terms ON pointers USING GIN (text_terms);
"""


def upgrade() -> None:
    op.execute(DDL)
    op.execute("ALTER TABLE pointers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE pointers FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON pointers "
        "USING (org_id = current_setting('app.org_id', true)) "
        "WITH CHECK (org_id = current_setting('app.org_id', true))")
    op.execute(
        "GRANT SELECT, INSERT, DELETE, UPDATE (node_id, kind, scope_id, tags, "
        "people, terms, text_terms, item_at, source, title, snippet, refs, "
        f"published_at) ON pointers TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pointers")
