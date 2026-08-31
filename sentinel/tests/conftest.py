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
import pytest_asyncio

REPO = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "database" / "migrations"

for p in ("shared", "ai", "backend", "video-ingestion", "event-processor", "tools"):
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


# ── the ASGI app, wired to the test database ─────────────────────────
# Lives here rather than in one test module because more than one suite
# needs it: the API tests and the security regression suite both drive the
# real app, and a second copy of this fixture would drift from the first.
@pytest_asyncio.fixture
async def api(db, pg_dsn):
    """The real app, wired to the test database."""
    import urllib.parse as up
    from sentinel_core.config import get_settings

    parsed = up.urlparse(pg_dsn)
    os.environ.update({
        "POSTGRES_HOST": parsed.hostname or "127.0.0.1",
        "POSTGRES_PORT": str(parsed.port or 5432),
        "POSTGRES_USER": parsed.username or "postgres",
        "POSTGRES_PASSWORD": parsed.password or "",
        "POSTGRES_DB": (parsed.path or "/sentinel_test").lstrip("/"),
        "SECRET_KEY": "test-secret-key-long-enough-for-hs256-signing-abcdef",
        "BUS_BACKEND": "memory",
        "LOG_LEVEL": "ERROR",
        "ENVIRONMENT": "development",
    })
    get_settings.cache_clear()

    import httpx
    from app.main import app
    from app import db as appdb

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            yield client
    await appdb.close_pool()
    get_settings.cache_clear()


# ── one user per role, and the login helpers ─────────────────────────
# Shared for the same reason as `api` above: the API suite and the
# government-integration suite both need an authenticated caller, and a
# duplicated copy of this would drift.
@pytest.fixture
def users(db):
    """One user per role, sharing a known password."""
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
    from app.security import hash_password

    db.execute("INSERT INTO department (code,name) VALUES ('API','API Test') "
               "ON CONFLICT (code) DO NOTHING")
    pw = "ApiTestPassword2026!"
    created = {}
    for role in ("VIEWER", "OPERATOR", "INVESTIGATOR", "ADMIN"):
        username = f"api_{role.lower()}"
        # Upsert rather than delete-and-recreate: an earlier test may have
        # left a watchlist entry referencing this user, and deleting the row
        # would trip the foreign key. Resetting the password in place also
        # undoes any password change a previous test made.
        db.execute("""INSERT INTO app_user (username, full_name, password_hash,
                          role, department_id)
                      SELECT %s,%s,%s,%s::user_role,d.id FROM department d
                      WHERE d.code='API'
                      ON CONFLICT (username) DO UPDATE
                        SET password_hash = EXCLUDED.password_hash,
                            role          = EXCLUDED.role,
                            is_active     = TRUE,
                            failed_logins = 0,
                            locked_until  = NULL""",
                   (username, f"{role} User", hash_password(pw), role))
        created[role] = username
    return {"password": pw, **created}


async def _token(api, username: str, password: str) -> str:
    r = await api.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _auth(api, users, role="ADMIN") -> dict:
    token = await _token(api, users[role], users["password"])
    return {"Authorization": f"Bearer {token}"}
