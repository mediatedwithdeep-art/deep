"""Async PostgreSQL connection pool.

psycopg3's async pool rather than SQLAlchemy. The API's queries are
spatial, partition-aware and lean on PostGIS functions the ORM would
obscure; the schema is hand-written and hand-indexed, and an ORM layer on
top would add a translation step without removing any work.

The pool is bounded. An unbounded pool under load does not fail fast: it
opens connections until PostgreSQL refuses them, and then everything --
including the ingestion pipeline and the event processor -- stops together.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from sentinel_core.log import get_logger

log = get_logger("sentinel.api.db")

_pool: AsyncConnectionPool | None = None


async def init_pool(dsn: str, min_size: int = 2, max_size: int = 20) -> AsyncConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = AsyncConnectionPool(
        dsn, min_size=min_size, max_size=max_size, open=False,
        kwargs={"row_factory": dict_row, "autocommit": True},
        # Fail a request rather than queue it forever behind an exhausted
        # pool. A hung dashboard tile is far easier to diagnose than a
        # request that never returns.
        timeout=10.0,
        max_idle=300.0,
    )
    await _pool.open(wait=True, timeout=15.0)
    log.info("database pool ready", extra={"min": min_size, "max": max_size})
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    return _pool


async def fetch_all(sql: str, params: Any = None) -> list[dict]:
    async with get_pool().connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(sql: str, params: Any = None) -> dict | None:
    async with get_pool().connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def execute(sql: str, params: Any = None) -> int:
    async with get_pool().connection() as conn:
        cur = await conn.execute(sql, params)
        return cur.rowcount


async def health() -> dict:
    try:
        async with get_pool().connection() as conn:
            cur = await conn.execute("SELECT 1 AS ok")
            await cur.fetchone()
        stats = get_pool().get_stats()
        return {"healthy": True,
                "pool_size": stats.get("pool_size"),
                "pool_available": stats.get("pool_available"),
                "requests_waiting": stats.get("requests_waiting")}
    except Exception as e:
        return {"healthy": False, "error": str(e)}
