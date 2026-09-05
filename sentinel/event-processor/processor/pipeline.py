"""Event-processing pipeline: bus -> match -> persist -> alert -> bus.

Batching is the whole performance story. Sightings are drained from the
bus into a batch, matched as one assignment problem, and written with
executemany. Processing one sighting at a time would spend its life on
round trips and would also make the Hungarian assignment meaningless --
optimal assignment across a batch is exactly what greedy per-sighting
matching gets wrong at a junction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sentinel_core.bus import Bus, Topics
from sentinel_core.domain import Alert, CameraHealth, Sighting
from sentinel_core.log import get_logger

from .alerts import AlertEngine
from .matcher import CrossCameraMatcher, MatchOutcome
from .store import Store

log = get_logger("sentinel.processor")


@dataclass
class ProcessorStats:
    batches: int = 0
    sightings: int = 0
    alerts: int = 0
    health_beacons: int = 0
    db_write_ms: float = 0.0
    match_ms: float = 0.0
    alert_ms: float = 0.0
    errors: int = 0

    def snapshot(self) -> dict:
        return {
            "batches": self.batches, "sightings": self.sightings,
            "alerts": self.alerts, "health_beacons": self.health_beacons,
            "errors": self.errors,
            "mean_match_ms": round(self.match_ms / max(self.batches, 1), 2),
            "mean_write_ms": round(self.db_write_ms / max(self.batches, 1), 2),
            "mean_alert_ms": round(self.alert_ms / max(self.batches, 1), 2),
        }


class EventProcessor:
    def __init__(self, store: Store, bus: Bus, *,
                 matcher: CrossCameraMatcher | None = None,
                 alert_engine: AlertEngine | None = None,
                 batch_size: int = 200):
        self.store = store
        self.bus = bus
        self.matcher = matcher or CrossCameraMatcher(store)
        self.alerts = alert_engine or AlertEngine(store)
        self.batch_size = batch_size
        self.stats = ProcessorStats()

    # ── one batch ────────────────────────────────────────────────────
    def process_sightings(self, sightings: list[Sighting]) -> tuple[list[MatchOutcome], list[Alert]]:
        if not sightings:
            return [], []
        self.stats.batches += 1
        self.stats.sightings += len(sightings)

        t0 = time.perf_counter()
        outcomes = self.matcher.process_batch(sightings)
        self.stats.match_ms += (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        self._persist(outcomes)
        self.stats.db_write_ms += (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        alerts = self.alerts.evaluate(outcomes)
        self.stats.alert_ms += (time.perf_counter() - t0) * 1000
        self._persist_alerts(alerts)
        self.stats.alerts += len(alerts)
        return outcomes, alerts

    def _persist(self, outcomes: list[MatchOutcome]) -> None:
        sighting_rows: list[tuple] = []
        plate_rows: list[tuple] = []
        moved: set[str] = set()

        for o in outcomes:
            s = o.sighting
            cam = self.store.camera(s.camera_id)
            if cam is None:
                # An unknown camera means the registry and the ingestion
                # config disagree. Drop the row rather than writing an
                # orphan the UI cannot resolve, and say so.
                log.warning("sighting from unknown camera",
                            extra={"camera_id": s.camera_id})
                self.stats.errors += 1
                continue
            uuid = cam["id"]
            p = s.plate
            sighting_rows.append((
                s.sighting_id, s.timestamp, s.first_seen, s.last_seen, uuid,
                s.camera_id, o.vehicle_track_id, s.track_id,
                s.vehicle_type.value, None, s.vehicle_color, s.color_confidence,
                p.raw_plate if p else None, p.normalized_plate if p else None,
                p.confidence if p else None, p.valid_format if p else False,
                str(s.embedding) if s.embedding else None, s.embedding_model,
                s.latitude, s.longitude, s.longitude, s.latitude,
                s.heading_deg, s.speed_kmph, s.detection_count,
                s.best_quality, s.clock_confidence))
            if p:
                plate_rows.append((
                    s.timestamp, uuid, s.camera_id, s.sighting_id,
                    o.vehicle_track_id, p.raw_plate, p.normalized_plate,
                    p.normalized_plate, p.confidence, p.normalized_plate,
                    p.corrected, p.plate_width_px, p.normalized_plate,
                    s.latitude, s.longitude))
            if not o.is_new:
                moved.add(o.vehicle_track_id)

        self.store.insert_sightings(sighting_rows)
        self.store.insert_plate_reads(plate_rows)

        for o in outcomes:
            cam = self.store.camera(o.sighting.camera_id)
            if cam is None:
                continue
            self.store.upsert_vehicle(vehicle_track_id=o.vehicle_track_id,
                                      sighting=o.sighting, camera_uuid=cam["id"],
                                      is_new=o.is_new)

        self.store.insert_track_links([o.link for o in outcomes if o.link])
        self.store.refresh_vehicle_paths(moved)

    def _persist_alerts(self, alerts: list[Alert]) -> None:
        if not alerts:
            return
        import json
        rows = []
        for a in alerts:
            cam = self.store.camera(a.camera_id) if a.camera_id else None
            rows.append((
                a.alert_id, a.timestamp, a.alert_type.value, a.severity.value,
                a.title, a.message, cam["id"] if cam else None, a.camera_id,
                a.camera_name, a.vehicle_track_id, a.sighting_id, a.plate,
                a.latitude, a.longitude, a.longitude, a.latitude,
                a.confidence, json.dumps(a.evidence), a.dedup_key))
        self.store.insert_alerts(rows)

    # ── health beacons ───────────────────────────────────────────────
    def process_health(self, beacons: list[CameraHealth]) -> list[Alert]:
        if not beacons:
            return []
        self.stats.health_beacons += len(beacons)
        rows: list[tuple] = []
        alerts: list[Alert] = []

        for h in beacons:
            cam = self.store.camera(h.camera_id)
            if cam is None:
                continue
            rows.append((h.timestamp, cam["id"], h.camera_id, h.reachable,
                         h.fps_actual, h.frames_decoded, h.decode_errors,
                         h.scene_change, h.mean_luma, h.blur_variance,
                         h.latency_ms, h.clock_offset_ms, h.inference_ms,
                         h.queue_depth, h.message))
            self.store.update_camera_status(h.camera_id, online=h.reachable,
                                            message=h.message)
            if h.reachable and h.scene_change is not None:
                a = self.alerts.frozen_camera_alert(
                    h.camera_id, cam["name"], h.scene_change,
                    cam["latitude"], cam["longitude"])
                if a:
                    alerts.append(a)

        self.store.insert_health(rows)
        alerts.extend(self.alerts.camera_health_alerts())
        self._persist_alerts(alerts)
        self.stats.alerts += len(alerts)
        return alerts

    async def publish_alerts(self, alerts: list[Alert]) -> None:
        for a in alerts:
            await self.bus.publish(Topics.ALERTS, a.model_dump(mode="json"),
                                   key=a.vehicle_track_id or a.camera_id or "system")
