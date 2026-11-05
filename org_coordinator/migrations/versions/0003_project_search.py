"""Records layer: full-text search over record values.

Revision ID: 0003_project_search
Revises: 0002_write_back
Create Date: 2026-11-05

Teammates now search shared project notes directly. The index covers only
the string values of a version, the same expression repo.search_records
queries, so a question never matches a JSON key.
"""
from __future__ import annotations

from alembic import op

revision = "0003_project_search"
down_revision = "0002_write_back"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_record_versions_search ON record_versions USING GIN "
        "(jsonb_to_tsvector('english'::regconfig, value_json, "
        "'[\"string\"]'::jsonb))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_record_versions_search")
