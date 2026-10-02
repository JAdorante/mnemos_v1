"""Engine, tenant-bound transactions, and migrations.

`Database.tenant(org_id)` is the only way business logic touches the
database: it opens a transaction, drops to the NOLOGIN app role (so row-level
security applies even to a table-owning connection user) and binds
app.org_id for the transaction. Everything inside sees one org.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from org_coordinator.repo.schema import APP_ROLE

if TYPE_CHECKING:
    from org_coordinator.repo.repo import Repo

_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def database_url() -> str:
    return (os.environ.get("QUILL_ORG_DATABASE_URL") or "").strip()


def migrate(url: str | None = None, revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url or database_url())
    command.upgrade(cfg, revision)


class Database:
    def __init__(self, url: str | None = None, *, pool_size: int = 10) -> None:
        url = url or database_url()
        if not url:
            raise RuntimeError("QUILL_ORG_DATABASE_URL is not set")
        self.engine: Engine = create_engine(url, pool_size=pool_size,
                                            max_overflow=pool_size,
                                            pool_pre_ping=True, future=True)

    @contextmanager
    def tenant(self, org_id: str) -> Iterator["Repo"]:
        from org_coordinator.repo.repo import Repo
        if not org_id:
            raise ValueError("tenant() needs an org id")
        with self.engine.begin() as conn:
            conn.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
            conn.execute(text("SELECT set_config('app.org_id', :o, true)"),
                         {"o": org_id})
            yield Repo(conn, org_id)

    @contextmanager
    def owner(self, org_id: str) -> Iterator["Repo"]:
        """Owner connection (no SET ROLE) still bound to one tenant — only for
        org bootstrap, where the app role has no INSERT on orgs."""
        from org_coordinator.repo.repo import Repo
        with self.engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.org_id', :o, true)"),
                         {"o": org_id})
            yield Repo(conn, org_id)

    def dispose(self) -> None:
        self.engine.dispose()
