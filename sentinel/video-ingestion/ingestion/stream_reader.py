"""Live stream reader.

Pulls decoded frames from RTSP / HLS / DVR sources through ffmpeg, as raw
BGR24 over a pipe. No OpenCV dependency: opencv-python is a ~90 MB wheel
that mostly duplicates ffmpeg, and on a headless server it drags in GUI
libraries the container does not need.

Two settings here carry most of the latency:

  -rtsp_transport tcp   UDP loss over a shared government WAN looks like
                        corruption, not loss, and produces green smears that
                        the detector happily finds vehicles in.
  -fflags nobuffer      plus a small analyzeduration. The default probe
  -flags low_delay      buffers several seconds before emitting a frame,
                        which is invisible in testing and fatal in pursuit.

A stalled camera must never block the pipeline, so reads are bounded by a
watchdog and the worker restarts the process rather than waiting.
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

from sentinel_core.log import get_logger

log = get_logger("sentinel.ingest.stream")


@dataclass
class StreamInfo:
    width: int
    height: int
    fps: float
    codec: str
    has_video: bool = True


@functools.lru_cache(maxsize=1)
def rtsp_socket_timeout_option() -> str:
    """Return the RTSP socket-timeout flag this ffmpeg actually accepts.

    This is not pedantry. ``-stimeout`` was removed from the RTSP demuxer
    after ffmpeg 4.x, and ffmpeg does not warn and continue -- it refuses
    to parse the argument list at all:

        Unrecognized option 'stimeout'.
        Error splitting the argument list: Option not found

    The process exits before it ever opens the input, so every RTSP camera
    fails instantly and the error looks like a configuration fault rather
    than a version mismatch. Government estates run whatever ffmpeg their
    distribution shipped, which spans both spellings, so the flag is
    probed once against the installed binary rather than assumed.
    """
    fallback = "-timeout"
    if not shutil.which("ffmpeg"):
        return fallback
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-h", "demuxer=rtsp"],
                           capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return fallback
    help_text = (r.stdout or "") + (r.stderr or "")
    # Match the option column so a mention inside prose cannot fool us.
    if re.search(r"^\s*-stimeout\s", help_text, re.MULTILINE):
        return "-stimeout"
    if re.search(r"^\s*-timeout\s", help_text, re.MULTILINE):
        return "-timeout"
    return fallback


def redact(url: str) -> str:
    """Strip credentials before any log line. A DVR password in a log file
    is how an estate gets compromised after the fact."""
    import re
    return re.sub(r"://[^/@]*@", "://***:***@", url or "")


def probe(url: str, timeout_s: int = 12) -> tuple[StreamInfo | None, str | None]:
    """Read a stream's real properties. A camera is not ONLINE until this
    succeeds -- registering 50 URLs from a spreadsheet and discovering on
    demo day that eleven were wrong is the classic failure."""
    if not shutil.which("ffprobe"):
        return None, "ffprobe not installed"
    cmd = ["ffprobe", "-v", "error"]
    if url.lower().startswith("rtsp://"):
        cmd += ["-rtsp_transport", "tcp"]
    cmd += ["-select_streams", "v:0",
           "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=0", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout_s}s"
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:300] or f"ffprobe exit {r.returncode}"

    fields = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line)
    if "width" not in fields:
        return None, "no video stream"
    num, _, den = fields.get("avg_frame_rate", "0/1").partition("/")
    try:
        fps = round(int(num) / int(den), 2) if int(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return StreamInfo(width=int(fields["width"]), height=int(fields["height"]),
                      fps=fps, codec=fields.get("codec_name", "unknown")), None


class FrameReader:
    """Background ffmpeg process yielding decoded frames.

    Frames are kept in a one-slot buffer that overwrites rather than queues.
    That is deliberate: for live surveillance a dropped frame is always
    better than a growing delay, and an unbounded queue turns a slow
    consumer into a steadily increasing lag that nobody notices until the
    alert arrives after the vehicle has gone.
    """

    def __init__(self, url: str, width: int = 640, height: int = 360,
                 fps: float = 6.0, transport: str = "tcp",
                 stall_timeout_s: float = 15.0):
        self.url = url
        self.width = width
        self.height = height
        self.fps = fps
        self.transport = transport
        self.stall_timeout_s = stall_timeout_s

        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._latest = None
        self._lock = threading.Lock()
        self._running = False
        self.frames_read = 0
        self.decode_errors = 0
        self.last_frame_at = 0.0
        self.last_error: str | None = None

    def _input_options(self) -> list[str]:
        """Input flags, chosen by protocol.

        These are NOT interchangeable. ffmpeg 6+ hard-fails with "Option
        rtsp_transport not found" when the flag is passed for a non-RTSP
        input, which silently breaks every file-backed and HLS camera. The
        demo estate uses file loops and the municipal feeds are HLS, so
        getting this wrong takes out most of the estate.
        """
        url = self.url.lower()
        opts = ["-fflags", "nobuffer", "-flags", "low_delay"]
        if url.startswith("rtsp://"):
            opts += ["-rtsp_transport", self.transport,
                     # Spelling differs across ffmpeg majors; probe it.
                     rtsp_socket_timeout_option(), "5000000"]   # 5 s, microseconds
        elif url.startswith(("http://", "https://")):
            # HLS: follow the live edge rather than starting from the top of
            # the playlist, which would otherwise replay minutes of history.
            opts += ["-live_start_index", "-1", "-reconnect", "1",
                     "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        else:
            # Local file, used by the demo harness: loop forever so a feed
            # never ends mid-presentation.
            opts += ["-stream_loop", "-1", "-re"]
        return opts + ["-analyzeduration", "1000000", "-probesize", "1000000"]

    def _command(self) -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            *self._input_options(),
            "-i", self.url,
            "-an",
            # Downscale in ffmpeg, not in Python: it is the difference
            # between moving 2.7 MB and 0.7 MB per frame across the pipe.
            "-vf", f"fps={self.fps},scale={self.width}:{self.height}",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ]

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name=f"reader-{redact(self.url)[:40]}")
        self._thread.start()

    def _pump(self) -> None:
        import numpy as np
        frame_bytes = self.width * self.height * 3
        while self._running:
            try:
                self._proc = subprocess.Popen(
                    self._command(), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, bufsize=frame_bytes * 2)
            except FileNotFoundError:
                self.last_error = "ffmpeg not installed"
                log.error("ffmpeg not installed; live ingestion unavailable")
                self._running = False
                return

            while self._running and self._proc.poll() is None:
                buf = self._proc.stdout.read(frame_bytes)
                if not buf or len(buf) < frame_bytes:
                    self.decode_errors += 1
                    break
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(
                    self.height, self.width, 3)
                with self._lock:
                    self._latest = frame          # overwrite, never queue
                self.frames_read += 1
                self.last_frame_at = time.time()

            if self._proc and self._proc.poll() is None:
                self._proc.kill()
            if self._proc:
                err = (self._proc.stderr.read() or b"").decode("utf-8", "replace")
                if err.strip():
                    self.last_error = err.strip()[:300]
            if self._running:
                # Back off before reconnecting. A camera that is genuinely
                # down should not be hammered once per second by each of
                # several thousand workers.
                time.sleep(3.0)

    def read(self):
        with self._lock:
            return self._latest

    @property
    def is_stalled(self) -> bool:
        return (self.last_frame_at > 0
                and time.time() - self.last_frame_at > self.stall_timeout_s)

    def stop(self) -> None:
        self._running = False
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        if self._thread:
            self._thread.join(timeout=3)
