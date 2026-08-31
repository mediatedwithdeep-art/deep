"""Per-camera worker: pixels (or simulated scene) in, Sightings out.

One worker per camera. Workers share no mutable state, so scaling out is
purely a matter of running more processes -- which is what makes the same
code serve 50 cameras on a laptop and 80,000 across a fleet.

A worker never raises into the supervisor. A camera that is unreachable,
returning corrupt frames, or has a drifting clock is a *normal* condition
in an estate this heterogeneous, not an exception: roughly a fifth of a
real government estate is broken at any moment. The worker records the
failure, reports it in its health beacon, and keeps going.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sentinel_core.domain import CameraHealth, CameraStatus, Sighting
from sentinel_core.log import get_logger
from sentinel_ai.detector import Detector, SceneObject
from sentinel_ai.pipeline import CameraConfig, CameraPipeline

from .camera_config import CameraSpec
from .live_reader import LiveStreamReader

log = get_logger("sentinel.ingest.worker")


@dataclass
class WorkerStats:
    frames: int = 0
    sightings: int = 0
    errors: int = 0
    reconnects: int = 0
    discontinuities: int = 0
    last_frame_at: float = 0.0
    last_scene_size: int = 0
    scene_change: float = 1.0
    started_at: float = field(default_factory=time.time)


class CameraWorker:
    def __init__(self, spec: CameraSpec, detector: Detector,
                 *, live: bool = False, target_fps: float = 6.0,
                 recognizer=None, reid=None):
        self.spec = spec
        self.live = live
        self.stats = WorkerStats()
        self._reader: LiveStreamReader | None = None
        self._prev_luma: float | None = None
        self._frozen_samples = 0

        self.pipeline = CameraPipeline(
            CameraConfig(
                camera_id=spec.camera_id,
                latitude=spec.latitude, longitude=spec.longitude,
                heading_deg=spec.heading_deg,
                anpr_capable=spec.anpr_capable,
                width=spec.width, height=spec.height, fps=spec.fps,
                target_fps=target_fps),
            detector, recognizer=recognizer, reid=reid)

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.live:
            return
        url = self.spec.resolve_url("main" if self.spec.anpr_capable else "sub")
        if not url:
            log.warning("camera has no stream URL", extra={"camera_id": self.spec.camera_id})
            return
        # ANPR lanes need the main stream's resolution to resolve a plate.
        # Everything else runs on the sub-stream, which is ~8x cheaper.
        w, h = (960, 540) if self.spec.anpr_capable else (640, 360)
        # LiveStreamReader, not FrameReader: it is the reader that carries a
        # PTS-derived capture time and a discontinuity flag on every frame.
        # FrameReader hands back bare pixels, and a worker holding bare
        # pixels has no honest timestamp to give the pipeline.
        self._reader = LiveStreamReader(
            url, camera_id=self.spec.camera_id, width=w, height=h,
            expected_fps=self.spec.fps)
        self._reader.start()

    def stop(self) -> list[Sighting]:
        if self._reader:
            self._reader.stop()
        return self.pipeline.flush()

    # ── one step ─────────────────────────────────────────────────────
    def step(self, now: datetime,
             scene: list[SceneObject] | None = None,
             is_night: bool = False) -> tuple[list[Sighting], bool]:
        """Advance this camera by one tick. Returns (sightings, healthy)."""
        frame = None
        healthy = True
        discontinuous = False
        # Simulation drives the clock from outside; a live camera drives it
        # from its own stream. Mixing the two is the defect PART 6 exists to
        # remove, so the live branch overwrites `now` from the frame's PTS.
        capture_time = now

        if self.live and self._reader is not None:
            live_frame = self._reader.read()
            if live_frame is None:
                healthy = False
            elif self._reader.is_stalled:
                healthy = False
                self.stats.errors += 1
            else:
                frame = live_frame.image
                capture_time = live_frame.capture_time
                discontinuous = live_frame.is_discontinuity
                self.stats.last_frame_at = time.time()
                self.stats.discontinuities += int(discontinuous)
                self._measure_scene_change(frame)

        if frame is None and not scene:
            return [], healthy

        try:
            _dets, sightings = self.pipeline.process(
                capture_time, frame=frame, scene=scene, is_night=is_night,
                is_discontinuity=discontinuous)
        except Exception as e:
            # One bad frame must never take down a worker, and one worker
            # must never take down the estate.
            self.stats.errors += 1
            log.exception("pipeline error", extra={"camera_id": self.spec.camera_id,
                                                   "error": str(e)})
            return [], False

        self.stats.frames += 1
        self.stats.sightings += len(sightings)
        self.stats.last_scene_size = len(scene or [])
        return sightings, healthy

    def _measure_scene_change(self, frame) -> None:
        """Detect a frozen picture.

        A live socket delivering an unchanging image is the most common
        silent camera failure in the field, and every other health signal
        looks perfect while it happens. Comparing mean luma between frames
        is crude but catches it, and costs nothing.
        """
        luma = float(frame.mean())
        if self._prev_luma is not None:
            delta = abs(luma - self._prev_luma) / max(self._prev_luma, 1.0)
            # Exponential moving average: a genuinely static scene at 03:00
            # should not be flagged on the strength of two identical frames.
            self.stats.scene_change = 0.7 * self.stats.scene_change + 0.3 * delta
            self._frozen_samples = self._frozen_samples + 1 if delta < 0.0005 else 0
        self._prev_luma = luma

    # ── health ───────────────────────────────────────────────────────
    def health(self) -> CameraHealth:
        reader = self._reader
        reachable = True
        message = None

        if self.live:
            if reader is None:
                reachable, message = False, "no stream URL configured"
            elif reader.health.status is CameraStatus.OFFLINE:
                reachable, message = False, (reader.health.last_error or "offline")[:200]
            elif reader.health.status is CameraStatus.RECONNECTING:
                # Distinct from OFFLINE on purpose: a camera in backoff is
                # coming back, and reporting it as gone starts a callout.
                reachable = True
                message = (f"reconnecting (attempt {reader.health.reconnects}, "
                           f"retry in {reader.health.next_retry_in_s:.0f}s)")
            elif reader.health.frames == 0 and reader.health.last_error:
                reachable, message = False, reader.health.last_error[:200]
            elif reader.is_stalled:
                reachable, message = False, "stream stalled"
        if self._frozen_samples >= 5:
            message = "picture frozen (stream healthy, image static)"

        uptime = max(time.time() - self.stats.started_at, 1e-6)
        return CameraHealth(
            camera_id=self.spec.camera_id,
            timestamp=datetime.now(timezone.utc),
            reachable=reachable,
            fps_actual=round(self.pipeline.stats.frames_processed / uptime, 2),
            frames_decoded=reader.health.frames if reader else self.stats.frames,
            decode_errors=(reader.health.reconnects if reader else 0) + self.stats.errors,
            scene_change=round(self.stats.scene_change, 5),
            inference_ms=round(self.pipeline.stats.mean_latency_ms, 3),
            queue_depth=len(self.pipeline.tracker.tracks),
            message=message,
        )
