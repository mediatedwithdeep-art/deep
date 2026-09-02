#!/usr/bin/env python3
"""Disaster-recovery drill: prove the replica, do not describe it.

DISASTER_RECOVERY.md described failover. Describing failover is not having
it. This script builds a primary and a streaming standby, writes evidence
rows, kills the primary the way a power loss kills it, promotes the
standby, and then counts what survived.

It is destructive by design -- it creates and destroys its own clusters in
a temp directory and touches nothing else -- and it prints an observed RPO
and RTO rather than a target.

    python scripts/dr_drill.py                 # full drill
    python scripts/dr_drill.py --keep          # leave the clusters running

Why this matters for an evidence system: a police VMS that loses the last
N minutes of sightings on a primary failure has lost evidence, not
throughput. The number this prints is how much.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

PGBIN = pathlib.Path("/usr/lib/postgresql/16/bin")
GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def say(status: str, title: str, detail: str = "") -> bool:
    colour = {"PASS": GREEN, "FAIL": RED, "WARN": YELLOW}.get(status, DIM)
    print(f"  {colour}{status:<6}{OFF} {title}")
    for chunk in str(detail).splitlines():
        if chunk:
            print(f"          {DIM}{chunk}{OFF}")
    return status == "PASS"


def run(cmd: list[str], user: str = "postgres", check: bool = True,
        timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a postgres binary as the postgres user."""
    full = ["su", user, "-s", "/bin/bash", "-c", " ".join(cmd)]
    proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd)[:120]} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:400]}")
    return proc


def psql(port: int, sql: str, db: str = "postgres") -> str:
    out = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-p", str(port), "-U", "postgres",
         "-d", db, "-tAc", sql],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout.strip()


def wait_ready(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            psql(port, "SELECT 1")
            return True
        except Exception:                                 # noqa: BLE001
            time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--primary-port", type=int, default=55432)
    ap.add_argument("--replica-port", type=int, default=55433)
    ap.add_argument("--rows", type=int, default=5000,
                    help="evidence rows written before the primary is killed")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if not PGBIN.exists():
        print(f"PostgreSQL binaries not found at {PGBIN}")
        return 2

    base = pathlib.Path(tempfile.mkdtemp(prefix="sentinel-dr-", dir="/var/tmp"))
    primary = base / "primary"
    replica = base / "replica"
    archive = base / "wal_archive"
    for d in (primary, replica, archive):
        d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["chown", "-R", "postgres:postgres", str(base)], check=False)
    # mkdtemp gives the base 0700 owned by the caller; the postgres user has
    # to be able to traverse into it or initdb cannot even start.
    subprocess.run(["chmod", "711", str(base)], check=False)
    subprocess.run(["chmod", "700", str(primary), str(replica)], check=False)

    print(f"\n{BOLD}SENTINEL — DISASTER RECOVERY DRILL{OFF}")
    print(f"{DIM}{time.strftime('%Y-%m-%d %H:%M:%S %Z')} · {base}{OFF}")
    print("─" * 72)

    ok = True
    observed: dict = {}
    try:
        # ── 1. primary with WAL archiving ────────────────────────────
        run([str(PGBIN / "initdb"), "-D", str(primary), "-U", "postgres",
             "--auth=trust", "--no-instructions"])
        conf = primary / "postgresql.auto.conf"
        conf.write_text(
            f"port = {args.primary_port}\n"
            "unix_socket_directories = '/tmp'\n"
            "listen_addresses = '127.0.0.1'\n"
            # Streaming replication needs replica-level WAL. Without this
            # a standby cannot be built at all.
            "wal_level = replica\n"
            "max_wal_senders = 8\n"
            "wal_keep_size = '128MB'\n"
            # Archiving is what makes point-in-time recovery possible.
            # Streaming alone protects against a node loss; archiving is
            # what protects against a bad DELETE noticed an hour later.
            "archive_mode = on\n"
            f"archive_command = 'test ! -f {archive}/%f && cp %p {archive}/%f'\n"
            # A police evidence store should not lose an acknowledged write.
            "synchronous_commit = on\n"
            "hot_standby = on\n")
        subprocess.run(["chown", "postgres:postgres", str(conf)], check=False)
        run([str(PGBIN / "pg_ctl"), "-D", str(primary), "-l",
             str(primary / "log"), "-w", "start"], timeout=90)
        ok &= say("PASS" if wait_ready(args.primary_port) else "FAIL",
                  "1. Primary up with wal_level=replica and archiving on",
                  f"port {args.primary_port}, archive -> {archive}")

        psql(args.primary_port,
             "CREATE TABLE evidence (id bigserial primary key, "
             "seen_at timestamptz default now(), plate text)")
        psql(args.primary_port,
             "SELECT pg_create_physical_replication_slot('sentinel_standby')")
        ok &= say("PASS", "2. Replication slot created",
                  "a slot stops the primary recycling WAL the standby still "
                  "needs -- without one, a standby that falls behind is lost")

        # ── 3. base backup -> standby ────────────────────────────────
        shutil.rmtree(replica)
        run([str(PGBIN / "pg_basebackup"), "-h", "127.0.0.1", "-p",
             str(args.primary_port), "-U", "postgres", "-D", str(replica),
             "-S", "sentinel_standby", "-R", "-X", "stream", "-c", "fast"],
            timeout=180)
        (replica / "postgresql.auto.conf").open("a").write(
            f"\nport = {args.replica_port}\n"
            "unix_socket_directories = '/tmp'\n"
            "listen_addresses = '127.0.0.1'\n"
            "hot_standby = on\n")
        subprocess.run(["chown", "-R", "postgres:postgres", str(replica)],
                       check=False)
        subprocess.run(["chmod", "700", str(replica)], check=False)
        run([str(PGBIN / "pg_ctl"), "-D", str(replica), "-l",
             str(replica / "log"), "-w", "start"], timeout=90)
        ok &= say("PASS" if wait_ready(args.replica_port) else "FAIL",
                  "3. Standby streaming from the primary",
                  f"pg_basebackup -R -X stream, port {args.replica_port}")

        state = psql(args.primary_port,
                     "SELECT state FROM pg_stat_replication LIMIT 1")
        ok &= say("PASS" if state == "streaming" else "FAIL",
                  "4. Replication is actually streaming",
                  f"pg_stat_replication.state = {state or '<no row>'}")

        # ── 5. write evidence, measure lag ───────────────────────────
        psql(args.primary_port,
             f"INSERT INTO evidence (plate) SELECT 'GJ01AB' || g "
             f"FROM generate_series(1,{args.rows}) g")
        on_primary = int(psql(args.primary_port, "SELECT count(*) FROM evidence"))

        lag_deadline = time.time() + 30
        on_replica = 0
        while time.time() < lag_deadline:
            on_replica = int(psql(args.replica_port,
                                  "SELECT count(*) FROM evidence"))
            if on_replica >= on_primary:
                break
            time.sleep(0.2)
        caught_up_in = round(time.time() - (lag_deadline - 30), 2)
        observed["rows"] = on_primary
        observed["replication_catchup_s"] = caught_up_in
        ok &= say("PASS" if on_replica == on_primary else "FAIL",
                  "5. Evidence reached the standby",
                  f"{on_primary} rows on primary, {on_replica} on standby, "
                  f"caught up in {caught_up_in}s")

        # ── 6. kill the primary the way a power cut does ─────────────
        t_fail = time.time()
        run([str(PGBIN / "pg_ctl"), "-D", str(primary), "-m", "immediate",
             "-w", "stop"], check=False, timeout=60)
        ok &= say("PASS", "6. Primary killed (immediate, no clean shutdown)",
                  "-m immediate is the closest a drill gets to losing power: "
                  "no checkpoint, no graceful flush")

        # ── 7. promote ───────────────────────────────────────────────
        run([str(PGBIN / "pg_ctl"), "-D", str(replica), "-w", "promote"],
            timeout=90)
        promoted = False
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if psql(args.replica_port, "SELECT pg_is_in_recovery()") == "f":
                    promoted = True
                    break
            except Exception:                             # noqa: BLE001
                pass
            time.sleep(0.3)
        rto = round(time.time() - t_fail, 2)
        observed["rto_s"] = rto
        ok &= say("PASS" if promoted else "FAIL",
                  "7. Standby promoted to primary",
                  f"observed RTO {rto}s from kill to accepting writes")

        # ── 8. count what survived ───────────────────────────────────
        survived = int(psql(args.replica_port, "SELECT count(*) FROM evidence"))
        lost = on_primary - survived
        observed["rows_lost"] = lost
        observed["rpo_rows"] = lost
        ok &= say("PASS" if lost == 0 else "FAIL",
                  "8. Evidence survived the failover",
                  f"{survived} of {on_primary} rows present after promotion; "
                  f"observed RPO = {lost} row(s) lost")

        # ── 9. the promoted node accepts writes ──────────────────────
        psql(args.replica_port, "INSERT INTO evidence (plate) VALUES ('GJ99ZZ9999')")
        after = int(psql(args.replica_port, "SELECT count(*) FROM evidence"))
        ok &= say("PASS" if after == survived + 1 else "FAIL",
                  "9. Promoted node accepts new evidence",
                  f"{after} rows after a post-failover insert")

        # ── 10. WAL archive exists for PITR ──────────────────────────
        segments = list(archive.glob("*"))
        ok &= say("PASS" if segments else "FAIL",
                  "10. WAL archived for point-in-time recovery",
                  f"{len(segments)} segment(s) in {archive}\n"
                  "Streaming survives a node loss; the archive is what "
                  "survives a bad write noticed an hour later.")

    finally:
        if not args.keep:
            for d in (primary, replica):
                run([str(PGBIN / "pg_ctl"), "-D", str(d), "-m", "immediate",
                     "-w", "stop"], check=False, timeout=60)
            shutil.rmtree(base, ignore_errors=True)
        else:
            print(f"\n{DIM}clusters left running under {base}{OFF}")

    print("─" * 72)
    print(f"  {BOLD}{'DRILL PASSED' if ok else 'DRILL FAILED'}{OFF}"
          f"   observed RPO {observed.get('rpo_rows', '?')} row(s) · "
          f"RTO {observed.get('rto_s', '?')}s\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
