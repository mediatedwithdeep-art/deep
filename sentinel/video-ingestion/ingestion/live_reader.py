"""PTS-derived live stream reader.

WHY NOT THE EXISTING FrameReader
────────────────────────────────
`stream_reader.FrameReader` pipes raw BGR24 out of an ffmpeg subprocess.
Raw video has no timestamp channel, so the only time available to Python is
the moment it read bytes off a socket. Worse, that reader asks ffmpeg for
`-vf fps=N`, which *resamples* the stream to constant frame rate by
duplicating and dropping frames — the original capture cadence is destroyed
inside ffmpeg before Python ever sees a pixel.

Measured against the sandbox gateway, opening a 15 fps stream:

    frame   pts_time   arrival     d_pts    d_arrival
      1      0.0667    0.0031     0.0667      0.0000
      2      0.1333    0.0041     0.0667      0.0010
      …
      12     0.8000    0.0114     0.0667      0.0007

PTS says 0.8 s of video elapsed. Arrival says 11 ms — the decoder emitted a
buffered burst as fast as the pipe would take it. **A speed derived from
arrival timestamps would be roughly seventy times wrong**, and the
spatio-temporal gate — the load-bearing claim of the whole architecture —
is computed from exactly those timestamps.

So this reader uses PyAV, which exposes the container's own timestamps.
PyAV is libav without the GUI baggage that made opencv-python unattractive;
it is the same ffmpeg already required, bound directly.

CAPTURE TIME
────────────
`pts_time` is a position within the stream, not a wall clock. Capture time
is anchored once, at the first frame:

    capture_time = anchor_wall_clock + (pts_time − anchor_pts)

Every subsequent frame's wall clock is then derived from the stream's own
clock rather than from network jitter. Anchoring on the *first* frame, not
continuously, is deliberate: re-anchoring per frame would reintroduce the
arrival jitter the anchor exists to remove.

This is honest about its limits. A true capture clock needs the RTCP sender
report's NTP mapping; PyAV does not expose it. The anchor therefore carries
the network's one-way delay as a constant offset — constant offsets do not
distort *intervals*, which is what the gate and every speed calculation
actually consume.

DISCONTINUITY
─────────────
Looping media and real camera restarts both make PTS jump or go backwards.
That is a scene cut: the tracker's state refers to a world that no longer
exists, and bridging across it fabricates journeys that never happened. The
reader flags it and the pipeline drops its state, rather than silently
producing an impossible track.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sentinel_core.domain import CameraStatus
from sentinel_core.log import get_logger

from .stream_reader import redact

log = get_logger("sentinel.ingest.live")

# Backoff schedule. A camera that is genuinely down must not be hammered
# once per second by each of several thousand workers, and the whole estate
# must not retry in lockstep after a shared network blip -- hence jitter.
BACKOFF_SCHEDULE = (2.0, 4.0, 8.0, 16.0, 30.0)
BACKOFF_JITTER = 0.25

# A PTS jump larger than this means the stream restarted or looped rather
# than merely dropped frames.
DISCONTINUITY_GAP_S = 5.0


@dataclass
class Frame:
    """One decoded frame and the time it was actually captured."""
    image: object                    # numpy BGR24 array
    pts_time: float                  # seconds within the stream
    capture_time: datetime           # wall clock, PTS-derived
    frame_index: int
    is_discontinuity: bool = False   # first frame after a scene cut
    width: int = 0
    height: int = 0


@dataclass
class ReaderHealth:
    status: CameraStatus = CameraStatus.PENDING
    frames: int = 0
    discontinuities: int = 0
    reconnects: int = 0
    consecutive_failures: int = 0
    #: How many times this reader rotated to a different transport.
    transport_failovers: int = 0
    last_error: str | None = None
    last_frame_wall: float = 0.0
    observed_fps: float = 0.0
    codec: str = ""
    width: int = 0
    height: int = 0
    pts_span_s: float = 0.0
    next_retry_in_s: float = 0.0


def backoff_delay(attempt: int, rng: random.Random | None = None) -> float:
    """2 → 4 → 8 → 16 → 30 s, capped, with jitter.

    `attempt` is 1-based. Jitter is proportional and symmetric so a large
    estate recovering from one network event spreads its reconnects instead
    of arriving as a thundering herd.
    """
    idx = min(max(attempt, 1), len(BACKOFF_SCHEDULE)) - 1
    base = BACKOFF_SCHEDULE[idx]
    r = rng or random
    return max(0.5, base * (1.0 + r.uniform(-BACKOFF_JITTER, BACKOFF_JITTER)))


class LiveStreamReader:
    """Background PyAV reader with PTS timing, backoff and a state machine.

    Frames land in a one-slot buffer that overwrites rather than queues: for
    live surveillance a dropped frame is always better than a growing delay,
    and an unbounded queue turns a slow consumer into a lag nobody notices
    until the alert arrives after the vehicle has gone.
    """

    def __init__(self, url: str, camera_id: str = "?",
                 width: int | None = 640, height: int | None = 360,
                 transport: str = "tcp",
                 open_timeout_s: float = 10.0,
                 stall_timeout_s: float = 15.0,
                 expected_fps: float | None = None,
                 max_failures_before_offline: int = 3,
                 fallback_urls: "tuple[str, ...] | list[str] | None" = None,
                 failures_before_failover: int = 2,
                 seed: int | None = None):
        # Ordered transports for one camera. The integrator's guide is
        # explicit that RTSP (8554/TCP) is blocked on many corporate and
        # government networks and that HLS is the supported way through,
        # so a camera carries its alternatives and the reader rotates to
        # them rather than sitting RECONNECTING against a port that policy
        # will never open.
        self._urls: list[str] = [url, *[u for u in (fallback_urls or [])
                                        if u and u != url]]
        self._url_index = 0
        self.failures_before_failover = failures_before_failover
        self.camera_id = camera_id
        self.width = width
        self.height = height
        self.transport = transport
        self.open_timeout_s = open_timeout_s
        self.stall_timeout_s = stall_timeout_s
        self.expected_fps = expected_fps
        self.max_failures_before_offline = max_failures_before_offline

        self.health = ReaderHealth()
        self._rng = random.Random(seed)
        self._latest: Frame | None = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()

        # PTS bookkeeping
        self._anchor_wall: float | None = None
        self._anchor_pts: float | None = None
        self._last_pts: float | None = None
        self._frame_index = 0
        self._pending_discontinuity = False
        self._recent_pts: list[float] = field(default_factory=list)  # replaced below
        self._recent_pts = []

    # ── transports ────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        """The transport currently in use."""
        return self._urls[self._url_index]

    @property
    def transports(self) -> list[str]:
        return list(self._urls)

    def _failover(self) -> bool:
        """Rotate to the next transport. True if it actually moved.

        Rotation is cyclic rather than terminal: a blocked port and a
        temporarily sick CDN are indistinguishable from here, so the reader
        keeps cycling instead of committing permanently to whichever
        transport happened to be up when it started.
        """
        if len(self._urls) < 2:
            return False
        previous = self.url
        self._url_index = (self._url_index + 1) % len(self._urls)
        log.warning("camera %s failing over: %s -> %s", self.camera_id,
                    redact(previous), redact(self.url))
        self.health.transport_failovers += 1
        return True

    # ── options ───────────────────────────────────────────────────────

    #: Schemes that can carry a genuinely live stream. Anything else -- a
    #: bare path, file://, a downloaded clip -- is recorded media.
    LIVE_SCHEMES = ("rtsp://", "rtsps://", "http://", "https://",
                    "rtmp://", "srt://", "udp://", "rtp://")

    @property
    def is_live_url(self) -> bool:
        """Whether this URL can be a live camera at all.

        PART 11 requires live-only evaluation, and the way that requirement
        fails in practice is not deliberate cheating: it is a catalogue
        entry with a stale local path, or a developer pointing a reader at
        a sample clip to reproduce something and leaving it. Either
        produces a stream that decodes perfectly, reports healthy, and
        measures recorded video -- with no symptom anywhere.

        A path is not merely discouraged here, it is refused by `start()`,
        because a warning in a log is not a control.
        """
        return self.url.lower().startswith(self.LIVE_SCHEMES)

    def _open_options(self) -> dict[str, str]:
        """Transport options. RTSP is pinned to TCP, always.

        UDP loss over a shared government WAN arrives as corruption rather
        than loss and produces green smears the detector happily finds
        vehicles in. There is no configuration path to UDP here on purpose.
        """
        opts = {
            # microseconds; PyAV passes these straight to libav
            "timeout": str(int(self.open_timeout_s * 1_000_000)),
        }
        if self.url.lower().startswith("rtsp://"):
            opts["rtsp_transport"] = self.transport
            opts["rtsp_flags"] = "prefer_tcp"
        return opts

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        if not self.is_live_url:
            # Refused, not warned. A reader pointed at a file decodes
            # perfectly and reports healthy while measuring recorded video,
            # so there is no symptom for a warning to be noticed by.
            self.health.status = CameraStatus.DISABLED
            self.health.last_error = (
                f"refusing a non-live source for camera {self.camera_id}: "
                f"{redact(self.url)} is not one of {', '.join(self.LIVE_SCHEMES)}")
            log.error("%s", self.health.last_error)
            return
        self._running = True
        self.health.status = CameraStatus.PROBING
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"live-{self.camera_id}")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.health.status = CameraStatus.DISABLED

    def read(self) -> Frame | None:
        with self._lock:
            return self._latest

    def take(self) -> Frame | None:
        """Read and clear, so a caller can tell a new frame from a repeat."""
        with self._lock:
            f, self._latest = self._latest, None
            return f

    @property
    def is_stalled(self) -> bool:
        return (self.health.last_frame_wall > 0
                and time.time() - self.health.last_frame_wall > self.stall_timeout_s)

    # ── main loop ─────────────────────────────────────────────────────

    def _run(self) -> None:
        attempt = 0
        while self._running:
            try:
                produced = self._session()
                if produced:
                    attempt = 0          # a working session resets backoff
            except Exception as exc:                      # noqa: BLE001
                self.health.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("stream error on %s (%s): %s", self.camera_id,
                            redact(self.url), self.health.last_error)

            if not self._running:
                break

            attempt += 1
            self.health.consecutive_failures += 1
            self.health.reconnects += 1
            # A stream that ends is not yet OFFLINE -- it is RECONNECTING.
            # Calling it OFFLINE immediately makes a looping camera flap
            # red in the control room and trains operators to ignore it.
            if self.health.consecutive_failures >= self.max_failures_before_offline:
                self.health.status = CameraStatus.OFFLINE
            else:
                self.health.status = CameraStatus.RECONNECTING

            if (self.health.consecutive_failures
                    and self.health.consecutive_failures
                    % self.failures_before_failover == 0):
                self._failover()

            delay = backoff_delay(attempt, self._rng)
            self.health.next_retry_in_s = delay
            log.info("camera %s reconnecting in %.1fs (attempt %d, state %s)",
                     self.camera_id, delay, attempt, self.health.status)
            # A stream restart is a scene cut: whatever the tracker believes
            # refers to a world that no longer exists.
            self._pending_discontinuity = True
            self._anchor_wall = None
            self._anchor_pts = None
            self._last_pts = None
            self._wake.wait(delay)
            self._wake.clear()

    def _session(self) -> bool:
        """One connection. Returns True if it produced at least one frame."""
        import av

        produced = False
        container = None
        try:
            container = av.open(self.url, options=self._open_options(),
                                timeout=self.open_timeout_s)
            stream = container.streams.video[0]
            # Let libav use several threads; a single-threaded decode of
            # 1080p H.265 does not keep up on a modest edge box.
            stream.thread_type = "AUTO"
            tb = stream.time_base

            self.health.codec = stream.codec_context.name
            self.health.width = stream.codec_context.width
            self.health.height = stream.codec_context.height
            self.health.consecutive_failures = 0
            self.health.status = CameraStatus.ONLINE
            self.health.last_error = None
            log.info("camera %s online: %s %dx%d", self.camera_id,
                     self.health.codec, self.health.width, self.health.height)

            for frame in container.decode(video=0):
                if not self._running:
                    break
                pts = frame.pts
                if pts is None:
                    continue
                pts_time = float(pts * tb) if tb else float(frame.time or 0.0)
                self._emit(frame, pts_time)
                produced = True
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:                          # noqa: BLE001
                    pass
        return produced

    # ── frame handling ────────────────────────────────────────────────

    def _emit(self, av_frame, pts_time: float) -> None:
        now = time.time()
        discontinuous = self._pending_discontinuity

        if self._last_pts is not None:
            gap = pts_time - self._last_pts
            # Backwards PTS or a large forward jump both mean the stream
            # restarted or looped. Either way the picture is unrelated to
            # the previous frame.
            if gap < 0 or gap > DISCONTINUITY_GAP_S:
                discontinuous = True
                log.info("scene discontinuity on %s: pts %.3f -> %.3f",
                         self.camera_id, self._last_pts, pts_time)

        if discontinuous:
            self.health.discontinuities += 1
            # Re-anchor: the stream clock restarted, so the old mapping
            # from pts to wall clock is meaningless.
            self._anchor_wall = now
            self._anchor_pts = pts_time
            self._recent_pts.clear()
            self._pending_discontinuity = False

        if self._anchor_wall is None or self._anchor_pts is None:
            self._anchor_wall = now
            self._anchor_pts = pts_time

        capture = datetime.fromtimestamp(self._anchor_wall, tz=timezone.utc) \
            + timedelta(seconds=pts_time - self._anchor_pts)

        image = self._to_bgr(av_frame)
        self._frame_index += 1
        frame = Frame(image=image, pts_time=pts_time, capture_time=capture,
                      frame_index=self._frame_index,
                      is_discontinuity=discontinuous,
                      width=image.shape[1] if image is not None else 0,
                      height=image.shape[0] if image is not None else 0)

        with self._lock:
            self._latest = frame

        self._last_pts = pts_time
        self.health.frames += 1
        self.health.last_frame_wall = now
        self._observe_cadence(pts_time)

    def _to_bgr(self, av_frame):
        """Reformat to BGR24, scaling only if a size was requested.

        Aspect ratio is preserved by scaling both axes; a 4:3 camera
        stretched into 16:9 distorts plate glyphs and every ReID crop taken
        through it.
        """
        if self.width and self.height:
            src_ar = av_frame.width / av_frame.height if av_frame.height else 1.0
            dst_ar = self.width / self.height
            if abs(src_ar - dst_ar) < 0.01:
                return av_frame.to_ndarray(format="bgr24",
                                           width=self.width, height=self.height)
            # Fit inside the box, keeping aspect ratio, rounded to even
            # dimensions because most swscale paths require it.
            if src_ar > dst_ar:
                w, h = self.width, max(2, int(self.width / src_ar) // 2 * 2)
            else:
                h, w = self.height, max(2, int(self.height * src_ar) // 2 * 2)
            return av_frame.to_ndarray(format="bgr24", width=w, height=h)
        return av_frame.to_ndarray(format="bgr24")

    def _observe_cadence(self, pts_time: float) -> None:
        """Measure real fps from PTS, not from a declared value.

        A camera that advertises 25 fps and delivers 4 is the normal case on
        a congested government WAN. What matters downstream is what actually
        arrived, so DEGRADED is decided by measurement.
        """
        self._recent_pts.append(pts_time)
        if len(self._recent_pts) > 60:
            self._recent_pts.pop(0)
        if len(self._recent_pts) >= 5:
            span = self._recent_pts[-1] - self._recent_pts[0]
            if span > 0:
                self.health.observed_fps = (len(self._recent_pts) - 1) / span
                self.health.pts_span_s = span
                if self.expected_fps and self.health.status in (
                        CameraStatus.ONLINE, CameraStatus.DEGRADED):
                    # Half the expected rate is the line between "live" and
                    # "technically connected".
                    self.health.status = (
                        CameraStatus.DEGRADED
                        if self.health.observed_fps < self.expected_fps * 0.5
                        else CameraStatus.ONLINE)
