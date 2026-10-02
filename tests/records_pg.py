"""Shared Postgres fixture for the Org Record Service tests.

Set QUILL_ORG_TEST_DATABASE_URL to a Postgres 16 URL whose user may CREATE
DATABASE (a throwaway container is the intended target):

    docker run -d --name sparrow-orgdb-test -e POSTGRES_PASSWORD=sparrowtest \
        -p 127.0.0.1:55432:5432 postgres:16-alpine
    export QUILL_ORG_TEST_DATABASE_URL=postgresql+psycopg://postgres:sparrowtest@127.0.0.1:55432/postgres

Each test class gets a fresh database, migrated to head, dropped afterwards.
Without the variable (or without SQLAlchemy installed) these tests skip:
they are never run against SQLite.
"""
from __future__ import annotations

import os
import secrets
import unittest

URL_ENV = "QUILL_ORG_TEST_DATABASE_URL"


def available() -> str | None:
    url = (os.environ.get(URL_ENV) or "").strip()
    if not url:
        return None
    try:
        import alembic  # noqa: F401
        import psycopg  # noqa: F401
        import sqlalchemy  # noqa: F401
    except ImportError:
        return None
    return url


class PgTestCase(unittest.TestCase):
    db = None
    db_url = None
    _admin_url = None
    _name = None

    @classmethod
    def setUpClass(cls) -> None:
        url = available()
        if not url:
            raise unittest.SkipTest(f"{URL_ENV} not set (Postgres-only tests)")
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        from org_coordinator.repo import Database, migrate
        cls._admin_url = url
        cls._name = f"sparrow_test_{secrets.token_hex(4)}"
        admin = create_engine(url, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{cls._name}"'))
        admin.dispose()
        cls.db_url = make_url(url).set(database=cls._name) \
            .render_as_string(hide_password=False)
        migrate(cls.db_url)
        cls.db = Database(cls.db_url, pool_size=8)
        cls._jwt = os.environ.get("QUILL_ORG_JWT_SECRET")
        os.environ["QUILL_ORG_JWT_SECRET"] = "t" * 48

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.db is not None:
            cls.db.dispose()
        if cls._jwt is None:
            os.environ.pop("QUILL_ORG_JWT_SECRET", None)
        else:
            os.environ["QUILL_ORG_JWT_SECRET"] = cls._jwt
        if cls._name:
            from sqlalchemy import create_engine, text
            admin = create_engine(cls._admin_url, isolation_level="AUTOCOMMIT")
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{cls._name}" '
                               f'WITH (FORCE)'))
            admin.dispose()


def make_app(db):
    from fastapi import FastAPI

    from org_coordinator.records import api
    api.set_database(db)
    app = FastAPI()
    app.include_router(api.router)
    api.install_error_handler(app)
    return app
