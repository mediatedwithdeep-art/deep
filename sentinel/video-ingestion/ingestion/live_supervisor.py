"""Live estate supervisor: catalogue → paced connections → readers → health.

CONNECTION PACING (PART 12)
───────────────────────────
Fifty workers opening fifty RTSP sessions in the same instant is a burst,
not a load. A real gateway sees one client mount its entire estate
simultaneously and may throttle, queue or ban it — and on a restart loop
that burst repeats. So opens are staggered and capped:

  * `stagger_ms`  spaces the *start* of each connection attempt
  * `max_concurrent_opens` bounds how many are mid-handshake at once

Both are configuration, because the right values depend on the gateway and
neither can be guessed from here.

ONE CAPTURE PER CAMERA
──────────────────────
Exactly one reader exists per camera_id, held in this registry. Two
consumers of the same camera share the one decode rather than opening a
second session — decoding twice doubles the most expensive thing in the
system to save a dictionary lookup.

RECONCILIATION
──────────────
The catalogue is polled; additions start a reader, removals stop and close
one, material changes restart it. A camera whose entry only changed
cosmetically is left alone: tearing down a working stream to apply a
renamed label is a self-inflicted outage.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from sentinel_core.domain import CameraStatus
from sentinel_core.log import get_logger

from .camera_config import CameraSpec
from .live_reader import LiveStreamReader
from .sentinel_catalogue import Reconciliation, load_from_sentinel, reconcile

log = get_logger("sentinel.ingest.live_supervisor")


@dataclass
class EstateHealth:
    total: int = 0
    online: int = 0
    degraded: int = 0
    reconnecting: int = 0
    offline: int = 0
    probing: int = 0
    frames: int = 0
    discontinuities: int = 0
    reconnects: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class LiveEstate:
    """Owns one LiveStreamReader per camera and keeps it matching the catalogue."""

    def __init__(self, *, stagger_ms: int = 200, max_concurrent_opens: int = 8,
                 width: int | None = 640, height: int | None = 360,
                 open_timeout_s: float = 10.0):
        self.stagger_ms = stagger_ms
        self.max_concurrent_opens = max_concurrent_opens
        self.width = width
        self.height = height
        self.open_timeout_s = open_timeout_s

        self.specs: dict[str, CameraSpec] = {}
        self.readers: dict[str, LiveStreamReader] = {}
        self._lock = threading.Lock()
        self._open_slots = threading.Semaphore(max_concurrent_opens)
        self.connect_started_at: dict[str, float] = {}
        # Observability for the cap: a configured limit nobody measures is
        # a comment, not a control.
        self.opens_in_flight = 0
        self.peak_opens_in_flight = 0

    # ── one capture per camera ────────────────────────────────────────

    def reader_for(self, camera_id: str) -> LiveStreamReader | None:
        """The single decode for this camera. Never opens a second session."""
        with self._lock:
            return self.readers.get(camera_id)

    def _start_one(self, spec: CameraSpec, delay_s: float) -> None:
        def run():
            if delay_s:
                time.sleep(delay_s)
            # Bound how many handshakes are in flight at once. A gateway
            # that is slow to answer must not cause the whole estate to sit
            # in a simultaneous connect.
            with self._open_slots:
                with self._lock:
                    self.opens_in_flight += 1
                    self.peak_opens_in_flight = max(
                        self.peak_opens_in_flight, self.opens_in_flight)
                try:
                    self._open(spec)
                finally:
                    with self._lock:
                        self.opens_in_flight -= 1

        threading.Thread(target=run, daemon=True,
                         name=f"open-{spec.camera_id}").start()

    def _open(self, spec: CameraSpec) -> None:
        """Open one camera, holding an open slot for the whole handshake."""
        self.connect_started_at[spec.camera_id] = time.time()
        # The catalogue's HLS URL is carried as a fallback transport. On a
        # network where 8554/TCP is closed -- which the integrator's guide
        # says to expect, and which a government WAN will often be -- the
        # reader rotates to HLS instead of reporting the camera down.
        primary = spec.ai_url or spec.stream_url or ""
        fallbacks = [u for u in (spec.extra or {}).get("hls_url", "") .split()
                     if u and u != primary]
        reader = LiveStreamReader(
            primary,
            camera_id=spec.camera_id,
            width=self.width, height=self.height,
            open_timeout_s=self.open_timeout_s,
            fallback_urls=fallbacks,
            expected_fps=spec.fps)
        with self._lock:
            # Guard against a reconcile having removed it while we were
            # queued behind the semaphore.
            if spec.camera_id not in self.specs:
                return
            self.readers[spec.camera_id] = reader
        reader.start()
        # Hold the slot until the stream is up or has clearly failed, so
        # the cap bounds real concurrent handshakes rather than just the
        # call to start().
        deadline = time.time() + self.open_timeout_s
        while time.time() < deadline:
            if reader.health.status in (
                    CameraStatus.ONLINE, CameraStatus.DEGRADED,
                    CameraStatus.RECONNECTING, CameraStatus.OFFLINE):
                break
            time.sleep(0.05)

    def add(self, specs: list[CameraSpec]) -> None:
        """Start readers for these cameras, paced."""
        for i, spec in enumerate(specs):
            with self._lock:
                if spec.camera_id in self.readers:
                    continue
                self.specs[spec.camera_id] = spec
            self._start_one(spec, delay_s=i * self.stagger_ms / 1000.0)

    def remove(self, camera_ids: list[str]) -> None:
        for cam_id in camera_ids:
            with self._lock:
                reader = self.readers.pop(cam_id, None)
                self.specs.pop(cam_id, None)
            if reader:
                log.info("retiring camera %s", cam_id)
                reader.stop()

    def apply(self, result: Reconciliation) -> None:
        """Make the running estate match the catalogue."""
        if result.is_noop:
            return
        log.info("reconciling estate: %s", result.summary())
        self.remove(result.removed)
        # A material change is a restart: the URL or the geometry moved.
        changed_specs = [spec for spec, _ in result.changed]
        if changed_specs:
            self.remove([s.camera_id for s in changed_specs])
        self.add(result.added + changed_specs)

    def sync(self, catalogue_url: str, token: str | None = None,
             credential_ref: str | None = None) -> Reconciliation:
        specs, _ = load_from_sentinel(catalogue_url, token=token,
                                      credential_ref=credential_ref)
        with self._lock:
            running = dict(self.specs)
        result = reconcile(specs, running)
        with self._lock:
            for s in specs:
                if s.camera_id in self.specs:
                    self.specs[s.camera_id] = s
        self.apply(result)
        return result

    # ── health ────────────────────────────────────────────────────────

    def health(self) -> EstateHealth:
        h = EstateHealth()
        with self._lock:
            readers = list(self.readers.values())
            h.total = len(self.specs)
        for r in readers:
            h.frames += r.health.frames
            h.discontinuities += r.health.discontinuities
            h.reconnects += r.health.reconnects
            match r.health.status:
                case CameraStatus.ONLINE:
                    h.online += 1
                case CameraStatus.DEGRADED:
                    h.degraded += 1
                case CameraStatus.RECONNECTING:
                    h.reconnecting += 1
                case CameraStatus.OFFLINE:
                    h.offline += 1
                case _:
                    h.probing += 1
        return h

    def wait_until_settled(self, timeout_s: float = 60.0,
                           min_online: int = 1) -> bool:
        """Block until the estate has largely come up, or give up."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            h = self.health()
            if h.online + h.degraded >= min_online and h.probing == 0:
                return True
            time.sleep(0.25)
        return False

    def stop(self) -> None:
        with self._lock:
            readers = list(self.readers.values())
            self.readers.clear()
            self.specs.clear()
        for r in readers:
            r.stop()
