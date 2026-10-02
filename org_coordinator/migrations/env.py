"""Alembic environment for the Org Record Service (Postgres only)."""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config


def _url() -> str:
    url = (config.get_main_option("sqlalchemy.url")
           or os.environ.get("QUILL_ORG_DATABASE_URL") or "")
    if not url:
        raise RuntimeError("QUILL_ORG_DATABASE_URL is not set")
    return url


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=None,
                          transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
