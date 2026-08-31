"""Ingestion supervisor: drives every camera worker and publishes results.

Three modes, chosen by INGEST_MODE:

    demo   -- the traffic world drives every camera. No cameras needed.
    live   -- real streams only.
    hybrid -- real cameras where configured, simulated for the rest. This
              is the realistic path for the competition: run the handful of
              feeds you actually have alongside a full 50-camera estate,
              and a venue network failure cannot end the demonstration.

Workers are stepped cooperatively in one asyncio task rather than one
thread per camera. At 50 cameras and 6 fps the whole estate costs ~30 ms
per tick, so threads would add contention and context-switching for no
benefit. Live frame *reading* is already off-thread inside FrameReader,
which is where the blocking actually happens.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from sentinel_core.bus import Bus, Topics
from sentinel_core.domain import Sighting
from sentinel_core.log import get_logger, set_camera_id
from sentinel_ai.backends.simulation import SimulationDetector
from sentinel_ai.detector import Detector

from .camera_config import CameraSpec
from .worker import CameraWorker
from .world import TrafficWorld

log = get_logger("sentinel.ingest.supervisor")


class IngestionSupervisor:
    def __init__(self, specs: list[CameraSpec], bus: Bus, *,
                 mode: str = "demo",
                 tick_hz: float = 6.0,
                 target_fps: float = 6.0,
                 vehicle_count: int = 1800,
                 time_scale: float = 3.0,
                 detector: Detector | None = None,
                 health_interval_s: float = 30.0,
                 seed: int = 20260907):
        self.specs = specs
        self.bus = bus
        self.mode = mode
        self.tick_hz = tick_hz
        self.health_interval_s = health_interval_s
        self._running = False
        self._last_health = 0.0
        self.ticks = 0
        self.published_sightings = 0

        detector = detector or SimulationDetector(seed=seed)
        self.world: TrafficWorld | None = None
        if mode in ("demo", "hybrid"):
            self.world = TrafficWorld(vehicle_count=vehicle_count, seed=seed,
                                      time_scale=time_scale)

        self.workers: dict[str, CameraWorker] = {}
        for spec in specs:
            # In hybrid mode a camera is live only if it has a real URL that
            # is not one of the seeded placeholders.
            live = mode == "live" or (mode == "hybrid" and self._is_real(spec))
            self.workers[spec.camera_id] = CameraWorker(
                spec, detector, live=live, target_fps=target_fps)

        live_n = sum(1 for w in self.workers.values() if w.live)
        log.info("supervisor ready",
                 extra={"mode": mode, "cameras": len(self.workers),
                        "live": live_n, "simulated": len(self.workers) - live_n})

    @staticmethod
    def _is_real(spec: CameraSpec) -> bool:
        url = spec.ai_url or ""
        # 10.42.x is the seeded demo address space; treat it as not-real so
        # `hybrid` does not spend 40 seconds timing out on 50 fake hosts.
        return bool(url) and "10.42." not in url

    # ── the loop ─────────────────────────────────────────────────────
    async def run(self) -> None:
        self._running = True
        for w in self.workers.values():
            w.start()

        interval = 1.0 / self.tick_hz
        log.info("ingestion started", extra={"tick_hz": self.tick_hz})

        while self._running:
            t0 = time.perf_counter()
            await self._tick()
            elapsed = time.perf_counter() - t0
            if elapsed > interval:
                # Falling behind is a capacity signal, not a nuisance: it
                # means this node needs fewer cameras or more CPU.
                log.warning("tick overran its budget",
                            extra={"elapsed_ms": round(elapsed * 1000, 1),
                                   "budget_ms": round(interval * 1000, 1),
                                   "cameras": len(self.workers)})
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _tick(self) -> None:
        self.ticks += 1
        now = datetime.now(timezone.utc)

        if self.world is not None:
            self.world.tick(1.0 / self.tick_hz)
            now = self.world.now

        is_night = not (6 <= now.hour < 19)
        batch: list[Sighting] = []

        for camera_id, worker in self.workers.items():
            set_camera_id(camera_id)
            scene = None
            if self.world is not None and not worker.live:
                spec = worker.spec
                scene = self.world.observe(
                    camera_lat=spec.latitude, camera_lon=spec.longitude,
                    heading_deg=spec.heading_deg, fov_deg=spec.fov_deg,
                    range_m=spec.range_m,
                    frame_width=spec.width, frame_height=spec.height)
            sightings, _healthy = worker.step(now, scene=scene, is_night=is_night)
            batch.extend(sightings)
        set_camera_id(None)

        for s in batch:
            await self.bus.publish(Topics.SIGHTINGS, s.model_dump(mode="json"),
                                   key=s.camera_id)
        self.published_sightings += len(batch)

        if time.time() - self._last_health >= self.health_interval_s:
            await self._publish_health()
            self._last_health = time.time()

    async def _publish_health(self) -> None:
        for worker in self.workers.values():
            await self.bus.publish(Topics.CAMERA_HEALTH,
                                   worker.health().model_dump(mode="json"),
                                   key=worker.spec.camera_id)

    async def stop(self) -> None:
        self._running = False
        # Close every open track so vehicles in flight still produce
        # sightings rather than vanishing on shutdown.
        for worker in self.workers.values():
            for s in worker.stop():
                await self.bus.publish(Topics.SIGHTINGS, s.model_dump(mode="json"),
                                       key=s.camera_id)
        await self._publish_health()
        log.info("ingestion stopped",
                 extra={"ticks": self.ticks, "sightings": self.published_sightings})

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "ticks": self.ticks,
            "cameras": len(self.workers),
            "live_cameras": sum(1 for w in self.workers.values() if w.live),
            "published_sightings": self.published_sightings,
            "world": self.world.stats() if self.world else None,
            "mean_inference_ms": round(sum(
                w.pipeline.stats.mean_latency_ms for w in self.workers.values())
                / max(len(self.workers), 1), 3),
        }
