"""Stream probing and ffmpeg input options.

WHAT THIS FILE IS, AND WHAT IT DELIBERATELY NO LONGER CONTAINS
─────────────────────────────────────────────────────────────
This module answers one question about a camera -- "is it alive, and what
are its real properties?" -- and builds the ffmpeg input options used to
ask it. Live frame reading is `live_reader.LiveStreamReader`, which decodes
through PyAV and carries each frame's own presentation timestamp.

It used to also contain `FrameReader`, which piped raw BGR24 out of an
ffmpeg subprocess. That class has been REMOVED rather than left unused,
because it embodied three defects the Phase 2 audit called P0 and every one
of them was invisible at rest:

  * ``-vf fps=N`` resampled the stream to constant frame rate inside
    ffmpeg, duplicating and dropping frames. Original capture cadence was
    destroyed before Python saw a pixel, and raw video over a pipe has no
    timestamp channel to recover it from.
  * Timing therefore came from the moment bytes were read off a socket.
    Measured against the sandbox, arrival said 11 ms where PTS said 0.8 s.
  * Reconnection was a flat ``time.sleep(3.0)``, so a down camera was
    retried every three seconds forever by every worker in lockstep.

Nothing imported it any more, which is exactly what made keeping it
dangerous: dead code with a working-looking constructor is code somebody
picks up. The spatio-temporal gate is computed from these timestamps, so a
reader that silently substitutes arrival time for capture time does not
produce an error -- it produces a plausible, wrong answer.

Two settings here still carry most of the latency:

  -rtsp_transport tcp   UDP loss over a shared government WAN looks like
                        corruption, not loss, and produces green smears that
                        the detector happily finds vehicles in.
  -fflags nobuffer      plus a small analyzeduration. The default probe
  -flags low_delay      buffers several seconds before emitting a frame,
                        which is invisible in testing and fatal in pursuit.
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


def ffmpeg_input_options(url: str, transport: str = "tcp") -> list[str]:
    """Input flags for `url`, chosen by protocol.

    These are NOT interchangeable. ffmpeg 6+ hard-fails with "Option
    rtsp_transport not found" when the flag is passed for a non-RTSP input,
    which silently breaks every file-backed and HLS camera at once.

    RTSP is pinned to TCP here and there is no configuration path to UDP,
    for the reason in the module docstring.
    """
    u = (url or "").lower()
    opts = ["-fflags", "nobuffer", "-flags", "low_delay"]
    if u.startswith(("rtsp://", "rtsps://")):
        opts += ["-rtsp_transport", transport,
                 # Spelling differs across ffmpeg majors; probe the binary.
                 rtsp_socket_timeout_option(), "5000000"]   # 5 s, microseconds
    elif u.startswith(("http://", "https://")):
        # HLS: follow the live edge rather than starting from the top of the
        # playlist, which would otherwise replay minutes of history.
        opts += ["-live_start_index", "-1", "-reconnect", "1",
                 "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    return opts + ["-analyzeduration", "1000000", "-probesize", "1000000"]


def probe(url: str, timeout_s: int = 12) -> tuple[StreamInfo | None, str | None]:
    """Read a stream's real properties. A camera is not ONLINE until this
    succeeds -- registering 50 URLs from a spreadsheet and discovering on
    demo day that eleven were wrong is the classic failure."""
    if not shutil.which("ffprobe"):
        return None, "ffprobe not installed"
    # Same options as the reader, so a camera that probes cannot then fail
    # to open for a transport reason the probe never exercised. Without the
    # socket timeout an unresponsive RTSP server holds the probe until the
    # subprocess timeout fires, which reads as a slow camera rather than an
    # unreachable one.
    cmd = ["ffprobe", "-v", "error", *ffmpeg_input_options(url)]
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
