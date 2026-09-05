#!/usr/bin/env python3
"""Measure the pipeline instead of asserting numbers about it.

Runs the real ingestion → AI → matcher path against the demo world, where
ground truth is known, and reports throughput and accuracy. Every figure in
docs/BENCHMARKS.md comes from this script; if it disagrees with the docs,
the docs are wrong.

    python scripts/benchmark.py                  # all suites
    python scripts/benchmark.py --suite anpr     # one suite
    python scripts/benchmark.py --json           # machine-readable
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics as stat
import sys
import time
from datetime import datetime, timedelta, timezone

REPO = pathlib.Path(__file__).resolve().parents[1]
for sub in ("shared", "ai", "video-ingestion", "event-processor", "database/seeds"):
    sys.path.insert(0, str(REPO / sub))

RESULTS: dict[str, dict] = {}


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("─" * 72)


def row(label: str, value, note: str = "") -> None:
    print(f"  {label:<40} {str(value):>14}  {note}")


# ─────────────────────────────────────────────────────────────────────
# 1. Detection + tracking
# ─────────────────────────────────────────────────────────────────────

def bench_detector() -> dict:
    from sentinel_ai.backends.simulation import SimulationDetector
    from sentinel_ai.detector import SceneObject
    from sentinel_core.domain import BoundingBox, VehicleType

    section("1. DETECTION — recall against known ground truth")
    t0 = datetime.now(timezone.utc)
    out = {}
    for label, box, occl in [
        ("clear, 140x95 px", (140, 95), 0.0),
        ("small, 60x42 px", (60, 42), 0.0),
        ("tiny, 30x22 px", (30, 22), 0.0),
        ("occluded 40%", (140, 95), 0.4),
        ("occluded 70%", (140, 95), 0.7),
    ]:
        d = SimulationDetector()
        obj = SceneObject(identity="GT", vehicle_type=VehicleType.CAR, colour="white",
                          plate="GJ01AB1234",
                          bbox=BoundingBox(x=10, y=10, w=box[0], h=box[1]),
                          occlusion=occl)
        for f in range(600):
            d.detect(camera_id="C", timestamp=t0, scene=[obj], frame_seq=f)
        recall = (d.stats.objects_in - d.stats.missed) / d.stats.objects_in
        out[label] = round(recall, 4)
        row(label, f"{recall*100:.1f}%", "recall")
    row("inference latency", f"{d.stats.mean_inference_ms:.3f} ms", "per frame, 1 object")
    return out


def bench_tracker() -> dict:
    from sentinel_ai.tracker import ByteTracker
    from sentinel_core.domain import BoundingBox, Detection, VehicleType

    section("2. TRACKING — identity continuity through occlusion")
    t0 = datetime.now(timezone.utc)

    def run(occluded_frames: int) -> int:
        tr = ByteTracker("C")
        for f in range(6):
            tr.update([Detection(camera_id="C", track_id="", timestamp=t0 + timedelta(seconds=f*.1),
                                 vehicle_type=VehicleType.CAR, confidence=0.9,
                                 bbox=BoundingBox(x=50+f*18, y=100, w=100, h=70))],
                      t0 + timedelta(seconds=f*.1))
        for f in range(6, 6+occluded_frames):
            tr.update([Detection(camera_id="C", track_id="", timestamp=t0 + timedelta(seconds=f*.1),
                                 vehicle_type=VehicleType.CAR, confidence=0.22,
                                 bbox=BoundingBox(x=50+f*18, y=100, w=100, h=70))],
                      t0 + timedelta(seconds=f*.1))
        f = 6 + occluded_frames
        active = tr.update([Detection(camera_id="C", track_id="", timestamp=t0 + timedelta(seconds=f*.1),
                                      vehicle_type=VehicleType.CAR, confidence=0.9,
                                      bbox=BoundingBox(x=50+f*18, y=100, w=100, h=70))],
                           t0 + timedelta(seconds=f*.1))
        return len(tr.tracks)

    out = {}
    for n in (2, 4, 8):
        tracks = run(n)
        held = tracks == 1
        out[f"{n}_occluded_frames"] = held
        row(f"{n} low-confidence frames", "held" if held else f"SPLIT into {tracks}",
            "ByteTrack second-pass association")

    # Throughput
    tr = ByteTracker("C")
    dets = [Detection(camera_id="C", track_id="", timestamp=t0, vehicle_type=VehicleType.CAR,
                      confidence=0.9, bbox=BoundingBox(x=i*40, y=100, w=60, h=45))
            for i in range(12)]
    start = time.perf_counter()
    for f in range(2000):
        tr.update(dets, t0 + timedelta(seconds=f*0.1))
    ms = (time.perf_counter() - start) / 2000 * 1000
    out["ms_per_frame_12_objects"] = round(ms, 4)
    row("tracker throughput", f"{ms:.3f} ms", "per frame, 12 objects")
    return out


# ─────────────────────────────────────────────────────────────────────
# 2. ANPR
# ─────────────────────────────────────────────────────────────────────

def bench_anpr() -> dict:
    from sentinel_ai.anpr import create_recognizer

    section("3. ANPR — accuracy envelope by camera class")
    r = create_recognizer("simulation")
    plates = ["GJ01AB1234", "MH12DE1433", "GJ27XY0987", "DL8CAF5030", "GJ05KL7788",
              "RJ14QR2211", "GJ18MN4455", "KA03PQ9900", "GJ21ST3344", "TN09UV5566",
              "GJ06BX9600", "UP33JG8841"]
    out = {}
    for label, px, night, blur in [
        ("dedicated ANPR lane, day", 165, False, 400),
        ("dedicated ANPR lane, night + IR", 155, True, 300),
        ("general surveillance, day", 108, False, 240),
        ("general surveillance, night", 102, True, 150),
        ("wide-angle junction, 72 px plate", 72, False, 200),
        ("wide-angle junction, 58 px plate", 58, False, 200),
    ]:
        reads = exact = corrected = 0
        for p in plates:
            for v in range(40):
                res = r.read(ground_truth=p, plate_width_px=px + v*0.4,
                             is_night=night, blur_variance=blur + v*2)
                if res:
                    reads += 1
                    corrected += res.corrected
                    exact += res.normalized_plate == p
        total = len(plates) * 40
        out[label] = {"read_rate": round(reads/total, 4),
                      "exact_of_read": round(exact/max(reads, 1), 4),
                      "end_to_end": round(exact/total, 4)}
        row(label, f"{100*exact/total:.1f}%",
            f"read {100*reads/total:.0f}% · exact-of-read {100*exact/max(reads,1):.0f}%")
    return out


def bench_fuzzy() -> dict:
    from sentinel_core import plate_rules

    section("4. PLATE MATCHING — recovery of systematic OCR confusions")
    confusions = [("GJ01AB1234", "GJ0IAB1234", "I/1"), ("GJ01AB1234", "GJO1AB1234", "O/0"),
                  ("GJ01AB1234", "GJ01AB1284", "3/8"), ("MH12DE1433", "MH12DE1A33", "4/A"),
                  ("GJ27XY0987", "GJ27XY098T", "7/T"), ("DL8CAF5030", "DL8CAF503O", "0/O")]
    matched = sum(1 for a, b, _ in confusions if plate_rules.match(a, b).matched)
    exact_only = sum(1 for a, b, _ in confusions if plate_rules.normalize(a) == plate_rules.normalize(b))
    row("single-confusion reads recovered", f"{matched}/{len(confusions)}", "fuzzy matcher")
    row("recovered by exact comparison", f"{exact_only}/{len(confusions)}", "for contrast")

    # False positives against genuinely different plates
    others = ["MH12CD9876", "RJ14QR2211", "KA03PQ9900", "TN09UV5566", "UP33JG8841",
              "GJ99ZZ9999", "DL01AA0001", "MP09XY1234"]
    fp = sum(1 for o in others if plate_rules.match("GJ01AB1234", o).matched)
    row("false matches on unrelated plates", f"{fp}/{len(others)}", "must be 0")
    return {"recovered": matched, "of": len(confusions),
            "exact_only": exact_only, "false_positives": fp}


# ─────────────────────────────────────────────────────────────────────
# 3. ReID
# ─────────────────────────────────────────────────────────────────────

def bench_reid() -> dict:
    from sentinel_ai.reid import create_extractor, cosine
    import random

    section("5. RE-IDENTIFICATION — separation and overlap")
    e = create_extractor("simulation")
    rng = random.Random(7)

    def emb(v, t="car", c="white", q=1.0):
        return e.extract(identity=v, vehicle_type=t, colour=c, view_quality=q)

    same = [cosine(emb(f"V{i}", q=rng.uniform(.5, 1)), emb(f"V{i}", q=rng.uniform(.5, 1)))
            for i in range(800)]
    hard = [cosine(emb(f"V{i}"), emb(f"V{i+9000}")) for i in range(800)]
    easy = [cosine(emb(f"V{i}", "car", "white"), emb(f"V{i+9000}", "truck", "red"))
            for i in range(800)]

    row("same vehicle, different view", f"{stat.mean(same):.3f} ± {stat.pstdev(same):.3f}", "cosine")
    row("different vehicle, same type+colour", f"{stat.mean(hard):.3f} ± {stat.pstdev(hard):.3f}", "hardest case")
    row("different vehicle, type+colour differ", f"{stat.mean(easy):.3f} ± {stat.pstdev(easy):.3f}", "")
    overlap = sum(1 for v in hard if v > min(same))
    row("distribution overlap", f"{overlap}/800", "real ReID overlaps; 0 would be a red flag")

    for thr in (0.55, 0.62, 0.70):
        tp = sum(1 for v in same if v >= thr) / len(same)
        fp = sum(1 for v in hard if v >= thr) / len(hard)
        row(f"  threshold {thr}", f"recall {tp*100:.1f}%", f"false positive {fp*100:.1f}%")

    # Extract once, then time only the comparison. Including extraction
    # would report the cost of a stage the matcher does not repeat per pair.
    a, b = emb("A"), emb("B")
    t0 = time.perf_counter()
    for _ in range(20000):
        cosine(a, b)
    per_us = (time.perf_counter() - t0) / 20000 * 1e6
    row("comparison throughput", f"{per_us:.1f} µs", "per pair, 512-d (vectorised)")
    return {"same_mean": round(stat.mean(same), 4), "same_sd": round(stat.pstdev(same), 4),
            "hard_mean": round(stat.mean(hard), 4), "hard_sd": round(stat.pstdev(hard), 4),
            "overlap_of_800": overlap}


# ─────────────────────────────────────────────────────────────────────
# 4. Estate throughput
# ─────────────────────────────────────────────────────────────────────

def bench_estate(cameras: int = 50, seconds: int = 30) -> dict:
    import asyncio
    from ahmedabad import JUNCTIONS
    from sentinel_core.bus import create_bus, Topics
    from sentinel_core.log import configure_logging
    from ingestion.camera_config import CameraSpec
    from ingestion.supervisor import IngestionSupervisor
    from sentinel_core.domain import Protocol

    configure_logging("bench", "ERROR", "console")
    section(f"6. ESTATE THROUGHPUT — {cameras} cameras, {seconds}s of simulated operation")

    # Aim each camera along the road it watches, exactly as the seeder
    # does. A fixed heading for every camera leaves most of them pointing at
    # empty ground, which understates throughput by an order of magnitude
    # and would make this benchmark measure the harness, not the system.
    from ahmedabad import RoadGraph, bearing_between
    graph = RoadGraph.build()
    specs = []
    for i, j in enumerate(JUNCTIONS[:cameras]):
        neighbours = graph.neighbours.get(j.code, [])
        heading = bearing_between(j.code, neighbours[0][0]) if neighbours else 0.0
        anpr = i % 4 == 0
        specs.append(CameraSpec(
            camera_id=f"B-{i:03d}", name=j.name, latitude=j.lat, longitude=j.lon,
            heading_deg=round(heading, 1),
            fov_deg=32 if anpr else 82, range_m=45 if anpr else 65,
            width=1920 if anpr else 1280, height=1080 if anpr else 720,
            anpr_capable=anpr, protocol=Protocol.SIMULATED))

    async def run():
        bus = create_bus("memory")
        await bus.connect()
        sup = IngestionSupervisor(specs, bus, mode="demo", tick_hz=6.0,
                                  vehicle_count=1800, time_scale=3.0)
        sup.seed_target_vehicle()
        for _ in range(30):
            await sup._tick()                     # warm up
        base = len(bus.published)
        t0 = time.perf_counter()
        ticks = int(seconds * 6)
        for _ in range(ticks):
            await sup._tick()
        elapsed = time.perf_counter() - t0
        msgs = bus.published[base:]
        return elapsed, ticks, msgs, sup

    elapsed, ticks, msgs, sup = asyncio.run(run())
    sightings = [m for m in msgs if m.topic == Topics.SIGHTINGS]
    with_plate = [m for m in sightings if m.payload.get("plate")]
    cams_active = {m.payload["camera_id"] for m in sightings}

    per_tick_ms = elapsed / ticks * 1000
    budget_pct = per_tick_ms / (1000 / 6) * 100
    row("wall time", f"{elapsed:.2f} s", f"for {seconds}s simulated")
    row("per tick, whole estate", f"{per_tick_ms:.1f} ms", f"{budget_pct:.0f}% of the 6 Hz budget")
    row("sightings produced", len(sightings), f"{len(sightings)/seconds:.0f}/s")
    row("with a plate read", f"{len(with_plate)} ({100*len(with_plate)/max(len(sightings),1):.1f}%)", "")
    row("cameras producing sightings", f"{len(cams_active)}/{cameras}", "")
    row("headroom at this rate", f"{100/max(budget_pct,0.01):.0f}x", "cameras per node, single core")
    est = int(cameras * (100 / max(budget_pct, 0.01)))
    row("→ implied capacity per core", f"~{est} cameras", "extrapolated, single process")
    return {"per_tick_ms": round(per_tick_ms, 2), "budget_pct": round(budget_pct, 1),
            "sightings": len(sightings), "plate_rate": round(len(with_plate)/max(len(sightings), 1), 4),
            "cameras_active": len(cams_active), "implied_capacity": est}


# ─────────────────────────────────────────────────────────────────────
# 5. Spatio-temporal gate
# ─────────────────────────────────────────────────────────────────────

def bench_gate() -> dict:
    from ahmedabad import RoadGraph, JUNCTIONS

    section("7. SPATIO-TEMPORAL GATE — candidate reduction")
    g = RoadGraph.build()
    n = len(JUNCTIONS)
    horizons = {}
    for horizon in (60, 180, 300, 900):
        counts = [len(g.shortest_paths(j.code, horizon)) for j in JUNCTIONS]
        mean = stat.mean(counts)
        horizons[horizon] = round(mean, 2)
        row(f"reachable within {horizon:>3}s", f"{mean:.1f} of {n-1}",
            f"{100*(1-mean/(n-1)):.1f}% fewer comparisons")
    row("", "", "")
    row("without the gate", f"{n-1} candidates", "every camera, every sighting")

    # ── PART 17: the same reduction priced in PAIRS and MILLISECONDS ──
    #
    # Candidate cameras are a property of the road graph and prove nothing
    # about cost. What a reviewer can hold us to is comparisons and time, so
    # the per-pair cost is MEASURED here against the real scorer and then
    # multiplied by the gated and ungated pair counts. The multiplication is
    # arithmetic and is labelled as such; the microseconds are not.
    from sentinel_core import fusion
    from sentinel_ai.reid import create_extractor

    ex = create_extractor("simulation")
    now = datetime.now(timezone.utc)

    def _tracklet(vid: str, cam: str):
        return fusion.Tracklet(
            tracklet_id=vid, camera_id=cam, ts_enter=now, ts_exit=now,
            vclass="car", embedding=ex.extract(identity=vid, vehicle_type="car",
                                               colour="white", view_quality=0.9),
            embedding_model="simulation", plate_text=None,
            colour="white", colour_conf=0.85)

    gate = fusion.Gate(from_camera="A", to_camera="B",
                       window_start=now - timedelta(seconds=60),
                       window_end=now + timedelta(seconds=60),
                       expected_at=now, travel_s=120.0, source="road")
    a, b = _tracklet("V1", "A"), _tracklet("V2", "B")
    fusion.score_pair(a, b, gate)                      # warm the path

    reps = 4000
    t0 = time.perf_counter()
    for _ in range(reps):
        fusion.score_pair(a, b, gate)
    per_pair_us = 1e6 * (time.perf_counter() - t0) / reps

    # One batch, at the demo estate's live-vehicle load. The gated count
    # follows from the 180 s reachability measured above.
    open_vehicles, batch = 400, 50
    mean_reach = horizons[180]
    gated_pairs = int(round(batch * open_vehicles * (mean_reach / (n - 1))))
    ungated_pairs = batch * open_vehicles

    row("", "", "")
    row("measured cost per scored pair", f"{per_pair_us:.1f} us", "fusion.score_pair, 4,000 reps")
    row("pairs scored WITHOUT the gate", f"{ungated_pairs:,}",
        f"{batch} sightings x {open_vehicles} open vehicles")
    row("pairs scored WITH the gate", f"{gated_pairs:,}",
        f"180 s reachability, {mean_reach:.1f} of {n-1} cameras")
    row("→ pair reduction", f"{100*(1-gated_pairs/ungated_pairs):.1f}%", "calculated")
    row("scoring time WITHOUT the gate",
        f"{ungated_pairs*per_pair_us/1000:.1f} ms", "measured us x calculated pairs")
    row("scoring time WITH the gate",
        f"{gated_pairs*per_pair_us/1000:.1f} ms", "measured us x calculated pairs")

    return {"cameras": n, "mean_candidates": horizons,
            "per_pair_us": round(per_pair_us, 2),
            "batch_sightings": batch, "open_vehicles": open_vehicles,
            "ungated_pairs": ungated_pairs, "gated_pairs": gated_pairs,
            "ungated_ms": round(ungated_pairs * per_pair_us / 1000, 2),
            "gated_ms": round(gated_pairs * per_pair_us / 1000, 2)}


SUITES = {
    "detector": bench_detector, "tracker": bench_tracker, "anpr": bench_anpr,
    "fuzzy": bench_fuzzy, "reid": bench_reid, "estate": bench_estate, "gate": bench_gate,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=list(SUITES) + ["all"], default="all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print("\033[1mSENTINEL BENCHMARK\033[0m")
    print(f"simulation backend · no GPU · {time.strftime('%Y-%m-%d %H:%M:%S')}")

    chosen = SUITES if args.suite == "all" else {args.suite: SUITES[args.suite]}
    for name, fn in chosen.items():
        RESULTS[name] = fn()

    if args.json:
        print("\n" + json.dumps(RESULTS, indent=2))
    print("\n\033[1mNote:\033[0m these are the simulation backend's numbers, calibrated to "
          "published\nmodel behaviour. Real-model figures require weights and a GPU; the "
          "envelope\nis in docs/BENCHMARKS.md.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
