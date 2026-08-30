#!/usr/bin/env python3
"""Seed the demo estate: departments, zones, users, 50 cameras, adjacency.

Run once after migrations. Idempotent -- safe to re-run.

The 50 cameras are deliberately heterogeneous, because a system that only
works on clean modern IP cameras has not solved the problem the challenge
is actually about:

    ~46%  modern IP over RTSP           (readable, some ANPR-capable)
    ~20%  ONVIF-discovered IP cameras
    ~18%  analog cameras on legacy DVRs (low res, no ANPR, EOL firmware)
    ~10%  HLS from municipal/smart-city feeds (high latency)
     ~6%  one department's proprietary VMS behind an RTSP gateway

Cameras are placed at real Ahmedabad junctions and aimed along the road
they watch, so the field-of-view wedges on the map point somewhere sensible
instead of all pointing north.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "shared"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import psycopg
from ahmedabad import JUNCTIONS, RoadGraph, bearing_between

RNG = random.Random(20260907)   # fixed seed: the demo must be reproducible

DEPARTMENTS = [
    ("GP_AHM",  "Ahmedabad City Police",            "POLICE"),
    ("GP_TRAF", "Ahmedabad Traffic Police",         "POLICE"),
    ("AMC",     "Ahmedabad Municipal Corporation",  "MUNICIPAL"),
    ("GSRTC",   "Gujarat State Road Transport Corp","TRANSPORT"),
    ("AUDA",    "Ahmedabad Urban Development Auth", "MUNICIPAL"),
    ("GAD",     "Gujarat Airports Authority",       "AVIATION"),
]

# (protocol, vendor, width, height, fps, signal_class, firmware_risk, share)
CAMERA_PROFILES = [
    ("RTSP",  "Hikvision", 1920, 1080, 25, "IP",   "OK",        0.20),
    ("RTSP",  "Dahua",     1280,  720, 15, "IP",   "OK",        0.16),
    ("RTSP",  "CP Plus",   1280,  720, 12, "IP",   "EOL",       0.10),
    ("ONVIF", "Axis",      1920, 1080, 25, "IP",   "OK",        0.10),
    ("ONVIF", "Uniview",   1280,  720, 15, "IP",   "OK",        0.10),
    ("DVR",   "CP Plus",    704,  576, 12, "CVBS", "EOL",       0.10),
    ("DVR",   "Hikvision",  960,  576, 10, "AHD",  "KNOWN_CVE", 0.08),
    ("HLS",   "Municipal",  854,  480, 10, "IP",   "UNKNOWN",   0.10),
    ("RTSP",  "Milestone", 1920, 1080, 20, "IP",   "OK",        0.06),
]


def weighted_profiles(n: int) -> list[tuple]:
    out: list[tuple] = []
    for p in CAMERA_PROFILES:
        out.extend([p] * max(1, round(p[-1] * n)))
    RNG.shuffle(out)
    while len(out) < n:
        out.append(CAMERA_PROFILES[0])
    return out[:n]


def hash_password(password: str) -> str:
    """PBKDF2-SHA256. Mirrors backend/app/security.py -- see the note there
    on why this rather than bcrypt."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return f"pbkdf2_sha256$260000${salt.hex()}${dk.hex()}"


def seed(dsn: str, camera_count: int = 50, demo_password: str | None = None) -> None:
    graph = RoadGraph.build()
    demo_password = demo_password or os.environ.get("SEED_ADMIN_PASSWORD", "")
    if not demo_password:
        # Generated, printed once, never committed. A fixed default password
        # in a seed script is how demo systems end up in production with
        # admin/admin.
        import secrets
        demo_password = "Sentinel-" + secrets.token_urlsafe(9)
        generated = True
    else:
        generated = False

    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.cursor()

        # ── departments ──────────────────────────────────────────────
        for code, name, kind in DEPARTMENTS:
            cur.execute(
                "INSERT INTO department (code, name, kind) VALUES (%s,%s,%s) "
                "ON CONFLICT (code) DO NOTHING", (code, name, kind))
        dept_ids = dict(cur.execute("SELECT code, id FROM department").fetchall())

        # ── zones, derived from the junction list ────────────────────
        zones = sorted({j.zone for j in JUNCTIONS})
        for z in zones:
            members = [j for j in JUNCTIONS if j.zone == z]
            clat = sum(j.lat for j in members) / len(members)
            clon = sum(j.lon for j in members) / len(members)
            cur.execute(
                "INSERT INTO location (code, name, kind, district, centroid) "
                "VALUES (%s,%s,'ZONE','Ahmedabad', ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography) "
                "ON CONFLICT (code) DO NOTHING",
                (f"ZONE_{z.upper().replace(' ', '_')}", z, clon, clat))

        # A real geofence, so the RESTRICTED_ZONE rule has something to fire
        # on. Drawn around the airport approach.
        cur.execute("""
            INSERT INTO location (code, name, kind, district, restricted, centroid, geom)
            VALUES ('RESTRICTED_AIRPORT','Airport Restricted Perimeter','RESTRICTED','Ahmedabad', TRUE,
                    ST_SetSRID(ST_MakePoint(72.6343, 23.0772),4326)::geography,
                    ST_SetSRID(ST_MakePolygon(ST_GeomFromText(
                      'LINESTRING(72.6270 23.0720, 72.6420 23.0720, 72.6420 23.0820,
                                  72.6270 23.0820, 72.6270 23.0720)')),4326)::geography)
            ON CONFLICT (code) DO NOTHING""")

        # ── users ────────────────────────────────────────────────────
        users = [
            ("admin",     "System Administrator", "ADMIN",        "GP_AHM",  "ADM-0001"),
            ("controller","Control Room Officer", "OPERATOR",     "GP_AHM",  "OPR-1042"),
            ("inspector", "Crime Branch Inspector","INVESTIGATOR","GP_AHM",  "INV-2210"),
            ("traffic",   "Traffic Control Desk", "OPERATOR",     "GP_TRAF", "OPR-3301"),
            ("viewer",    "Read Only Account",    "VIEWER",       "AMC",     None),
        ]
        for username, full_name, role, dept, badge in users:
            cur.execute(
                "INSERT INTO app_user (username, full_name, password_hash, role, department_id, badge_number, email) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING",
                (username, full_name, hash_password(demo_password), role,
                 dept_ids[dept], badge, f"{username}@sentinel.gujarat.gov.in"))

        # ── cameras ──────────────────────────────────────────────────
        profiles = weighted_profiles(camera_count)
        placed = JUNCTIONS[:camera_count]
        loc_ids = dict(cur.execute("SELECT code, id FROM location").fetchall())

        for idx, (junction, prof) in enumerate(zip(placed, profiles), start=1):
            protocol, vendor, w, h, fps, signal, fw_risk, _ = prof
            neigh = graph.neighbours.get(junction.code, [])
            # Aim the camera along the road it watches, not at random.
            heading = bearing_between(junction.code, neigh[0][0]) if neigh else RNG.uniform(0, 360)

            # ANPR capability is a physical property, not a wish: it needs
            # enough pixels on the plate. Only higher-resolution cameras with
            # a narrow field of view qualify, which is ~15% of the estate --
            # matching what a real estate looks like.
            anpr = w >= 1280 and RNG.random() < 0.35
            role = "ANPR" if anpr else "SURVEILLANCE"
            fov = 32.0 if anpr else RNG.choice([70.0, 82.0, 90.0, 100.0])
            rng_m = 45.0 if anpr else RNG.choice([50.0, 60.0, 75.0])

            dept_code = RNG.choices(
                [d[0] for d in DEPARTMENTS], weights=[45, 20, 15, 8, 7, 5])[0]
            if junction.zone == "Hansol":
                dept_code = "GAD"

            camera_ref = f"AHM-{junction.zone.upper().replace(' ', '')[:6]}-{idx:03d}"
            host = f"10.42.{(idx // 32) + 7}.{(idx % 200) + 20}"

            if protocol == "DVR":
                stream = f"rtsp://{host}:554/cam/realmonitor?channel={(idx % 8) + 1}&subtype=0"
                sub    = f"rtsp://{host}:554/cam/realmonitor?channel={(idx % 8) + 1}&subtype=1"
            elif protocol == "HLS":
                stream = f"https://stream.amc.gov.in/live/{camera_ref}/index.m3u8"
                sub    = stream
            elif protocol == "ONVIF":
                stream = f"rtsp://{host}:554/onvif1"
                sub    = f"rtsp://{host}:554/onvif2"
            else:
                stream = f"rtsp://{host}:554/Streaming/Channels/101"
                sub    = f"rtsp://{host}:554/Streaming/Channels/102"

            zone_key = f"ZONE_{junction.zone.upper().replace(' ', '_')}"
            cur.execute("""
                INSERT INTO camera (camera_id, external_ref, name, department_id, location_id,
                    zone, district, protocol, role, status, latitude, longitude,
                    heading_deg, fov_deg, range_m, stream_url, substream_url,
                    credential_ref, vendor, signal_class, firmware_risk,
                    codec, width, height, fps, gop_size, anpr_capable, tags)
                VALUES (%s,%s,%s,%s,%s,%s,'Ahmedabad',%s,%s,'OFFLINE',%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (camera_id) DO NOTHING""",
                (camera_ref, f"{dept_code}-{idx:04d}", junction.name, dept_ids[dept_code],
                 loc_ids.get(zone_key), junction.zone, protocol, role,
                 junction.lat, junction.lon, round(heading, 1), fov, rng_m,
                 stream, sub,
                 # Never a credential in a URL or a column. This names a
                 # secret in the store; nothing here can be replayed.
                 f"vault://sentinel/cameras/{camera_ref}",
                 vendor, signal, fw_risk, "h264", w, h, fps, fps,
                 anpr, [junction.zone.lower(), protocol.lower()] + (["anpr"] if anpr else [])))

        # ── adjacency, from the road graph ───────────────────────────
        cam_rows = cur.execute(
            "SELECT camera_id, id, latitude, longitude FROM camera").fetchall()
        # Map each camera back to the junction it sits on.
        cam_by_junction: dict[str, str] = {}
        for ref, cid, lat, lon in cam_rows:
            for j in JUNCTIONS:
                if abs(j.lat - lat) < 1e-6 and abs(j.lon - lon) < 1e-6:
                    cam_by_junction[j.code] = cid
                    break

        edges = []
        for jcode, cid in cam_by_junction.items():
            for target, (secs, metres) in graph.shortest_paths(jcode, 900).items():
                tid = cam_by_junction.get(target)
                if tid:
                    edges.append((cid, tid, metres, secs))
        cur.executemany(
            "INSERT INTO camera_adjacency (from_camera, to_camera, road_dist_m, travel_s) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (from_camera,to_camera) DO UPDATE "
            "SET road_dist_m=EXCLUDED.road_dist_m, travel_s=EXCLUDED.travel_s, updated_at=now()",
            edges)

        # ── partitions for today ─────────────────────────────────────
        cur.execute("SELECT count(*) FROM ensure_partitions()")

        n_cam = cur.execute("SELECT count(*) FROM camera").fetchone()[0]
        n_adj = cur.execute("SELECT count(*) FROM camera_adjacency").fetchone()[0]
        avg = cur.execute(
            "SELECT round(avg(c),1) FROM (SELECT count(*) c FROM camera_adjacency "
            "GROUP BY from_camera) t").fetchone()[0]

    print(f"  departments      {len(DEPARTMENTS)}")
    print(f"  zones            {len(zones)} (+1 restricted geofence)")
    print(f"  users            {len(users)}")
    print(f"  cameras          {n_cam}")
    print(f"  adjacency edges  {n_adj}  (avg {avg} reachable per camera within 15 min)")
    if generated:
        print()
        print("  ┌─────────────────────────────────────────────────────────┐")
        print(f"  │  Demo login for all accounts:  {demo_password:<24} │")
        print("  │  Shown once. Set SEED_ADMIN_PASSWORD to choose your own. │")
        print("  └─────────────────────────────────────────────────────────┘")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get(
        "DATABASE_URL", "postgresql://sentinel:sentinel@localhost:5432/sentinel"))
    ap.add_argument("--cameras", type=int, default=50)
    args = ap.parse_args()
    print("Seeding Sentinel demo estate (Ahmedabad)...")
    seed(args.dsn, args.cameras)
    return 0


if __name__ == "__main__":
    sys.exit(main())
