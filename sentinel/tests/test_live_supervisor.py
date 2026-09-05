"""Estate supervision: connection pacing, one capture per camera, reconcile.

PART 12 requires that the estate does not open every session at once.
Fifty simultaneous RTSP handshakes are a burst, not a load: a real gateway
sees one client mount its whole estate at once and may throttle or ban it,
and on a restart loop that burst repeats.
"""

from __future__ import annotations

import time

import pytest

from ingestion.camera_config import CameraSpec
from ingestion.live_supervisor import LiveEstate
from ingestion.sentinel_catalogue import reconcile
from sentinel_sandbox.gateway import SandboxCamera, SandboxGateway
from sentinel_sandbox.rtsp_server import make_clip

pytestmark = pytest.mark.slow

pytest.importorskip("av", reason="PyAV is required for the live reader")


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> str:
    p = tmp_path_factory.mktemp("sup") / "c.mp4"
    return make_clip(str(p), "h264", seconds=3.0, fps=10.0)


@pytest.fixture
def gateway(clip):
    gw = SandboxGateway()
    for i in range(6):
        gw.add(SandboxCamera(f"cam-{i+1:03d}", f"Junction {i+1}",
                             23.0 + i * 0.003, 72.5, media_path=clip))
    gw.start()
    yield gw
    gw.stop()


def _specs(n: int) -> list[CameraSpec]:
    return [CameraSpec(camera_id=f"c{i}", name=f"c{i}", latitude=23.0,
                       longitude=72.5, stream_url="rtsp://127.0.0.1:9/stream/x")
            for i in range(n)]


def test_connections_are_staggered_rather_than_opened_at_once():
    """The gap between the first and last connection attempt must reflect
    the configured stagger."""
    estate = LiveEstate(stagger_ms=120, max_concurrent_opens=4,
                        open_timeout_s=1.0)
    specs = _specs(6)
    estate.add(specs)
    deadline = time.time() + 20
    while time.time() < deadline and len(estate.connect_started_at) < 6:
        time.sleep(0.02)
    started = sorted(estate.connect_started_at.values())
    estate.stop()

    assert len(started) == 6, f"only {len(started)} connections started"
    spread = started[-1] - started[0]
    # 6 cameras at 120 ms apart is ~0.6 s of stagger; without pacing this
    # would be a few milliseconds.
    assert spread > 0.35, f"connections were not staggered (spread {spread:.3f}s)"


def test_concurrent_opens_are_capped(gateway):
    """More cameras than slots must not be mid-handshake at once.

    Measured as peak in-flight handshakes, not as connections started in a
    window: a refused connection frees its slot immediately, so counting
    starts would measure how fast failures cycle rather than concurrency.
    """
    from ingestion.sentinel_catalogue import load_from_sentinel
    specs, _ = load_from_sentinel(gateway.catalogue_url)
    estate = LiveEstate(stagger_ms=0, max_concurrent_opens=2,
                        open_timeout_s=5.0)
    estate.add(specs)
    estate.wait_until_settled(timeout_s=60, min_online=1)
    peak = estate.peak_opens_in_flight
    opened = len(estate.readers)
    estate.stop()

    assert opened == len(specs), f"only {opened}/{len(specs)} cameras opened"
    assert peak <= 2, f"peak {peak} concurrent handshakes with a cap of 2"
    assert peak >= 1


def test_exactly_one_capture_exists_per_camera(gateway):
    """Decoding a camera twice doubles the most expensive thing in the
    system to save a dictionary lookup."""
    from ingestion.sentinel_catalogue import load_from_sentinel
    specs, _ = load_from_sentinel(gateway.catalogue_url)
    estate = LiveEstate(stagger_ms=50, max_concurrent_opens=4)
    estate.add(specs)
    estate.add(specs)          # a second request must not open a second session
    estate.wait_until_settled(timeout_s=45, min_online=1)
    count = len(estate.readers)
    a = estate.reader_for(specs[0].camera_id)
    b = estate.reader_for(specs[0].camera_id)
    estate.stop()
    assert count == len(specs), f"{count} readers for {len(specs)} cameras"
    assert a is b, "two consumers got two different decodes of one camera"


def test_reconcile_retires_a_camera_the_catalogue_dropped(gateway):
    from ingestion.sentinel_catalogue import load_from_sentinel
    estate = LiveEstate(stagger_ms=30, max_concurrent_opens=4)
    estate.sync(gateway.catalogue_url)
    estate.wait_until_settled(timeout_s=45, min_online=1)
    assert "cam-002" in estate.readers

    gateway.hide("cam-002")
    result = estate.sync(gateway.catalogue_url)
    retired = "cam-002" not in estate.readers
    estate.stop()

    assert result.removed == ["cam-002"]
    assert retired, "a camera removed upstream kept its capture open"


def test_a_cosmetic_change_does_not_restart_a_working_stream(gateway):
    """Tearing down a live stream to apply a renamed label is a
    self-inflicted outage."""
    from ingestion.sentinel_catalogue import load_from_sentinel
    specs, _ = load_from_sentinel(gateway.catalogue_url)
    estate = LiveEstate(stagger_ms=30, max_concurrent_opens=4)
    estate.add(specs)
    estate.wait_until_settled(timeout_s=45, min_online=1)
    before = estate.reader_for(specs[0].camera_id)

    renamed = [CameraSpec(**{**s.__dict__, "name": s.name + " (renamed)"})
               for s in specs]
    result = reconcile(renamed, dict(estate.specs))
    estate.apply(result)
    after = estate.reader_for(specs[0].camera_id)
    estate.stop()

    assert result.is_noop
    assert before is after, "a rename restarted the capture"
