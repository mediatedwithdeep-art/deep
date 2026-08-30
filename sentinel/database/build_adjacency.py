#!/usr/bin/env python3
"""
Build the camera adjacency graph: road-network travel time between every
pair of nearby cameras.

This populates `camera_adjacency`, which is the spatio-temporal gate. It is
the highest-leverage component in the whole system -- it removes 95-98% of
candidate comparisons before any model runs, and it is what makes an 85%-mAP
ReID model operationally trustworthy (docs/03 §4.3).

Run after any bulk camera onboarding:
    python3 scripts/build_adjacency.py --max-dist 5000

Requires an OSRM instance with the Gujarat extract (scripts/prepare_osrm.sh).
Falls back to straight-line distance with a road-winding factor if OSRM is
unreachable -- degraded but still far better than no gate at all.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.request
import urllib.error

try:
    import psycopg
except ImportError:
    print("pip install 'psycopg[binary]'", file=sys.stderr)
    raise

OSRM_URL = os.environ.get("OSRM_URL", "http://localhost:5001")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://sentinel:sentinel@localhost:5432/sentinel")

# Straight-line distance underestimates road distance. Urban Indian road
# networks run about 1.35-1.45x the crow-flight distance; use the lower end
# so the fallback gate stays permissive rather than wrongly excluding a real
# transition. A gate that is too tight causes MISSED detections, which are
# far more damaging than the extra false positives from one that is loose.
WINDING_FACTOR = 1.35
FALLBACK_SPEED_KMPH = 28.0      # realistic urban average incl. signals


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def osrm_table(coords: list[tuple[float, float]], sources: list[int],
               destinations: list[int], timeout: int = 60) -> dict | None:
    """OSRM /table: many-to-many durations and distances in one call.

    Batched deliberately -- one /route call per pair would be ~2500 HTTP
    round trips for 50 cameras and hours for the state estate.
    """
    coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
    url = (f"{OSRM_URL}/table/v1/driving/{coord_str}"
           f"?sources={';'.join(map(str, sources))}"
           f"&destinations={';'.join(map(str, destinations))}"
           f"&annotations=duration,distance")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read())
        return data if data.get("code") == "Ok" else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        print(f"  OSRM unavailable ({e}); falling back to winding-factor estimate",
              file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-dist", type=float, default=5000,
                    help="Only pair cameras within this crow-flight distance (m)")
    ap.add_argument("--max-travel", type=float, default=900,
                    help="Drop edges slower than this (s). Beyond ~15 min the gate stops discriminating.")
    ap.add_argument("--batch", type=int, default=80,
                    help="Cameras per OSRM /table call")
    ap.add_argument("--department", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        q = ("SELECT c.id, ST_Y(c.geom::geometry), ST_X(c.geom::geometry), c.name "
             "FROM camera c JOIN department d ON d.id = c.department_id "
             "WHERE c.status <> 'DISABLED' AND c.geom IS NOT NULL")
        params: list = []
        if args.department:
            q += " AND d.code = %s"
            params.append(args.department)
        cams = conn.execute(q, params).fetchall()

        n = len(cams)
        print(f"{n} cameras")
        if n < 2:
            print("Need at least 2 cameras.")
            return 0

        # Candidate pairs by crow-flight distance first. Quadratic in n, but
        # n is small per district and the spatial prefilter is what stops
        # this from being quadratic across the whole state.
        pairs: list[tuple[int, int, float]] = []
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                d = haversine_m(cams[i][1], cams[i][2], cams[j][1], cams[j][2])
                if d <= args.max_dist:
                    pairs.append((i, j, d))
        print(f"{len(pairs)} candidate pairs within {args.max_dist:.0f} m "
              f"({len(pairs) / max(1, n * (n - 1)) * 100:.1f}% of all ordered pairs)")

        coords = [(c[1], c[2]) for c in cams]
        rows: list[tuple] = []
        idx = {(i, j): d for i, j, d in pairs}

        # Batch the OSRM calls over blocks of source cameras.
        used_osrm = False
        for start in range(0, n, args.batch):
            srcs = list(range(start, min(start + args.batch, n)))
            dests = sorted({j for i, j, _ in pairs if i in srcs})
            if not dests:
                continue
            tbl = osrm_table(coords, srcs, dests)
            if tbl is None:
                break
            used_osrm = True
            durations = tbl["durations"]
            distances = tbl.get("distances") or [[None] * len(dests)] * len(srcs)
            for si, i in enumerate(srcs):
                for di, j in enumerate(dests):
                    if (i, j) not in idx:
                        continue
                    dur = durations[si][di]
                    dist = distances[si][di]
                    if dur is None or dur > args.max_travel:
                        continue
                    rows.append((cams[i][0], cams[j][0],
                                 float(dist if dist is not None else idx[(i, j)]),
                                 float(dur)))
            print(f"  OSRM batch {start}-{srcs[-1]}: {len(rows)} edges so far")

        if not used_osrm:
            # Degraded fallback. Still enormously better than no gate.
            for i, j, d in pairs:
                road_m = d * WINDING_FACTOR
                travel_s = road_m / (FALLBACK_SPEED_KMPH * 1000 / 3600)
                if travel_s <= args.max_travel:
                    rows.append((cams[i][0], cams[j][0], road_m, travel_s))
            print(f"  fallback estimate: {len(rows)} edges")

        print(f"{len(rows)} adjacency edges "
              f"(avg {len(rows) / n:.1f} downstream candidates per camera)")

        if args.dry_run:
            for a, b, dm, ds in rows[:10]:
                print(f"  {a} -> {b}: {dm:.0f} m, {ds:.0f} s")
            return 0

        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO camera_adjacency
                       (from_camera, to_camera, road_dist_m, travel_s, updated_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (from_camera, to_camera) DO UPDATE
                     SET road_dist_m = EXCLUDED.road_dist_m,
                         travel_s    = EXCLUDED.travel_s,
                         updated_at  = now()""",
                rows)
        print("written to camera_adjacency")

        # Sanity check: report the gate's selectivity, which is the number
        # that actually predicts cross-camera precision.
        avg = conn.execute(
            "SELECT avg(cnt) FROM (SELECT count(*) cnt FROM camera_adjacency "
            "GROUP BY from_camera) t").fetchone()[0]
        if avg:
            print(f"Gate selectivity: {avg:.1f} candidates per sighting "
                  f"instead of {n - 1} — a {(1 - avg / max(1, n - 1)) * 100:.1f}% reduction")
    return 0


if __name__ == "__main__":
    sys.exit(main())
