"""Tests for video ingestion: world simulation, config loading, workers.

No cameras, no GPU, no database required.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile

import pytest

from sentinel_core.bus import create_bus, Topics
from sentinel_core.domain import Protocol, VehicleType
from sentinel_core.geo import haversine_m
from ingestion.camera_config import (
    CameraSpec, load_from_yaml, resolve_credentials,
)
from ingestion.stream_reader import FrameReader, redact
from ingestion.world import TrafficWorld
from ingestion.supervisor import IngestionSupervisor


# ── camera configuration ─────────────────────────────────────────────

def _write(text: str) -> pathlib.Path:
    p = pathlib.Path(tempfile.mktemp(suffix=".yaml"))
    p.write_text(text)
    return p


def test_yaml_defaults_merge_into_each_camera():
    p = _write("""
defaults: {protocol: DVR, fps: 10, department: AMC}
cameras:
  - {camera_id: C1, name: One, latitude: 23.0, longitude: 72.5}
  - {camera_id: C2, name: Two, latitude: 23.1, longitude: 72.6, fps: 25}
""")
    specs = {s.camera_id: s for s in load_from_yaml(p)}
    assert specs["C1"].protocol == Protocol.DVR
    assert specs["C1"].fps == 10 and specs["C2"].fps == 25
    assert specs["C1"].department == "AMC"
    p.unlink()


def test_one_malformed_camera_does_not_stop_the_others():
    """A single bad row in a 2,000-camera import must not take the estate
    offline. It is reported and skipped."""
    p = _write("""
cameras:
  - {camera_id: GOOD1, name: Fine, latitude: 23.0, longitude: 72.5}
  - {camera_id: BAD, name: NoCoordinates}
  - {camera_id: GOOD2, name: Also fine, latitude: 23.1, longitude: 72.6}
""")
    specs = load_from_yaml(p)
    assert {s.camera_id for s in specs} == {"GOOD1", "GOOD2"}
    p.unlink()


def test_inline_credentials_are_refused_in_production():
    p = _write("""
cameras:
  - {camera_id: C1, name: X, latitude: 23.0, longitude: 72.5,
     stream_url: 'rtsp://admin:secret@10.0.0.1:554/main'}
""")
    assert len(load_from_yaml(p, "demo")) == 1          # tolerated for a laptop demo
    with pytest.raises(ValueError, match="credential_ref"):
        load_from_yaml(p, "production")
    p.unlink()


def test_credentials_resolve_from_environment_and_are_url_encoded():
    os.environ["TEST_CAM_CRED"] = "viewer:p@ss/word:x"
    assert resolve_credentials("env:TEST_CAM_CRED") == ("viewer", "p@ss/word:x")
    spec = CameraSpec(camera_id="C1", name="X", latitude=23.0, longitude=72.5,
                      substream_url="rtsp://10.0.0.1:554/sub",
                      credential_ref="env:TEST_CAM_CRED")
    url = spec.resolve_url("sub")
    # Government DVR passwords contain '@', '/' and ':' with cheerful
    # regularity; an unencoded one silently resolves to a different host.
    assert "p%40ss%2Fword" in url
    assert url.endswith("@10.0.0.1:554/sub")
    del os.environ["TEST_CAM_CRED"]


def test_unresolvable_credentials_connect_anonymously_rather_than_guessing():
    spec = CameraSpec(camera_id="C1", name="X", latitude=23.0, longitude=72.5,
                      substream_url="rtsp://10.0.0.1:554/sub",
                      credential_ref="env:DEFINITELY_NOT_SET")
    assert spec.resolve_url("sub") == "rtsp://10.0.0.1:554/sub"


def test_ai_consumes_the_substream_when_one_exists():
    """The single biggest scale lever: the sub-stream costs roughly an
    eighth of the main stream to move and decode."""
    spec = CameraSpec(camera_id="C1", name="X", latitude=23.0, longitude=72.5,
                      stream_url="rtsp://h/main", substream_url="rtsp://h/sub")
    assert spec.ai_url == "rtsp://h/sub"
    assert CameraSpec(camera_id="C2", name="Y", latitude=23.0, longitude=72.5,
                      stream_url="rtsp://h/main").ai_url == "rtsp://h/main"


def test_shipped_camera_config_is_valid_and_empty():
    """config/cameras.yaml ships with every example commented out, so a
    fresh checkout starts in demo mode rather than timing out on somebody
    else's IP addresses."""
    root = pathlib.Path(__file__).resolve().parents[1]
    assert load_from_yaml(root / "config" / "cameras.yaml") == []


# ── stream reader ────────────────────────────────────────────────────

def test_credentials_are_redacted_from_log_output():
    assert redact("rtsp://admin:hunter2@10.42.7.14:554/x") == "rtsp://***:***@10.42.7.14:554/x"
    assert redact("rtsp://10.42.7.14:554/x") == "rtsp://10.42.7.14:554/x"


@pytest.mark.parametrize("url,expected,forbidden", [
    ("rtsp://h/s",           "-rtsp_transport", "-stream_loop"),
    ("https://h/x.m3u8",     "-live_start_index", "-rtsp_transport"),
    ("/tmp/clip.mp4",        "-stream_loop", "-rtsp_transport"),
])
def test_ffmpeg_input_flags_are_protocol_specific(url, expected, forbidden):
    """ffmpeg 6+ hard-fails with 'Option rtsp_transport not found' when the
    flag is passed for a non-RTSP input. That silently kills every
    file-backed and HLS camera, which is most of the demo estate."""
    opts = FrameReader(url)._input_options()
    assert expected in opts
    assert forbidden not in opts


def test_unreachable_stream_fails_without_raising():
    r = FrameReader("rtsp://10.255.255.1:554/nope", width=64, height=48, fps=1)
    r.start()
    import time
    time.sleep(1.5)
    assert r.frames_read == 0
    r.stop()


# ── traffic world ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def world():
    return TrafficWorld(vehicle_count=400, seed=7, speed_multiplier=1.0)


def test_world_spawns_the_requested_fleet(world):
    assert world.stats()["vehicles"] == 400


def test_vehicles_follow_roads_rather_than_straight_lines(world):
    """Movement must obey the road graph. If vehicles drifted freely, the
    spatio-temporal gate would be validated against motion it will never
    see in production."""
    w = TrafficWorld(vehicle_count=20, seed=3, speed_multiplier=1.0)
    v = next(iter(w.vehicles.values()))
    positions = []
    for _ in range(120):
        w.tick(0.5)
        positions.append((v.lat, v.lon))
        if not v.active:
            break
    # Every step must land on or between two junctions of its route.
    from ahmedabad import JUNCTION_BY_CODE
    route_pts = [(JUNCTION_BY_CODE[c].lat, JUNCTION_BY_CODE[c].lon) for c in v.route]
    for lat, lon in positions:
        assert min(haversine_m(lat, lon, a, b) for a, b in route_pts) < 4000


def test_vehicles_move_at_plausible_road_speeds(world):
    w = TrafficWorld(vehicle_count=200, seed=11, speed_multiplier=1.0)
    for _ in range(60):
        w.tick(0.5)
    speeds = [v.speed_kmph for v in w.vehicles.values() if v.active and v.speed_kmph > 0]
    assert speeds
    assert 8 < sum(speeds) / len(speeds) < 75
    assert max(speeds) < 110, "a vehicle exceeded any plausible urban speed"


def test_some_vehicles_wait_at_junctions():
    """Signals and congestion are what stretch real arrival times, and are
    why the gate's late bound is far wider than its early one."""
    w = TrafficWorld(vehicle_count=300, seed=5, speed_multiplier=2.0)
    for _ in range(400):
        w.tick(0.5)
    assert w.stats()["stopped"] > 0


def test_camera_sees_only_what_is_in_range_and_in_frame(world):
    from ahmedabad import JUNCTION_BY_CODE
    j = JUNCTION_BY_CODE["J01"]
    objs = world.observe(camera_lat=j.lat, camera_lon=j.lon, heading_deg=47,
                         fov_deg=82, range_m=65, frame_width=1280, frame_height=720)
    for o in objs:
        assert o.distance_m <= 65
        assert 0 <= o.bbox.x < 1280 * 1.6
        assert o.bbox.w > 0 and o.bbox.h > 0


def test_apparent_size_shrinks_with_distance():
    """Boxes must obey optics. Without this the quality gate never fires and
    ANPR appears to work at any range, which is the single most misleading
    thing a surveillance demo can do."""
    w = TrafficWorld(vehicle_count=1, seed=2, speed_multiplier=1.0)
    v = next(iter(w.vehicles.values()))
    v.lat, v.lon, v.width_m, v.height_m = 23.0, 72.5, 1.75, 1.5
    w._grid.clear()
    w._grid.setdefault(w._cell(v.lat, v.lon), set()).add(v.identity)

    from sentinel_core.geo import destination
    widths = []
    for d in (10, 25, 50):
        clat, clon = destination(23.0, 72.5, 180, d)     # camera d metres away
        objs = w.observe(camera_lat=clat, camera_lon=clon, heading_deg=0,
                         fov_deg=90, range_m=80, frame_width=1280, frame_height=720)
        widths.append(objs[0].bbox.w if objs else 0)
    assert widths[0] > widths[1] > widths[2] > 0, widths


def test_crowded_scenes_produce_occlusion(world):
    from ahmedabad import JUNCTION_BY_CODE
    j = JUNCTION_BY_CODE["J18"]
    for _ in range(50):
        world.tick(0.5)
        objs = world.observe(camera_lat=j.lat, camera_lon=j.lon, heading_deg=None,
                             fov_deg=360, range_m=120,
                             frame_width=1280, frame_height=720)
        if len(objs) >= 4:
            assert objs[0].occlusion == 0.0            # nearest is unobstructed
            assert objs[-1].occlusion > 0.0            # furthest is behind others
            return
    pytest.skip("no crowded frame occurred in this window")


def test_target_vehicle_gets_a_long_deterministic_route():
    w = TrafficWorld(vehicle_count=10, seed=1)
    t = w.add_target_vehicle(plate="GJ01AB1234")
    assert t.plate == "GJ01AB1234"
    assert t.is_target and len(t.route) >= 10
    assert w.vehicles["GT-TARGET"] is t


def test_plates_are_unique_across_the_fleet():
    w = TrafficWorld(vehicle_count=500, seed=13)
    plates = [v.plate for v in w.vehicles.values()]
    assert len(plates) == len(set(plates))


def test_world_is_reproducible_for_a_given_seed():
    """A demo that behaves differently on each run cannot be rehearsed."""
    a = TrafficWorld(vehicle_count=50, seed=99)
    b = TrafficWorld(vehicle_count=50, seed=99)
    for _ in range(40):
        a.tick(0.5)
        b.tick(0.5)
    assert ([(round(v.lat, 6), round(v.lon, 6)) for v in a.vehicles.values()]
            == [(round(v.lat, 6), round(v.lon, 6)) for v in b.vehicles.values()])


# ── supervisor end-to-end ────────────────────────────────────────────

def _specs(n=12):
    from ahmedabad import JUNCTIONS
    return [CameraSpec(camera_id=f"T-{i:03d}", name=j.name,
                       latitude=j.lat, longitude=j.lon,
                       heading_deg=45.0, fov_deg=90, range_m=70,
                       width=1920, height=1080,
                       anpr_capable=(i % 3 == 0), protocol=Protocol.SIMULATED)
            for i, j in enumerate(JUNCTIONS[:n])]


def test_supervisor_publishes_sightings_to_the_bus():
    async def run():
        bus = create_bus("memory")
        await bus.connect()
        sup = IngestionSupervisor(_specs(), bus, mode="demo", tick_hz=6.0,
                                  vehicle_count=600, speed_multiplier=3.0)
        for _ in range(6 * 40):
            await sup._tick()
        return bus, sup

    bus, sup = asyncio.run(run())
    sightings = [m for m in bus.published if m.topic == Topics.SIGHTINGS]
    assert sightings, "no sightings produced"
    assert sup.published_sightings == len(sightings)
    p = sightings[0].payload
    for key in ("camera_id", "track_id", "vehicle_type", "first_seen",
                "last_seen", "latitude", "longitude", "embedding"):
        assert key in p
    assert sightings[0].key == p["camera_id"], "must partition by camera"


def test_only_anpr_capable_cameras_produce_plate_reads():
    """Plate reads are a physical property of the installation. A wide-angle
    surveillance camera cannot resolve a plate at any settings, and a system
    that claimed otherwise would be lying to its operators."""
    async def run():
        bus = create_bus("memory")
        await bus.connect()
        specs = _specs()
        sup = IngestionSupervisor(specs, bus, mode="demo", tick_hz=6.0,
                                  vehicle_count=900, speed_multiplier=3.0)
        for _ in range(6 * 60):
            await sup._tick()
        return bus, {s.camera_id for s in specs if s.anpr_capable}

    bus, anpr_cams = asyncio.run(run())
    with_plate = [m.payload for m in bus.published
                  if m.topic == Topics.SIGHTINGS and m.payload.get("plate")]
    assert with_plate, "no plates read anywhere"
    assert all(p["camera_id"] in anpr_cams for p in with_plate)


def test_supervisor_publishes_health_beacons():
    async def run():
        bus = create_bus("memory")
        await bus.connect()
        sup = IngestionSupervisor(_specs(4), bus, mode="demo", tick_hz=6.0,
                                  vehicle_count=100, health_interval_s=0.0)
        await sup._tick()
        return bus

    bus = asyncio.run(run())
    health = [m for m in bus.published if m.topic == Topics.CAMERA_HEALTH]
    assert len(health) == 4
    assert {"camera_id", "reachable", "fps_actual", "scene_change"} <= set(health[0].payload)


def test_shutdown_flushes_vehicles_still_in_frame():
    """A vehicle mid-pass when the service stops must still produce a
    sighting rather than disappearing from the record."""
    async def run():
        bus = create_bus("memory")
        await bus.connect()
        sup = IngestionSupervisor(_specs(8), bus, mode="demo", tick_hz=6.0,
                                  vehicle_count=800, speed_multiplier=3.0)
        for _ in range(6 * 20):
            await sup._tick()
        before = len([m for m in bus.published if m.topic == Topics.SIGHTINGS])
        await sup.stop()
        after = len([m for m in bus.published if m.topic == Topics.SIGHTINGS])
        return before, after

    before, after = asyncio.run(run())
    assert after > before, "open tracks were discarded on shutdown"


def test_estate_tick_stays_within_budget():
    """Capacity check. If one tick for the whole estate exceeds its budget
    the node is over-subscribed, and that is a sizing signal rather than a
    nuisance."""
    import time

    async def run():
        bus = create_bus("memory")
        await bus.connect()
        sup = IngestionSupervisor(_specs(12), bus, mode="demo", tick_hz=6.0,
                                  vehicle_count=1800, speed_multiplier=3.0)
        for _ in range(20):
            await sup._tick()                       # warm up
        t0 = time.perf_counter()
        for _ in range(60):
            await sup._tick()
        return (time.perf_counter() - t0) / 60

    per_tick = asyncio.run(run())
    assert per_tick < 1.0 / 6.0, f"{per_tick*1000:.1f} ms/tick exceeds the 6 Hz budget"
