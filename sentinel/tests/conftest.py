"""Shared pytest fixtures.

Integration tests need a real PostgreSQL with PostGIS + pgvector. They are
skipped automatically when one is not reachable, so `pytest` always runs
green on a fresh checkout and the full suite runs under `make test`.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "database" / "migrations"

for p in ("shared", "ai", "backend", "video-ingestion", "event-processor"):
    sys.path.insert(0, str(REPO / p))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires a live PostgreSQL")
    config.addinivalue_line("markers", "slow: takes more than a second")


@pytest.fixture(scope="session")
def migrations_dir() -> pathlib.Path:
    return MIGRATIONS


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://postgres@127.0.0.1:5433/sentinel_test")


@pytest.fixture(scope="session")
def db(pg_dsn):
    """A migrated test database, or skip if PostgreSQL is unreachable."""
    psycopg = pytest.importorskip("psycopg")
    import urllib.parse as up

    parsed = up.urlparse(pg_dsn)
    dbname = parsed.path.lstrip("/")
    admin = pg_dsn.replace(f"/{dbname}", "/postgres")

    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
            c.execute(f'CREATE DATABASE "{dbname}"')
    except Exception as e:                      # pragma: no cover
        pytest.skip(f"PostgreSQL unavailable: {e}")

    sys.path.insert(0, str(REPO / "database"))
    from migrate import apply_all
    apply_all(pg_dsn, MIGRATIONS, quiet=True)

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        yield conn
