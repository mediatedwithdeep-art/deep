#!/usr/bin/env python3
"""Live POC harness — measure a real camera estate over RTSP.

Produces the evidence for docs/SENTINEL_LIVE_TEST_REPORT.md: per-camera
measured FPS, bitrate, connect latency, PTS behaviour and reconnect
behaviour, taken from live streams rather than declared values.

    python3 scripts/live_poc.py --cameras 50 --seconds 60
    python3 scripts/live_poc.py --catalogue-url https://<sandbox-host> --seconds 120

With no --catalogue-url it stands up the local sandbox gateway (a real
RTSP server, see tools/sentinel_sandbox) and measures against that. Against
the real sandbox, pass its URL: nothing else changes.

MEASUREMENT RULES
─────────────────
* Live only. Nothing is read from a local file, nothing is seeked, and
  nothing runs faster than real time — every stream is paced by `-re` on
  the server side and by PTS on the client side.
* FPS is derived from PTS deltas, never from a declared frame rate and
  never from frame arrival, which on a buffered connect is ~70x wrong.
* Numbers that were measured are labelled MEASURED. Nothing here is
  extrapolated; the extrapolations live in the scaling document and are
  labelled separately.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in ("shared", "video-ingestion", "tools"):
    sys.path.insert(0, str(ROOT / p))

from ingestion.live_supervisor import LiveEstate           # noqa: E402
from ingestion.sentinel_catalogue import load_from_sentinel  # noqa: E402
from sentinel_core.domain import CameraStatus              # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def _spread_estate(n: int, clip_h264: str, clip_hevc: str, clip_slow: str):
    """A deliberately heterogeneous estate.

    A POC where every camera is identical measures one camera fifty times.
    Real estates mix codecs, frame rates and reliability, and the mix is
    what exposes the interesting failures.
    """
    from sentinel_sandbox.gateway import SandboxCamera

    cams = []
    for i in range(n):
        cam_id = f"cam-{i + 1:03d}"
        if i % 7 == 3:
            codec, clip, fps = "hevc", clip_hevc, 12.0
        elif i % 11 == 5:
            codec, clip, fps = "h264", clip_slow, 4.0
        else:
            codec, clip, fps = "h264", clip_h264, 15.0
        cams.append(SandboxCamera(
            camera_id=cam_id,
            name=f"Junction {i + 1}",
            latitude=23.00 + (i % 10) * 0.004,
            longitude=72.50 + (i // 10) * 0.004,
            heading_deg=(i * 37) % 360,
            codec=codec, fps=fps, media_path=clip,
            anpr_capable=(i % 4 == 0),
            # Every thirteenth camera drops mid-run, so reconnection is
            # measured rather than assumed.
            drop_after_s=25.0 if i % 13 == 9 else None,
        ))
    return cams


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", type=int, default=50)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--catalogue-url", default="")
    ap.add_argument("--stagger-ms", type=int, default=200)
    ap.add_argument("--max-concurrent-opens", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # Diagnostics go to stderr so --json emits a parseable document.
    def note(msg: str = "", end: str = "\n") -> None:
        print(msg, end=end, file=sys.stderr, flush=True)

    gateway = None
    catalogue_url = args.catalogue_url

    if not catalogue_url:
        from sentinel_sandbox.gateway import SandboxGateway
        from sentinel_sandbox.rtsp_server import make_clip

        media = pathlib.Path("/tmp/sentinel-poc-media")
        media.mkdir(exist_ok=True)
        note(f"{DIM}encoding source media ...{RESET}")
        h264 = make_clip(str(media / "h264.mp4"), "h264", seconds=8.0, fps=15.0)
        hevc = make_clip(str(media / "hevc.mp4"), "hevc", seconds=8.0, fps=12.0)
        slow = make_clip(str(media / "slow.mp4"), "h264", seconds=8.0, fps=4.0)

        gateway = SandboxGateway()
        for cam in _spread_estate(args.cameras, h264, hevc, slow):
            gateway.add(cam)
        gateway.start()
        catalogue_url = gateway.catalogue_url
        note(f"{DIM}local sandbox gateway: {catalogue_url}{RESET}")

    specs, report = load_from_sentinel(catalogue_url)
    note(f"{BOLD}catalogue{RESET}: {report.summary()}")

    estate = LiveEstate(stagger_ms=args.stagger_ms,
                        max_concurrent_opens=args.max_concurrent_opens)

    t_connect = time.time()
    estate.add(specs)

    # Let the estate come up, then measure for the requested window.
    settled = estate.wait_until_settled(timeout_s=90.0,
                                        min_online=max(1, len(specs) // 2))
    connect_wall = time.time() - t_connect
    note(f"{DIM}estate settled={settled} in {connect_wall:.1f}s{RESET}")

    # Snapshot, wait, snapshot: everything is a delta over a known window.
    start = {cid: (r.health.frames, time.time())
             for cid, r in estate.readers.items()}
    t0 = time.time()
    deadline = t0 + args.seconds
    while time.time() < deadline:
        time.sleep(1.0)
        h = estate.health()
        note(f"\r{DIM}  t+{time.time()-t0:5.0f}s  online={h.online} "
             f"degraded={h.degraded} reconnecting={h.reconnecting} "
             f"offline={h.offline}  frames={h.frames}{RESET}   ", end="")
    note()
    window = time.time() - t0

    rows = []
    for cid, reader in sorted(estate.readers.items()):
        f0, _ = start.get(cid, (0, t0))
        produced = reader.health.frames - f0
        rows.append({
            "camera_id": cid,
            "status": str(reader.health.status),
            "codec": reader.health.codec,
            "resolution": f"{reader.health.width}x{reader.health.height}",
            "frames_in_window": produced,
            "fps_from_pts": round(reader.health.observed_fps, 2),
            "fps_wall": round(produced / window, 2) if window else 0.0,
            "discontinuities": reader.health.discontinuities,
            "reconnects": reader.health.reconnects,
            "last_error": reader.health.last_error,
        })

    health = estate.health()
    estate.stop()
    if gateway:
        gateway.stop()

    online_rows = [r for r in rows if r["status"] in
                   (str(CameraStatus.ONLINE), str(CameraStatus.DEGRADED))]
    pts_fps = [r["fps_from_pts"] for r in online_rows if r["fps_from_pts"] > 0]

    summary = {
        "cameras_requested": len(specs),
        "window_s": round(window, 1),
        "connect_wall_s": round(connect_wall, 1),
        "settled": settled,
        "online": health.online,
        "degraded": health.degraded,
        "reconnecting": health.reconnecting,
        "offline": health.offline,
        "total_frames": health.frames,
        "discontinuities": health.discontinuities,
        "reconnects": health.reconnects,
        "fps_from_pts_median": round(statistics.median(pts_fps), 2) if pts_fps else 0.0,
        "fps_from_pts_min": round(min(pts_fps), 2) if pts_fps else 0.0,
        "fps_from_pts_max": round(max(pts_fps), 2) if pts_fps else 0.0,
        "stagger_ms": args.stagger_ms,
        "max_concurrent_opens": args.max_concurrent_opens,
    }

    if args.json:
        print(json.dumps({"summary": summary, "cameras": rows}, indent=1))
        return 0

    print(f"\n{BOLD}LIVE POC — MEASURED{RESET}")
    print("─" * 72)
    for k, v in summary.items():
        print(f"  {k:26s} {v}")
    print(f"\n{BOLD}per camera (first 15){RESET}")
    print(f"  {'camera':10s} {'status':13s} {'codec':6s} {'res':10s} "
          f"{'fps(pts)':>8s} {'frames':>7s} {'disc':>5s} {'recon':>6s}")
    for r in rows[:15]:
        print(f"  {r['camera_id']:10s} {r['status']:13s} {r['codec']:6s} "
              f"{r['resolution']:10s} {r['fps_from_pts']:>8.2f} "
              f"{r['frames_in_window']:>7d} {r['discontinuities']:>5d} "
              f"{r['reconnects']:>6d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
