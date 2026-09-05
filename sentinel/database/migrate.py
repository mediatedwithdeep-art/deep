#!/usr/bin/env python3
"""Migration runner.

Deliberately not Alembic. Alembic's autogenerate fights PostGIS geometry
columns, partitioned tables and pgvector indexes -- all three of which this
schema uses heavily -- and the generated migrations need hand-editing
anyway. Plain ordered SQL files with a ledger table are easier to review,
easier to run in a container entrypoint, and impossible to get subtly wrong.

Each file runs once, inside a transaction, and is recorded with the SHA-256
of its contents. Editing an already-applied migration is refused: silent
drift between environments is far more expensive than an explicit error.

Usage:
    python migrate.py status
    python migrate.py up
    python migrate.py up --dsn postgresql://...
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sys
import time

try:
    import psycopg
except ImportError:                                     # pragma: no cover
    print("pip install 'psycopg[binary]'", file=sys.stderr)
    raise

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_MIGRATIONS = HERE / "migrations"

LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms INTEGER NOT NULL
);
"""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def discover(migrations_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(migrations_dir.glob("*.sql"))


def applied(conn) -> dict[str, str]:
    conn.execute(LEDGER)
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {r[0]: r[1] for r in rows}


def apply_all(dsn: str, migrations_dir: pathlib.Path | None = None,
              quiet: bool = False) -> int:
    migrations_dir = migrations_dir or DEFAULT_MIGRATIONS
    files = discover(migrations_dir)
    if not files:
        raise SystemExit(f"no .sql files in {migrations_dir}")

    count = 0
    with psycopg.connect(dsn, autocommit=True) as conn:
        done = applied(conn)
        for f in files:
            version = f.stem
            sql = f.read_text()
            cs = _checksum(sql)
            if version in done:
                if done[version] != cs:
                    raise SystemExit(
                        f"MIGRATION DRIFT: {version} was applied with checksum "
                        f"{done[version]} but the file on disk is {cs}.\n"
                        f"Applied migrations are immutable -- add a new migration "
                        f"instead of editing this one.")
                continue
            if not quiet:
                print(f"  applying {version} ...", end="", flush=True)
            t0 = time.monotonic()
            # autocommit=False for the DDL so a failure leaves nothing behind.
            with psycopg.connect(dsn) as tx:
                tx.execute(sql)
                tx.commit()
            ms = int((time.monotonic() - t0) * 1000)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum, duration_ms) "
                "VALUES (%s, %s, %s)", (version, cs, ms))
            count += 1
            if not quiet:
                print(f" ok ({ms} ms)")
    return count


def status(dsn: str, migrations_dir: pathlib.Path | None = None) -> None:
    migrations_dir = migrations_dir or DEFAULT_MIGRATIONS
    with psycopg.connect(dsn, autocommit=True) as conn:
        done = applied(conn)
    print(f"{'VERSION':<34} {'STATUS':<10} APPLIED AT")
    for f in discover(migrations_dir):
        v = f.stem
        mark = "applied" if v in done else "PENDING"
        print(f"{v:<34} {mark:<10}")
    pending = [f.stem for f in discover(migrations_dir) if f.stem not in done]
    print(f"\n{len(done)} applied, {len(pending)} pending")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel database migrations")
    ap.add_argument("command", choices=["up", "status"])
    ap.add_argument("--dsn", default=os.environ.get(
        "DATABASE_URL", "postgresql://sentinel:sentinel@localhost:5432/sentinel"))
    ap.add_argument("--dir", default=None)
    args = ap.parse_args()
    mdir = pathlib.Path(args.dir) if args.dir else DEFAULT_MIGRATIONS

    if args.command == "status":
        status(args.dsn, mdir)
    else:
        n = apply_all(args.dsn, mdir)
        print(f"{n} migration(s) applied" if n else "already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
