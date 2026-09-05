#!/usr/bin/env python3
"""Inspect, unlock and reset Sentinel accounts from the database side.

Why this exists. The seed prints a generated demo password exactly once,
into the bootstrap container's log:

    Demo login for all accounts:  Sentinel-xxxxxxxxxxxx

Miss that line and there is no way back in. Five wrong guesses then trip the
login lockout in backend/app/routers/auth.py -- five failures, fifteen
minutes -- and the account is shut until the clock runs out. The lockout is
correct and stays; what was missing is the operator's side of it. Every
system with a lockout needs a way for whoever holds the database to look at
the state and clear it.

Database access IS the authorisation boundary here: anyone who can run this
can already read every row. It adds no privilege, it just stops people
reaching for `DELETE FROM app_user` or a hand-written UPDATE with a hash
pasted from somewhere.

Run it against the compose stack without rebuilding anything:

    docker compose run --rm --no-deps -v "${PWD}/scripts:/scripts:ro" \
        api python /scripts/admin_unlock.py --status

    ... --unlock admin
    ... --set-password admin

On a host that has run `pip install -e .`, just:

    python scripts/admin_unlock.py --status

Every change is written to audit_log, because an out-of-band password reset
on an evidence system that leaves no trace is exactly the thing an auditor
is looking for.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime, timezone

# The hashing is imported, never reimplemented. A second copy of
# hash_password that drifts from the one verify_password uses would write
# hashes that look right and never authenticate -- and the only symptom
# would be "wrong password" on a password you just set. seed.py already
# keeps a hand-copy of this; one is enough.
try:
    from app.security import PBKDF2_ITERATIONS, hash_password, password_problems
except ImportError:
    sys.exit(
        "Cannot import app.security, so this script cannot hash a password "
        "the way login verifies it.\n"
        "Run it inside the api image, which already has the code:\n\n"
        '  docker compose run --rm --no-deps -v "${PWD}/scripts:/scripts:ro" \\\n'
        "      api python /scripts/admin_unlock.py --status\n\n"
        "or, on a host, run `pip install -e .` from the sentinel directory "
        "first.")

try:
    import psycopg
except ImportError:
    sys.exit("psycopg is not installed. Use the docker compose run form "
             "shown in this file's header.")

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def dsn_from_env(explicit: str | None) -> str:
    """Same environment the services read, so this needs no separate config."""
    if explicit:
        return explicit
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    user = os.environ.get("POSTGRES_USER", "sentinel")
    pw = os.environ.get("POSTGRES_PASSWORD", "sentinel-demo-password")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "sentinel")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def audit(cur, action: str, username: str, detail: dict) -> None:
    # audit_log is RANGE-partitioned by timestamp, and a row with no
    # partition to land in is rejected outright. The API creates the current
    # month's partition at startup and the seed does the same, but this tool
    # must not assume either has run recently -- on a stack that has sat idle
    # past the lookahead window, the partition can simply be absent. The
    # function is idempotent (IF NOT EXISTS per partition), so calling it
    # here costs one cheap query and removes the assumption.
    cur.execute("SELECT count(*) FROM ensure_partitions()")
    cur.execute(
        "INSERT INTO audit_log (username, action, resource, resource_id, "
        "                       reason, result, detail) "
        "VALUES (%s,%s,%s,%s,%s,'SUCCESS',%s)",
        ("admin_unlock.py", action, "/admin/account", username,
         "out-of-band administrative action", psycopg.types.json.Jsonb(detail)))


def show_status(cur) -> int:
    cur.execute(
        "SELECT u.username, u.full_name, u.role::text AS role, u.is_active, "
        "       u.failed_logins, u.locked_until, u.last_login_at, d.code AS dept "
        "FROM app_user u LEFT JOIN department d ON d.id = u.department_id "
        "ORDER BY u.username")
    rows = cur.fetchall()
    if not rows:
        print(f"{RED}No accounts exist. The database was never seeded.{OFF}")
        print("  docker compose up -d bootstrap")
        return 1

    now = datetime.now(timezone.utc)
    # INVESTIGATOR is 12 characters; a narrower column silently shunts
    # every later field right and the table stops lining up.
    print(f"\n{BOLD}{'USER':<14}{'ROLE':<14}{'DEPT':<9}{'FAILED':<8}STATE{OFF}")
    print("─" * 66)
    locked = 0
    for r in rows:
        (username, _full, role, active, failed, until, last, dept) = r
        if until and until > now:
            mins = (until - now).total_seconds() / 60
            state = f"{RED}LOCKED — {mins:.0f} min left{OFF}"
            locked += 1
        elif not active:
            state = f"{YELLOW}disabled{OFF}"
        else:
            state = (f"{GREEN}ok{OFF}" +
                     (f"{DIM} (last login {last:%Y-%m-%d %H:%M}){OFF}" if last
                      else f"{DIM} (never logged in){OFF}"))
        print(f"{username:<14}{role:<14}{(dept or '-'):<9}"
              f"{failed or 0:<8}{state}")
    print()
    if locked:
        print(f"{YELLOW}{locked} account(s) locked.{OFF} Clear with "
              f"--unlock <username>, or --unlock-all.")
        print(f"{DIM}The lock also expires on its own; the countdown above is "
              f"how long that takes.{OFF}\n")
    return 0


def unlock(cur, username: str | None, everyone: bool) -> int:
    if everyone:
        cur.execute("UPDATE app_user SET failed_logins=0, locked_until=NULL "
                    "WHERE locked_until IS NOT NULL OR failed_logins > 0 "
                    "RETURNING username")
    else:
        cur.execute("UPDATE app_user SET failed_logins=0, locked_until=NULL "
                    "WHERE username=%s RETURNING username", (username,))
    freed = [r[0] for r in cur.fetchall()]
    if not freed:
        if everyone:
            print(f"{GREEN}Nothing was locked.{OFF}")
            return 0
        print(f"{RED}No account named {username!r}.{OFF} "
              f"Run --status to list them.")
        return 1
    for name in freed:
        audit(cur, "ACCOUNT_UNLOCK", name, {"cleared": ["failed_logins",
                                                        "locked_until"]})
    print(f"{GREEN}Unlocked:{OFF} {', '.join(freed)}")
    return 0


def set_password(cur, username: str, password: str | None) -> int:
    cur.execute("SELECT 1 FROM app_user WHERE username=%s", (username,))
    if cur.fetchone() is None:
        print(f"{RED}No account named {username!r}.{OFF}")
        return 1

    if password is None:
        password = getpass.getpass(f"New password for {username}: ")
        if password != getpass.getpass("Repeat: "):
            print(f"{RED}They do not match. Nothing changed.{OFF}")
            return 1

    # Check against the API's own policy, so this cannot set a password the
    # change-password endpoint would later reject.
    problems = password_problems(password)
    if problems:
        print(f"{RED}That password does not meet the policy:{OFF}")
        for p in problems:
            print(f"  - it {p}")
        return 1

    cur.execute(
        "UPDATE app_user SET password_hash=%s, failed_logins=0, "
        "locked_until=NULL WHERE username=%s",
        (hash_password(password), username))
    # The password itself is never logged, here or in the audit row.
    audit(cur, "PASSWORD_RESET", username,
          {"iterations": PBKDF2_ITERATIONS, "also_unlocked": True})
    print(f"{GREEN}Password set for {username}, and the account unlocked.{OFF}")
    print(f"{DIM}Existing sessions keep working until their tokens expire; "
          f"revoke them by deleting that user's refresh_token rows.{OFF}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Inspect, unlock and reset Sentinel accounts.",
        epilog="With no action, prints the status of every account.")
    ap.add_argument("--status", action="store_true",
                    help="show every account and its lock state (default)")
    ap.add_argument("--unlock", metavar="USERNAME",
                    help="clear the lock and failure count for one account")
    ap.add_argument("--unlock-all", action="store_true",
                    help="clear every lock")
    ap.add_argument("--set-password", metavar="USERNAME",
                    help="set a new password (prompts) and unlock")
    ap.add_argument("--password", metavar="VALUE",
                    help="supply the password instead of being prompted; it "
                         "will be visible in your shell history")
    ap.add_argument("--dsn", help="override the database connection string")
    args = ap.parse_args()

    try:
        with psycopg.connect(dsn_from_env(args.dsn), autocommit=False,
                             connect_timeout=10) as conn:
            with conn.cursor() as cur:
                if args.set_password:
                    rc = set_password(cur, args.set_password, args.password)
                elif args.unlock or args.unlock_all:
                    rc = unlock(cur, args.unlock, args.unlock_all)
                else:
                    rc = show_status(cur)
                # Commit only a clean run, so a rejected password or a
                # missing account leaves no audit row claiming otherwise.
                if rc == 0:
                    conn.commit()
                else:
                    conn.rollback()
                if rc == 0 and (args.unlock or args.unlock_all
                                or args.set_password):
                    print()
                    show_status(cur)
                return rc
    except psycopg.OperationalError as e:
        print(f"{RED}Cannot reach the database.{OFF}\n{e}")
        print(f"{DIM}Is sentinel-postgres running?  docker compose ps{OFF}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
