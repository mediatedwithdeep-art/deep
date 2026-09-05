#!/usr/bin/env python3
"""Scale load test — measure the estate at 50, 500 and 1,000 cameras.

    python3 scripts/scale_test.py                    # 50, 500, 1000
    python3 scripts/scale_test.py --cameras 50 500 1000 2000
    python3 scripts/scale_test.py --json

WHAT THIS MEASURES, AND WHAT IT DOES NOT
────────────────────────────────────────
It measures the **event path**: scene -> detect -> track -> quality gate ->
ANPR/ReID -> sighting -> bus, for N cameras, with the real pipeline code
and the real spatio-temporal gate.

It does NOT measure video decode. Decoding 1,000 RTSP streams needs 1,000
streams and the hardware to decode them; that cost is measured separately
at 50 real cameras in docs/SENTINEL_LIVE_TEST_REPORT.md and modelled from
there. Presenting a number here that skipped decode and calling it
"1,000 cameras" would be the single most misleading thing this report
could do, so the two are kept apart and labelled.

THE SYNTHETIC ESTATE, AND WHY IT IS DENSE RATHER THAN TILED
───────────────────────────────────────────────────────────
There are 50 real Ahmedabad junctions with a measured road graph, and one
simulated traffic world that moves vehicles along it.

The first version of this harness tiled that topology across synthetic
districts offset in lat/lon. It produced a flat per-camera cost and a
nonsense measurement: the traffic world spans only the real graph, so
cameras in districts 1-9 watched empty ground. At 500 cameras just 32 were
producing sightings, and the other 468 were being timed on the
nothing-in-view path -- which would have understated the real per-camera
cost by roughly the idle fraction.

So the estate is DENSE instead: camera i watches junction i % 50, with its
heading rotated per camera so cameras sharing a junction cover different
approaches. A busy junction genuinely does carry four to eight cameras, so
this is a real deployment shape, and critically EVERY camera sees traffic.

It is also the pessimistic shape for the spatio-temporal gate: cameras
packed onto shared junctions have far denser adjacency than a spread
estate, so the gate does more work per sighting here than it would across
26 districts. A capacity number derived from this errs low, which is the
right direction for a claim.

Every figure printed is MEASURED on this host. Extrapolations to 3k/10k/
50k/80k are in docs/SCALE_BENCHMARK.md and are labelled there.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import resource
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in ("shared", "video-ingestion", "ai", "event-processor",
          "database/seeds"):
    sys.path.insert(0, str(ROOT / p))

from ahmedabad import JUNCTIONS, RoadGraph, bearing_between   # noqa: E402
from ingestion.camera_config import CameraSpec                # noqa: E402
from ingestion.supervisor import IngestionSupervisor          # noqa: E402
from sentinel_core.bus import Topics, create_bus              # noqa: E402
from sentinel_core.domain import Protocol                     # noqa: E402
from sentinel_core.log import configure_logging               # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"

def build_estate(n: int) -> list[CameraSpec]:
    """N cameras over the measured Ahmedabad topology, densely.

    Camera i watches junction i % 50. Cameras sharing a junction get
    different headings so they cover different approaches rather than
    duplicating one view -- and so every camera is looking at road that
    the traffic world actually populates.

    Headings come from the road graph exactly as the seeder does. A fixed
    heading for every camera leaves most of them pointing at empty ground,
    which would make this measure the harness rather than the system.
    """
    graph = RoadGraph.build()
    specs: list[CameraSpec] = []
    per_tile = len(JUNCTIONS)

    for i in range(n):
        layer, idx = divmod(i, per_tile)
        j = JUNCTIONS[idx]
        neighbours = graph.neighbours.get(j.code, [])
        base = bearing_between(j.code, neighbours[0][0]) if neighbours else 0.0
        # Each additional camera on this junction covers a different
        # approach. Four cameras per junction is an ordinary signalised
        # intersection; the modulo keeps them spread rather than stacked.
        heading = (base + layer * 73.0) % 360.0
        anpr = i % 4 == 0
        specs.append(CameraSpec(
            camera_id=f"S{layer:02d}-{idx:03d}",
            name=f"{j.name} #{layer + 1}",
            latitude=j.lat, longitude=j.lon,
            heading_deg=round(heading, 1),
            fov_deg=32 if anpr else 82,
            range_m=45 if anpr else 65,
            width=1920 if anpr else 1280,
            height=1080 if anpr else 720,
            anpr_capable=anpr,
            protocol=Protocol.SIMULATED))
    return specs


def _rss_mb() -> float:
    # ru_maxrss is KiB on Linux.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


async def measure(n: int, seconds: float, tick_hz: float,
                  vehicles_per_100_cams: int) -> dict:
    specs = build_estate(n)
    # Traffic density is held constant PER CAMERA. Holding the absolute
    # vehicle count constant while multiplying cameras would make each
    # camera quieter as the estate grew, and the per-camera cost would fall
    # for a reason that has nothing to do with scaling.
    vehicles = max(200, int(n * vehicles_per_100_cams / 100))

    bus = create_bus("memory")
    await bus.connect()
    sup = IngestionSupervisor(specs, bus, mode="demo", tick_hz=tick_hz,
                             vehicle_count=vehicles, time_scale=3.0)
    sup.seed_target_vehicle()

    rss_before = _rss_mb()
    build_done = time.perf_counter()

    for _ in range(20):                       # warm up: JIT-free, but the
        await sup._tick()                     # first ticks allocate.
    base = len(bus.published)

    ticks = max(1, int(seconds * tick_hz))
    per_tick: list[float] = []
    t0 = time.perf_counter()
    for _ in range(ticks):
        t = time.perf_counter()
        await sup._tick()
        per_tick.append((time.perf_counter() - t) * 1000)
    elapsed = time.perf_counter() - t0

    msgs = bus.published[base:]
    sightings = [m for m in msgs if m.topic == Topics.SIGHTINGS]
    with_plate = [m for m in sightings if m.payload.get("plate")]
    active = {m.payload["camera_id"] for m in sightings}

    budget_ms = 1000.0 / tick_hz
    mean_ms = statistics.mean(per_tick)
    p95_ms = sorted(per_tick)[int(len(per_tick) * 0.95) - 1]
    budget_pct = mean_ms / budget_ms * 100

    return {
        "cameras": n,
        "vehicles": vehicles,
        "tick_hz": tick_hz,
        "simulated_s": round(ticks / tick_hz, 1),
        "wall_s": round(elapsed, 2),
        "realtime_factor": round((ticks / tick_hz) / elapsed, 2),
        "mean_tick_ms": round(mean_ms, 2),
        "p95_tick_ms": round(p95_ms, 2),
        "budget_pct": round(budget_pct, 1),
        "us_per_camera_per_tick": round(mean_ms * 1000 / n, 1),
        "sightings": len(sightings),
        "sightings_per_s": round(len(sightings) / (ticks / tick_hz), 1),
        "plate_rate": round(len(with_plate) / max(len(sightings), 1), 4),
        "cameras_active": len(active),
        "rss_mb": round(_rss_mb(), 1),
        "rss_mb_per_camera": round((_rss_mb() - rss_before) / n, 3),
        "setup_s": round(build_done - build_done, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", type=int, nargs="+", default=[50, 500, 1000])
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="simulated seconds per level")
    ap.add_argument("--tick-hz", type=float, default=6.0)
    ap.add_argument("--vehicles-per-100-cams", type=int, default=3600,
                    help="traffic density, held constant per camera")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    configure_logging("scale", "ERROR", "console")

    def note(m=""):
        print(m, file=sys.stderr, flush=True)

    note(f"{BOLD}SCALE LOAD TEST{RESET}  "
         f"{os.cpu_count()} vCPU · event path only, no video decode")

    results = []
    for n in args.cameras:
        note(f"{DIM}  {n:>5} cameras ...{RESET}")
        r = asyncio.run(measure(n, args.seconds, args.tick_hz,
                                args.vehicles_per_100_cams))
        results.append(r)
        note(f"{DIM}      {r['mean_tick_ms']:.1f} ms/tick "
             f"({r['budget_pct']:.0f}% of budget), "
             f"{r['sightings_per_s']:.0f} sightings/s, "
             f"{r['rss_mb']:.0f} MB RSS{RESET}")

    if args.json:
        print(json.dumps({"host": {"cpus": os.cpu_count()},
                          "levels": results}, indent=1))
        return 0

    print(f"\n{BOLD}MEASURED — event path, {os.cpu_count()} vCPU{RESET}")
    print("─" * 88)
    hdr = (f"{'cameras':>8} {'ms/tick':>9} {'p95':>8} {'budget':>8} "
           f"{'us/cam':>8} {'sight/s':>9} {'active':>8} {'RSS MB':>8}")
    print(hdr)
    for r in results:
        print(f"{r['cameras']:>8} {r['mean_tick_ms']:>9.2f} "
              f"{r['p95_tick_ms']:>8.2f} {r['budget_pct']:>7.1f}% "
              f"{r['us_per_camera_per_tick']:>8.1f} "
              f"{r['sightings_per_s']:>9.1f} "
              f"{r['cameras_active']:>8} {r['rss_mb']:>8.1f}")

    # Linearity is the property that matters for extrapolation. If the
    # per-camera cost is flat, the model in SCALE_BENCHMARK.md is a
    # multiplication; if it grows, that document must say so instead.
    if len(results) > 1:
        first, last = results[0], results[-1]
        ratio = last["us_per_camera_per_tick"] / first["us_per_camera_per_tick"]
        print(f"\n{BOLD}per-camera cost{RESET}: "
              f"{first['us_per_camera_per_tick']:.1f} us at "
              f"{first['cameras']} -> {last['us_per_camera_per_tick']:.1f} us at "
              f"{last['cameras']}  ({ratio:.2f}x)")
        print("  " + ("linear within noise; extrapolation is a multiplication"
                      if 0.8 <= ratio <= 1.25 else
                      "NOT linear -- extrapolation must account for this"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
