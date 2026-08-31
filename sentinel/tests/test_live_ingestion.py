"""Live-feed tests against a real RTSP server.

These are the tests Phase 1 did not have. Every ingestion test in
`test_ingestion.py` runs against the simulated traffic world, which is why
a bug that made *every* RTSP camera fail (`-stimeout`, removed after ffmpeg
4.x) survived 186 passing tests.

Everything here runs against `tools/sentinel_sandbox`: a real RTSP 1.0
server carrying real RTP from real H.264/H.265 encoders, on the Sentinel
contract's URL shapes. Real protocol, real timestamps, real disconnects.
"""

from __future__ import annotations

import random
import time

import pytest

from sentinel_core.domain import CameraStatus
from ingestion.live_reader import (
    BACKOFF_SCHEDULE, LiveStreamReader, backoff_delay,
)
from sentinel_sandbox.gateway import SandboxCamera, SandboxGateway
from sentinel_sandbox.rtsp_server import MediaSource, RtspServer, make_clip

pytestmark = pytest.mark.slow

av = pytest.importorskip("av", reason="PyAV is required for PTS-derived timing")


# ── media, encoded once for the whole session ────────────────────────

@pytest.fixture(scope="session")
def h264_clip(tmp_path_factory) -> str:
    p = tmp_path_factory.mktemp("media") / "h264.mp4"
    return make_clip(str(p), "h264", seconds=4.0, fps=15.0)


@pytest.fixture(scope="session")
def hevc_clip(tmp_path_factory) -> str:
    p = tmp_path_factory.mktemp("media") / "hevc.mp4"
    return make_clip(str(p), "hevc", seconds=4.0, fps=12.0)


@pytest.fixture(scope="session")
def slow_clip(tmp_path_factory) -> str:
    """A deliberately low frame rate. A camera that advertises 25 and
    delivers 4 is the normal case on a congested government WAN."""
    p = tmp_path_factory.mktemp("media") / "slow.mp4"
    return make_clip(str(p), "h264", seconds=4.0, fps=4.0)


def _read_frames(url: str, want: int, timeout_s: float = 30.0, **kw):
    r = LiveStreamReader(url, camera_id="test", **kw)
    r.start()
    frames, seen, deadline = [], set(), time.time() + timeout_s
    while len(frames) < want and time.time() < deadline:
        f = r.read()
        if f is not None and f.frame_index not in seen:
            seen.add(f.frame_index)
            frames.append(f)
        time.sleep(0.004)
    return r, frames


# ── PART 6 · PTS-derived timing ──────────────────────────────────────

def test_frame_timing_comes_from_pts_not_from_arrival(h264_clip):
    """The core of PART 6.

    A decoder emits a buffered burst on connect: twelve frames can arrive
    in eleven milliseconds while representing 0.8 s of video. If timing is
    taken from arrival, every speed is wrong by that ratio and the
    spatio-temporal gate -- the load-bearing claim of the architecture --
    is computed from fiction.
    """
    with RtspServer(port=0) as srv:
        srv.add(MediaSource("cam-1", h264_clip, "h264"))
        reader, frames = _read_frames(srv.rtsp_url("cam-1"), 12)
        reader.stop()

    assert len(frames) >= 10, f"only {len(frames)} frames decoded"

    pts_gaps = [b.pts_time - a.pts_time
                for a, b in zip(frames, frames[1:])
                if not b.is_discontinuity]
    assert pts_gaps, "no continuous frame pairs"

    # 15 fps source → 1/15 s between frames, from the stream's own clock.
    nominal = 1.0 / 15.0
    median = sorted(pts_gaps)[len(pts_gaps) // 2]
    assert abs(median - nominal) < nominal * 0.25, (
        f"median PTS gap {median:.4f}s is not the source's {nominal:.4f}s")

    # And capture_time must track PTS, not arrival.
    cap_gaps = [(b.capture_time - a.capture_time).total_seconds()
                for a, b in zip(frames, frames[1:])
                if not b.is_discontinuity]
    for p, c in zip(pts_gaps, cap_gaps):
        assert abs(p - c) < 1e-6, "capture_time drifted from PTS"


def test_capture_time_is_monotonic_and_spans_real_video_time(h264_clip):
    with RtspServer(port=0) as srv:
        srv.add(MediaSource("cam-1", h264_clip, "h264"))
        reader, frames = _read_frames(srv.rtsp_url("cam-1"), 15)
        reader.stop()

    times = [f.capture_time for f in frames]
    assert times == sorted(times), "capture_time went backwards"
    span = (times[-1] - times[0]).total_seconds()
    # 15 frames of a 15 fps stream is about a second of video. If timing
    # came from arrival this would be a few milliseconds.
    assert span > 0.4, f"span {span:.3f}s looks like arrival time, not PTS"


def test_pts_is_reported_in_seconds_not_raw_ticks(h264_clip):
    """90 kHz is the usual RTP clock; a raw tick count would be ~90,000x
    too large and would silently poison every travel-time calculation."""
    with RtspServer(port=0) as srv:
        srv.add(MediaSource("cam-1", h264_clip, "h264"))
        reader, frames = _read_frames(srv.rtsp_url("cam-1"), 6)
        reader.stop()
    assert all(0.0 <= f.pts_time < 60.0 for f in frames), \
        [f.pts_time for f in frames[:5]]


# ── PART 7/8 · codecs, variable rate, resolution ─────────────────────

@pytest.mark.parametrize("codec,fps", [("h264", 15.0), ("hevc", 12.0)])
def test_both_codecs_decode_over_rtsp(codec, fps, h264_clip, hevc_clip):
    clip = h264_clip if codec == "h264" else hevc_clip
    with RtspServer(port=0) as srv:
        srv.add(MediaSource(f"cam-{codec}", clip, codec))
        reader, frames = _read_frames(srv.rtsp_url(f"cam-{codec}"), 8)
        codec_seen = reader.health.codec
        reader.stop()

    assert len(frames) >= 5, f"{codec}: only {len(frames)} frames"
    assert codec_seen == codec
    assert frames[0].image is not None and frames[0].image.ndim == 3


def test_a_low_frame_rate_camera_is_not_treated_as_a_failure(slow_clip):
    """4 fps is a slow camera, not a broken one."""
    with RtspServer(port=0) as srv:
        srv.add(MediaSource("cam-slow", slow_clip, "h264"))
        reader, frames = _read_frames(srv.rtsp_url("cam-slow"), 8,
                                      timeout_s=40, expected_fps=4.0)
        status, observed = reader.health.status, reader.health.observed_fps
        reader.stop()

    assert len(frames) >= 5
    assert status == CameraStatus.ONLINE, f"4 fps reported as {status}"
    assert 2.0 < observed < 7.0, f"measured {observed:.1f} fps"


def test_a_camera_delivering_far_below_its_declared_rate_is_degraded(slow_clip):
    """Declared 25, delivering 4 → DEGRADED, decided by measurement."""
    with RtspServer(port=0) as srv:
        srv.add(MediaSource("cam-slow", slow_clip, "h264"))
        reader, frames = _read_frames(srv.rtsp_url("cam-slow"), 8,
                                      timeout_s=40, expected_fps=25.0)
        status = reader.health.status
        reader.stop()
    assert status == CameraStatus.DEGRADED


def test_aspect_ratio_is_preserved_rather_than_stretched(tmp_path):
    """A 4:3 camera squeezed into 16:9 distorts every plate glyph and every
    ReID crop taken through it."""
    clip = make_clip(str(tmp_path / "43.mp4"), "h264", seconds=3.0,
                     width=640, height=480, fps=12.0)
    with RtspServer(port=0) as srv:
        srv.add(MediaSource("cam-43", clip, "h264"))
        reader, frames = _read_frames(srv.rtsp_url("cam-43"), 5, timeout_s=40,
                                      width=640, height=360)
        reader.stop()

    assert frames, "no frames"
    h, w = frames[0].image.shape[:2]
    assert abs((w / h) - (640 / 480)) < 0.02, (
        f"4:3 source came back as {w}x{h}, aspect {(w/h):.3f}")


# ── PART 9 · reconnection ────────────────────────────────────────────

def test_backoff_is_exponential_capped_and_jittered():
    rng = random.Random(7)
    for attempt, base in enumerate(BACKOFF_SCHEDULE, start=1):
        d = backoff_delay(attempt, rng)
        assert base * 0.7 <= d <= base * 1.3, f"attempt {attempt}: {d}"
    # Capped, not unbounded.
    assert backoff_delay(50, rng) <= BACKOFF_SCHEDULE[-1] * 1.3
    # Jitter is real: identical attempts must not align across the estate.
    assert len({round(backoff_delay(3, rng), 6) for _ in range(20)}) > 1


def test_a_dropped_stream_reconnects_and_reports_reconnecting(h264_clip):
    """The server kills the session mid-stream. The reader must retry, not
    give up, and must report RECONNECTING rather than OFFLINE -- a camera
    briefly dropped is not a camera believed gone."""
    with RtspServer(port=0) as srv:
        srv.add(MediaSource("cam-drop", h264_clip, "h264", drop_after_s=1.5))
        reader = LiveStreamReader(srv.rtsp_url("cam-drop"), camera_id="drop",
                                  seed=1)
        reader.start()
        seen_reconnecting = False
        deadline = time.time() + 25
        while time.time() < deadline:
            if reader.health.status == CameraStatus.RECONNECTING:
                seen_reconnecting = True
                break
            time.sleep(0.05)
        # Give it long enough to come back after the backoff.
        recovered = False
        deadline = time.time() + 25
        while time.time() < deadline:
            if reader.health.status == CameraStatus.ONLINE and reader.health.frames > 0:
                recovered = True
                break
            time.sleep(0.05)
        reconnects, drops = reader.health.reconnects, srv.stats["forced_drops"]
        reader.stop()

    assert drops >= 1, "the server never dropped the session"
    assert seen_reconnecting, "never reported RECONNECTING"
    assert reconnects >= 1
    assert recovered, "did not recover after the backoff"


def test_an_unreachable_camera_goes_offline_without_raising():
    reader = LiveStreamReader("rtsp://127.0.0.1:9/stream/nope",
                              camera_id="dead", open_timeout_s=1.0, seed=3)
    reader.start()
    deadline = time.time() + 30
    while time.time() < deadline and reader.health.status != CameraStatus.OFFLINE:
        time.sleep(0.1)
    status, failures = reader.health.status, reader.health.consecutive_failures
    reader.stop()
    assert status == CameraStatus.OFFLINE
    assert failures >= 3


# ── PART 5 · transport ───────────────────────────────────────────────

def test_rtsp_is_pinned_to_tcp():
    r = LiveStreamReader("rtsp://host/stream/x")
    assert r._open_options()["rtsp_transport"] == "tcp"
    # Non-RTSP inputs must not carry the flag at all.
    assert "rtsp_transport" not in LiveStreamReader("https://h/x.m3u8")._open_options()


def test_the_sandbox_refuses_udp_so_a_misconfigured_client_is_loud(h264_clip):
    """A server that merely prefers TCP cannot prove a client is configured
    correctly. This one refuses UDP with 461."""
    import subprocess
    with RtspServer(port=0) as srv:
        srv.add(MediaSource("cam-1", h264_clip, "h264"))
        url = srv.rtsp_url("cam-1")
        r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "udp",
                            "-i", url], capture_output=True, text=True, timeout=60)
        refused = srv.stats["udp_refused"]
    assert r.returncode != 0
    assert "461" in (r.stderr or "") or refused >= 1


# ── PART 10 · scene discontinuity ────────────────────────────────────

def test_a_loop_point_is_reported_as_a_scene_discontinuity(tmp_path):
    """Looping media restarts PTS. Bridging a tracker across that point
    fabricates a journey that never happened, so it must be visible."""
    clip = make_clip(str(tmp_path / "short.mp4"), "h264", seconds=1.5, fps=12.0)
    with RtspServer(port=0) as srv:
        srv.add(MediaSource("cam-loop", clip, "h264", loop=True))
        reader = LiveStreamReader(srv.rtsp_url("cam-loop"), camera_id="loop")
        reader.start()
        seen, deadline = [], time.time() + 40
        while time.time() < deadline and reader.health.discontinuities == 0:
            f = reader.read()
            if f is not None:
                seen.append(f)
            time.sleep(0.004)
        discontinuities = reader.health.discontinuities
        reader.stop()

    assert discontinuities >= 1, "looped past the end without noticing"


def test_capture_time_is_re_anchored_after_a_discontinuity(tmp_path):
    """After a restart the stream clock means something different. Keeping
    the old anchor would place new frames in the past and invert apparent
    direction of travel."""
    clip = make_clip(str(tmp_path / "short2.mp4"), "h264", seconds=1.5, fps=12.0)
    with RtspServer(port=0) as srv:
        srv.add(MediaSource("cam-loop", clip, "h264", loop=True))
        reader = LiveStreamReader(srv.rtsp_url("cam-loop"), camera_id="loop")
        reader.start()
        frames, seen_idx, deadline = [], set(), time.time() + 40
        while time.time() < deadline and reader.health.discontinuities == 0:
            f = reader.read()
            if f is not None and f.frame_index not in seen_idx:
                seen_idx.add(f.frame_index)
                frames.append(f)
            time.sleep(0.004)
        # capture one frame after the discontinuity
        after_deadline = time.time() + 10
        while time.time() < after_deadline:
            f = reader.read()
            if f is not None and f.frame_index not in seen_idx:
                seen_idx.add(f.frame_index)
                frames.append(f)
                if f.is_discontinuity:
                    break
            time.sleep(0.004)
        reader.stop()

    times = [f.capture_time for f in frames]
    assert times == sorted(times), (
        "capture_time went backwards across a loop point -- the anchor was "
        "not reset, so the vehicle appears to travel into the past")
