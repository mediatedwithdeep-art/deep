"""The demo traffic world.

Vehicles drive the real Ahmedabad road graph at real road speeds. Cameras
observe whatever is inside their field of view.

Why a world simulator rather than canned detections: cross-camera tracking
is only meaningful if a vehicle that leaves camera A genuinely *arrives* at
camera B after a plausible travel time. Replaying independent per-camera
event lists would produce vehicles that teleport, and the spatio-temporal
gate -- the component the whole design leans on -- would either reject
everything or be untested. Here the gate is exercised against motion that
actually obeys the road network, because both read the same graph.

The world is the ground truth. The AI backends convert it into noisy
observations. That split is what lets the demo measure precision and recall
instead of asserting them.
"""

from __future__ import annotations

import math
import random
import sys
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "shared"))
sys.path.insert(0, str(_REPO / "ai"))
sys.path.insert(0, str(_REPO / "database" / "seeds"))

from ahmedabad import RoadGraph, JUNCTION_BY_CODE
from sentinel_core.domain import BoundingBox, VehicleType
from sentinel_core.geo import bearing_deg, haversine_m
from sentinel_ai.detector import SceneObject

# Fleet composition roughly matching Indian urban traffic, and physical
# dimensions that drive apparent size in a camera. Getting these wrong is
# what makes simulated bounding boxes look obviously fake.
VEHICLE_MIX: list[tuple[VehicleType, float, float, float]] = [
    # (type, share, width_m, height_m)
    (VehicleType.CAR,           0.42, 1.75, 1.50),
    (VehicleType.MOTORCYCLE,    0.31, 0.75, 1.30),
    (VehicleType.AUTO_RICKSHAW, 0.14, 1.40, 1.70),
    (VehicleType.TRUCK,         0.06, 2.45, 3.00),
    (VehicleType.BUS,           0.04, 2.55, 3.20),
    (VehicleType.BICYCLE,       0.02, 0.60, 1.10),
    (VehicleType.TRACTOR,       0.01, 2.00, 2.60),
]

# Indian vehicle colour distribution: white dominates by a wide margin,
# which is precisely what makes appearance-only ReID hard and why the
# demo must not pretend otherwise.
COLOUR_MIX: list[tuple[str, float]] = [
    ("white", 0.38), ("silver", 0.15), ("grey", 0.12), ("black", 0.10),
    ("red", 0.07), ("blue", 0.07), ("brown", 0.04), ("yellow", 0.03),
    ("green", 0.02), ("orange", 0.02),
]

STATE_CODES = ["GJ"] * 12 + ["MH", "RJ", "MP", "DL", "KA", "UP", "TN", "HR"]
SERIES = "ABCDEFGHJKLMNPQRSTUVWXYZ"


@dataclass
class SimVehicle:
    identity: str
    vehicle_type: VehicleType
    colour: str
    plate: str
    width_m: float
    height_m: float
    route: list[str]             # junction codes
    leg: int = 0                 # index of current edge in route
    progress_m: float = 0.0      # distance travelled along the current edge
    speed_kmph: float = 30.0
    lat: float = 0.0
    lon: float = 0.0
    heading: float = 0.0
    active: bool = True
    is_target: bool = False      # the vehicle the demo narrative follows
    stopped_until: datetime | None = None

    @property
    def at_end(self) -> bool:
        return self.leg >= len(self.route) - 1


class TrafficWorld:
    def __init__(self, vehicle_count: int = 1800, seed: int = 20260907,
                 start_time: datetime | None = None,
                 time_scale: float = 1.0):
        self.graph = RoadGraph.build()
        self.rng = random.Random(seed)
        self.now = start_time or datetime.now(timezone.utc)
        # Compresses the DEMO CLOCK, not vehicle speed. This distinction is
        # load-bearing and the difference between working and broken
        # cross-camera tracking.
        #
        # Scaling vehicle speed makes vehicles arrive 3x sooner than the
        # road network says they can, so every genuine transition falls
        # outside the early bound of the spatio-temporal gate and is
        # rejected. Cross-camera matching then produces zero matches while
        # every individual component tests green -- the failure looks like a
        # broken matcher and is actually a broken world.
        #
        # Scaling the clock instead advances simulated time and vehicle
        # position by the same factor, so a journey the graph says takes
        # 120 s still takes 120 s of SIMULATED time. The gate stays exact
        # and the demo still fits in a presentation slot.
        self.time_scale = time_scale
        self.vehicles: dict[str, SimVehicle] = {}
        self._counter = 0
        self._plates_used: set[str] = set()
        # Spatial hash. A realistic estate needs a few thousand vehicles for
        # cameras to see traffic at believable rates, and checking every
        # vehicle against every camera would be O(V*C) per tick -- about
        # 100k distance computations at 2 Hz, which Python will not do.
        # Bucketing by ~200 m cell makes each camera check only its own
        # neighbourhood, so cost scales with local density, not fleet size.
        self._grid: dict[tuple[int, int], set[str]] = {}
        self._cell_deg = 0.0018          # ~200 m at Ahmedabad's latitude
        for _ in range(vehicle_count):
            self._spawn()

    # ── vehicle creation ─────────────────────────────────────────────
    def _random_plate(self) -> str:
        while True:
            state = self.rng.choice(STATE_CODES)
            rto = self.rng.randrange(1, 40)
            series = "".join(self.rng.choice(SERIES) for _ in range(2))
            num = self.rng.randrange(1000, 10000)
            plate = f"{state}{rto:02d}{series}{num}"
            if plate not in self._plates_used:
                self._plates_used.add(plate)
                return plate

    def _weighted(self, mix):
        r = self.rng.random()
        acc = 0.0
        for item in mix:
            acc += item[1]
            if r <= acc:
                return item
        return mix[-1]

    def _random_route(self, min_hops: int = 6) -> list[str]:
        """A connected walk through the graph, avoiding immediate backtracks.

        Not shortest-path: real traffic does not all take the same route,
        and a demo where every vehicle follows one corridor would make
        cross-camera matching trivially easy.
        """
        codes = list(self.graph.junctions)
        current = self.rng.choice(codes)
        route = [current]
        previous = None
        for _ in range(self.rng.randint(min_hops, min_hops + 10)):
            options = [n for n, _ in self.graph.neighbours.get(current, [])
                       if n != previous]
            if not options:
                options = [n for n, _ in self.graph.neighbours.get(current, [])]
            if not options:
                break
            previous, current = current, self.rng.choice(options)
            route.append(current)
        return route

    def _spawn(self, is_target: bool = False, plate: str | None = None,
               route: list[str] | None = None) -> SimVehicle:
        self._counter += 1
        vtype, _, w_m, h_m = self._weighted(VEHICLE_MIX)
        colour, _ = self._weighted(COLOUR_MIX)
        v = SimVehicle(
            identity=f"GT-{self._counter:05d}",
            vehicle_type=vtype, colour=colour,
            plate=plate or self._random_plate(),
            width_m=w_m, height_m=h_m,
            route=route or self._random_route(),
            speed_kmph=0.0, is_target=is_target)
        self._place(v)
        self.vehicles[v.identity] = v
        return v

    def add_target_vehicle(self, plate: str = "GJ01AB1234",
                           colour: str = "white",
                           vehicle_type: VehicleType = VehicleType.CAR,
                           route: list[str] | None = None) -> SimVehicle:
        """The vehicle the demo narrative follows.

        Given a long route so it crosses many cameras, which is what makes
        the cross-camera story visible on stage.
        """
        route = route or ["J27", "J25", "J26", "J01", "J07", "J08", "J09",
                          "J12", "J16", "J15", "J17", "J18", "J32", "J34"]
        self._counter += 1
        v = SimVehicle(
            identity="GT-TARGET", vehicle_type=vehicle_type, colour=colour,
            plate=plate, width_m=1.75, height_m=1.50,
            route=route, speed_kmph=0.0, is_target=True)
        self._place(v)
        self.vehicles[v.identity] = v
        return v

    # ── movement ─────────────────────────────────────────────────────
    def _edge(self, v: SimVehicle):
        a, b = v.route[v.leg], v.route[v.leg + 1]
        road = next((r for n, r in self.graph.neighbours[a] if n == b), None)
        return a, b, road

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return int(lat / self._cell_deg), int(lon / self._cell_deg)

    def _reindex(self, v: SimVehicle, old: tuple[int, int] | None) -> None:
        new = self._cell(v.lat, v.lon)
        if old == new:
            return
        if old is not None:
            bucket = self._grid.get(old)
            if bucket:
                bucket.discard(v.identity)
                if not bucket:
                    del self._grid[old]
        self._grid.setdefault(new, set()).add(v.identity)

    def _place(self, v: SimVehicle) -> None:
        """Interpolate the vehicle's position along its current edge."""
        old = self._cell(v.lat, v.lon) if (v.lat or v.lon) else None
        if v.at_end:
            j = JUNCTION_BY_CODE[v.route[-1]]
            v.lat, v.lon = j.lat, j.lon
            self._reindex(v, old)
            return
        a, b, road = self._edge(v)
        ja, jb = JUNCTION_BY_CODE[a], JUNCTION_BY_CODE[b]
        length = self.graph.edge_length_m(a, b)
        t = min(1.0, v.progress_m / length) if length else 1.0
        v.lat = ja.lat + (jb.lat - ja.lat) * t
        v.lon = ja.lon + (jb.lon - ja.lon) * t
        v.heading = bearing_deg(ja.lat, ja.lon, jb.lat, jb.lon)
        self._reindex(v, old)
        if road is not None and v.speed_kmph == 0.0:
            # Drivers vary; a fleet all doing exactly the posted speed would
            # make the gate's time windows unrealistically easy to satisfy.
            v.speed_kmph = road.speed_kmph * self.rng.uniform(0.55, 1.25)

    def tick(self, dt_seconds: float) -> None:
        """Advance the world by `dt_seconds` of wall time.

        Simulated time and vehicle movement both advance by
        dt_seconds * time_scale, so they never disagree.
        """
        dt = dt_seconds * self.time_scale
        self.now += timedelta(seconds=dt)
        for v in list(self.vehicles.values()):
            if not v.active:
                continue
            if v.stopped_until and self.now < v.stopped_until:
                continue
            v.stopped_until = None
            if v.at_end:
                # Retire and replace, keeping the fleet size stable.
                v.active = False
                if not v.is_target:
                    bucket = self._grid.get(self._cell(v.lat, v.lon))
                    if bucket:
                        bucket.discard(v.identity)
                    del self.vehicles[v.identity]
                    self._spawn()
                continue

            a, b, road = self._edge(v)
            length = self.graph.edge_length_m(a, b)
            v.progress_m += (v.speed_kmph * 1000 / 3600) * dt

            if v.progress_m >= length:
                v.progress_m -= length
                v.leg += 1
                v.speed_kmph = 0.0        # re-drawn for the next road
                # Signals and congestion: a fraction of vehicles wait at a
                # junction. This is what stretches real arrival times, and
                # why the gate's late bound is far wider than its early one.
                if self.rng.random() < 0.22:
                    # Dwell is in simulated seconds, so it scales with the
                    # clock automatically -- no division here.
                    v.stopped_until = self.now + timedelta(
                        seconds=self.rng.uniform(8, 55))
            self._place(v)

    # ── observation ──────────────────────────────────────────────────
    def observe(self, *, camera_lat: float, camera_lon: float,
                heading_deg: float | None, fov_deg: float, range_m: float,
                frame_width: int, frame_height: int,
                max_objects: int = 12) -> list[SceneObject]:
        """What this camera can see right now, as ground-truth SceneObjects.

        Apparent size is computed from real optics: an object of width W at
        distance d subtends W / (2 d tan(fov/2)) of the frame. Without this
        the boxes would not shrink with distance, the quality gate would
        never fire, and ANPR would appear to work at any range.
        """
        out: list[SceneObject] = []
        half_fov = fov_deg / 2.0
        # Horizontal extent of the scene at 1 m, used for the size formula.
        tan_half = math.tan(math.radians(min(half_fov, 85.0)))

        # Only vehicles in the cells overlapping this camera's range.
        span = int(range_m / (self._cell_deg * 111_320)) + 1
        clat, clon = self._cell(camera_lat, camera_lon)
        nearby: list[SimVehicle] = []
        for di in range(-span, span + 1):
            for dj in range(-span, span + 1):
                for vid in self._grid.get((clat + di, clon + dj), ()):
                    v = self.vehicles.get(vid)
                    if v is not None:
                        nearby.append(v)

        for v in nearby:
            if not v.active:
                continue
            d = haversine_m(camera_lat, camera_lon, v.lat, v.lon)
            if d > range_m or d < 1.0:
                continue
            if heading_deg is not None:
                b = bearing_deg(camera_lat, camera_lon, v.lat, v.lon)
                if abs((b - heading_deg + 180) % 360 - 180) > half_fov:
                    continue

            scene_width_m = 2.0 * d * tan_half
            px_w = max(6, int(frame_width * v.width_m / max(scene_width_m, 0.5)))
            px_h = max(6, int(px_w * (v.height_m / v.width_m)))
            if px_w > frame_width * 1.6:
                continue                                # effectively on top of the lens

            # Lateral offset within the frame, from the bearing difference.
            if heading_deg is not None:
                b = bearing_deg(camera_lat, camera_lon, v.lat, v.lon)
                off = ((b - heading_deg + 180) % 360 - 180) / half_fov
            else:
                off = 0.0
            cx = int(frame_width * (0.5 + 0.5 * off))
            # Nearer vehicles sit lower in the frame -- the usual perspective
            # cue, and it keeps boxes from all stacking on one row.
            cy = int(frame_height * (0.35 + 0.5 * (1.0 - min(1.0, d / range_m))))

            out.append(SceneObject(
                identity=v.identity, vehicle_type=v.vehicle_type,
                colour=v.colour, plate=v.plate,
                bbox=BoundingBox(x=max(0, cx - px_w // 2), y=max(0, cy - px_h // 2),
                                 w=px_w, h=px_h),
                distance_m=d,
                # Crowded scenes occlude. Sorted by distance below, so the
                # further a vehicle is down the list the more likely it is
                # hidden behind something nearer.
                occlusion=0.0,
                speed_kmph=round(v.speed_kmph, 1),
                heading_deg=round(v.heading, 1),
                latitude=v.lat, longitude=v.lon))

        out.sort(key=lambda o: o.distance_m)
        for i, o in enumerate(out):
            if i >= 2:
                o.occlusion = min(0.85, 0.12 * (i - 1))
        return out[:max_objects]

    def stats(self) -> dict:
        active = [v for v in self.vehicles.values() if v.active]
        return {
            "vehicles": len(active),
            "sim_time": self.now.isoformat(),
            "mean_speed_kmph": round(
                sum(v.speed_kmph for v in active) / max(len(active), 1), 1),
            "stopped": sum(1 for v in active if v.stopped_until),
        }
