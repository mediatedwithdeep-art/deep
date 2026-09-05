#!/usr/bin/env python3
"""Event processor service entrypoint.

    python event-processor/service.py

Consumes sightings and health beacons from the bus, assigns cross-camera
vehicle identity, persists everything, evaluates alert rules, and publishes
alerts back to the bus for the API to fan out over WebSocket.
"""

from __future__ import annotations

import asyncio
import pathlib
import signal
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
for sub in ("shared", "ai", "event-processor"):
    sys.path.insert(0, str(_REPO / sub))

from sentinel_core.bus import create_bus, Topics
from sentinel_core.config import get_settings
from sentinel_core.domain import CameraHealth, Sighting
from sentinel_core.log import configure_logging, get_logger

from processor.alerts import AlertEngine
from processor.matcher import CrossCameraMatcher
from processor.pipeline import EventProcessor
from processor.store import Store

log = get_logger("sentinel.processor")

GROUP = "sentinel-event-processor"


async def main() -> int:
    s = get_settings()
    configure_logging("event-processor", s.log_level, s.log_format)

    store = Store(s.database_url)
    store.connect()
    store.ensure_partitions()

    bus = create_bus(s.bus_backend, redis_url=s.redis_url,
                     kafka_bootstrap=s.kafka_bootstrap)
    await bus.connect()

    processor = EventProcessor(
        store, bus,
        matcher=CrossCameraMatcher(store, track_ttl_seconds=s.matcher_track_ttl_seconds),
        alert_engine=AlertEngine(store, default_dedup_seconds=s.alert_dedup_seconds))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())

    sighting_batch: list[Sighting] = []
    health_batch: list[CameraHealth] = []
    pending: list = []

    async def consume():
        nonlocal sighting_batch, health_batch, pending
        async for msg in bus.subscribe(
                [Topics.SIGHTINGS, Topics.CAMERA_HEALTH], GROUP, "worker-1"):
            try:
                if msg.topic == Topics.SIGHTINGS:
                    sighting_batch.append(Sighting.model_validate(msg.payload))
                else:
                    health_batch.append(CameraHealth.model_validate(msg.payload))
                pending.append(msg)
            except Exception as e:
                # A malformed message must be acked, or it blocks the whole
                # consumer group forever. Record it; do not pretend it worked.
                log.error("undecodable message", extra={"error": str(e),
                                                        "topic": msg.topic})
                await bus.ack(msg, GROUP)
            if stop.is_set():
                return

    async def flush_loop():
        nonlocal sighting_batch, health_batch, pending
        while not stop.is_set():
            await asyncio.sleep(s.matcher_tick_seconds)
            if not sighting_batch and not health_batch:
                continue
            sightings, sighting_batch = sighting_batch, []
            health, health_batch = health_batch, []
            acks, pending = pending, []
            try:
                _outcomes, alerts = processor.process_sightings(sightings)
                alerts += processor.process_health(health)
                await processor.publish_alerts(alerts)
                for m in acks:
                    await bus.ack(m, GROUP)
                if alerts:
                    log.info("alerts raised", extra={"count": len(alerts)})
            except Exception as e:
                # Deliberately do NOT ack on failure: the batch is redelivered
                # rather than silently lost. At-least-once beats at-most-once
                # for evidence.
                processor.stats.errors += 1
                log.exception("batch failed; will be redelivered",
                              extra={"error": str(e), "batch": len(sightings)})

    consumer = asyncio.create_task(consume())
    flusher = asyncio.create_task(flush_loop())
    log.info("event processor running", extra={"group": GROUP})

    await stop.wait()
    for t in (consumer, flusher):
        t.cancel()
    log.info("event processor stopped", extra=processor.stats.snapshot())
    store.close()
    await bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
