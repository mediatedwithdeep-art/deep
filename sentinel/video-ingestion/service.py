#!/usr/bin/env python3
"""Video ingestion service entrypoint.

    python video-ingestion/service.py

Reads its camera list from the database registry, overlaid by
config/cameras.yaml, and publishes Sightings and CameraHealth to the bus.
Runs in demo, live or hybrid mode -- see INGEST_MODE.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import signal
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
for sub in ("shared", "ai", "video-ingestion", "database/seeds"):
    sys.path.insert(0, str(_REPO / sub))

from sentinel_core.bus import create_bus
from sentinel_core.config import get_settings
from sentinel_core.log import configure_logging, get_logger

from ingestion.camera_config import load_cameras
from ingestion.supervisor import IngestionSupervisor

log = get_logger("sentinel.ingest")


async def main() -> int:
    s = get_settings()
    configure_logging("ingestion", s.log_level, s.log_format)

    cameras = load_cameras(
        dsn=s.database_url,
        yaml_path=str(_REPO / s.camera_config_path),
        environment=s.environment)
    if not cameras:
        log.error("no cameras configured. Run the seeder, or add cameras to "
                  "config/cameras.yaml")
        return 1

    bus = create_bus(s.bus_backend, redis_url=s.redis_url,
                     kafka_bootstrap=s.kafka_bootstrap)
    await bus.connect()

    supervisor = IngestionSupervisor(
        cameras, bus,
        mode=s.ingest_mode,
        tick_hz=s.demo_tick_hz,
        target_fps=s.ai_target_fps,
        vehicle_count=s.demo_vehicle_count,
        speed_multiplier=getattr(s, "demo_speed_multiplier", 3.0))

    # The demo narrative needs a vehicle to follow. Seeding it here rather
    # than hoping a random one happens to cross enough cameras is what makes
    # the presentation repeatable.
    if supervisor.world is not None:
        target_plate = os.environ.get("DEMO_TARGET_PLATE", "GJ01AB1234")
        supervisor.world.add_target_vehicle(plate=target_plate)
        log.info("demo target vehicle seeded", extra={"plate": target_plate})

    stop = asyncio.Event()

    def _shutdown(*_):
        log.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            signal.signal(sig, _shutdown)

    runner = asyncio.create_task(supervisor.run())
    await stop.wait()
    await supervisor.stop()
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass
    await bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
