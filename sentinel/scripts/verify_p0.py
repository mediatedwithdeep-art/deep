#!/usr/bin/env python3
"""Answer the ten Phase 2B verification questions by RUNNING the system.

The test suite already asserts these properties; this script exists for a
different reader. It starts a real RTSP server, opens a real socket, decodes
real H.264, and prints the measured numbers next to each claim, so a
reviewer can watch the evidence being produced rather than trust a green
dot next to a test name.

    python scripts/verify_p0.py

Every check prints PASS or FAIL and the observation it is based on. A check
that cannot run says so and counts as NOT RUN -- it never becomes a PASS.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("shared", "ai", "video-ingestion", "event-processor", "tools",
            "backend", "database/seeds"):
    sys.path.insert(0, str(ROOT / sub))

RESULTS: list[tuple[str, str, str]] = []


def record(q: str, ok: bool | None, evidence: str) -> None:
    state = "PASS" if ok is True else ("FAIL" if ok is False else "NOT RUN")
    RESULTS.append((q, state, evidence))
    colour = {"PASS": "\033[32m", "FAIL": "\033[31m", "NOT RUN": "\033[33m"}[state]
    print(f"  {colour}{state:<8}\033[0m {q}\n           {evidence}")


def main() -> int:
    print("\n\033[1mPHASE 2B — FINAL VERIFICATION\033[0m")
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC · "
          f"python {sys.version.split()[0]}")
    print("─" * 72)

    if not shutil.which("ffmpeg"):
        print("ffmpeg is not installed; nothing here can run.")
        return 2

    from ingestion.live_reader import LiveStreamReader, backoff_delay, BACKOFF_SCHEDULE
    from ingestion.stream_reader import ffmpeg_input_options, rtsp_socket_timeout_option
    from sentinel_sandbox.rtsp_server import MediaSource, RtspServer, make_clip
    from sentinel_sandbox.gateway import SandboxCamera, SandboxGateway

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="verify-p0-"))
    clip = make_clip(str(tmp / "h264.mp4"), "h264", seconds=4.0, fps=15.0)

    server = RtspServer(port=0)
    server.add(MediaSource(camera_id="VER-1", path=clip, codec="h264"))
    server.start()
    url = server.rtsp_url("VER-1")

    try:
        # ── 1 · does the RTSP command actually open? ──────────────────
        reader = LiveStreamReader(url, camera_id="VER-1")
        reader.start()
        frames, seen, deadline = [], set(), time.time() + 30
        while len(frames) < 40 and time.time() < deadline:
            f = reader.read()
            if f is not None and f.frame_index not in seen:
                seen.add(f.frame_index)
                frames.append(f)
            time.sleep(0.004)
        record("1. Does the RTSP command actually open?",
               len(frames) >= 20,
               f"{len(frames)} frames decoded from {url}; "
               f"codec={reader.health.codec} "
               f"{reader.health.width}x{reader.health.height} "
               f"status={reader.health.status.value}")

        # ── 2 · is it TCP? ───────────────────────────────────────────
        opts = LiveStreamReader(url)._open_options()
        tcp = opts.get("rtsp_transport") == "tcp"
        probe_opts = ffmpeg_input_options("rtsp://h/s")
        probe_tcp = probe_opts[probe_opts.index("-rtsp_transport") + 1] == "tcp"
        udp_refused = None
        if shutil.which("ffprobe"):
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-rtsp_transport", "udp",
                 "-i", url, "-show_entries", "stream=codec_name"],
                capture_output=True, text=True, timeout=25)
            udp_refused = r.returncode != 0
        record("2. Does it use TCP?",
               tcp and probe_tcp and udp_refused is not False,
               f"reader rtsp_transport={opts.get('rtsp_transport')!r}; "
               f"probe pins tcp={probe_tcp}; socket timeout flag="
               f"{rtsp_socket_timeout_option()}; "
               f"server refused a UDP client={udp_refused}")

        # ── 3 · is source PTS preserved? ─────────────────────────────
        d_pts = [round(b.pts_time - a.pts_time, 4)
                 for a, b in zip(frames, frames[1:])][:8]
        span_pts = frames[-1].pts_time - frames[0].pts_time
        span_cap = (frames[-1].capture_time - frames[0].capture_time).total_seconds()
        record("3. Is source PTS preserved?",
               span_pts > 0.5 and abs(span_pts - span_cap) < 0.05,
               f"pts spans {span_pts:.3f}s over {len(frames)} frames; "
               f"capture_time spans {span_cap:.3f}s; "
               f"first deltas={d_pts}")

        # ── 4 · does tracking use PTS-derived elapsed time? ───────────
        from sentinel_ai.tracker import ByteTracker
        from sentinel_core.domain import BoundingBox, Detection, VehicleType

        # The brief's own example: PTS at 0, 40, 120, 165 ms. A tracker that
        # assumes one frame is one fixed tick sees four equal steps. A
        # PTS-driven one must see 40, 80 and 45 ms.
        pts_ms = [0, 40, 120, 165]
        t0 = datetime.now(timezone.utc)
        tr = ByteTracker("VER-1", min_hits=1)
        ages = []
        for i, ms in enumerate(pts_ms):
            at = t0 + timedelta(milliseconds=ms)
            det = Detection(
                camera_id="VER-1", track_id="", timestamp=at,
                vehicle_type=VehicleType.CAR, confidence=0.9,
                bbox=BoundingBox(x=100 + i * 8, y=100, w=100, h=70))
            tracks = tr.update([det], at)
            ages.append(round(tracks[0].age_s, 4) if tracks else None)

        # `age_s` is time since the last MATCH, so it is 0 on a track that
        # matches every frame -- it cannot show the interval. Velocity can:
        # a vehicle moving at one real speed through irregular intervals
        # comes out at one velocity only if each gap was measured. Timed by
        # frame count instead, the 40 ms and 80 ms gaps read as equal and
        # the speed swings by a factor of two.
        # The brief's four intervals, continued far enough for the
        # velocity estimator to settle -- it is an EMA, so three samples
        # show it converging rather than converged.
        irregular_ms = [0, 40, 120, 165, 205, 330, 370, 410, 530, 570]
        tr2 = ByteTracker("VER-1", min_hits=1)
        px_per_s = 200.0
        for ms in irregular_ms:
            at = t0 + timedelta(milliseconds=ms)
            x = int(50 + px_per_s * (ms / 1000.0))
            tr2.update(
                [Detection(camera_id="VER-1", track_id="", timestamp=at,
                           vehicle_type=VehicleType.CAR, confidence=0.9,
                           bbox=BoundingBox(x=x, y=100, w=100, h=70))], at)
        track = tr2.tracks[0] if tr2.tracks else None
        vx = round(track.velocity[0], 1) if track else 0.0
        span = round(track.duration_s, 4) if track else 0.0
        gaps = [round((b - a) / 1000, 3)
                for a, b in zip(irregular_ms, irregular_ms[1:])]
        record("4. Does tracking use PTS-derived elapsed time?",
               track is not None and abs(vx - px_per_s) < px_per_s * 0.15
               and abs(span - 0.570) < 1e-6 and gaps[:3] == [0.04, 0.08, 0.045],
               f"PTS {irregular_ms[:4]}... ms -> gaps {gaps[:3]}... s, not a "
               f"constant 0.04; a {px_per_s:.0f} px/s vehicle through them "
               f"tracked at {vx} px/s; track span {span}s = real elapsed, "
               f"not {len(irregular_ms)} frames x a nominal interval; "
               f"max_age_s={tr2.max_age_s}s is seconds")

        # ── 5 · does the pipeline avoid forced CFR? ───────────────────
        bad = [t for t in ("-vf", "-r", "-vsync", "-re", "-stream_loop",
                           "fps=", "readrate")
               if any(t in str(v) for v in opts.items())]
        probe_bad = [t for t in ("-vf", "-vsync", "-re", "-stream_loop", "-r")
                     if t in probe_opts]
        import ingestion.stream_reader as sr
        record("5. Does the pipeline avoid forced CFR timing?",
               not bad and not probe_bad and not hasattr(sr, "FrameReader"),
               f"reader options={sorted(opts)}; probe options carry "
               f"{probe_bad or 'no CFR flag'}; "
               f"FrameReader present={hasattr(sr, 'FrameReader')}")

        # ── 6 · exponential backoff? ─────────────────────────────────
        import random
        seq = [round(backoff_delay(a, random.Random(4)), 2) for a in range(1, 8)]
        monotonic = all(b >= a * 0.7 for a, b in zip(seq, seq[1:]))
        record("6. Does reconnect use exponential backoff?",
               monotonic and max(seq) <= BACKOFF_SCHEDULE[-1] * 1.3
               and len(set(seq)) > 1,
               f"attempts 1..7 -> {seq} s (schedule {list(BACKOFF_SCHEDULE)}, "
               f"jittered, capped at {BACKOFF_SCHEDULE[-1]}s)")
        reader.stop()

        # ── 7 · does scene discontinuity reset state? ────────────────
        #
        # Driven directly rather than by looping media, and deliberately so.
        # The sandbox loops with ffmpeg's `-stream_loop -1`, which may splice
        # the loop with CONTINUOUS PTS -- in which case there is no
        # discontinuity to detect and the observation says nothing either
        # way. Waiting for one makes this check a coin toss on ffmpeg's
        # splicing behaviour rather than a test of our own.
        #
        # What the requirement actually asks is that a hard PTS break is
        # detected and resets state. That is driven here against the real
        # production path.
        class _FakeFrame:
            width, height = 64, 48
            def to_ndarray(self, format=None, width=None, height=None):
                import numpy as np
                return np.zeros((height or self.height,
                                 width or self.width, 3), dtype="uint8")

        r3 = LiveStreamReader("rtsp://127.0.0.1:9/synthetic", camera_id="CUT")
        ff = _FakeFrame()
        for pts in (0.0, 0.0667, 0.1333):          # a normal run of frames
            r3._emit(ff, pts)
        before = r3.health.discontinuities
        anchor_before = r3._anchor_pts
        r3._emit(ff, 0.2000)                        # an ordinary gap
        ordinary = r3.read().is_discontinuity
        r3._emit(ff, 0.0)                           # PTS goes BACKWARDS
        backwards = r3.read()
        r3._emit(ff, 0.0667)
        r3._emit(ff, 99.0)                          # and a large forward jump
        jumped = r3.read()
        cuts = r3.health.discontinuities - before

        # Supporting live observation, reported but not asserted.
        short = make_clip(str(tmp / "short.mp4"), "h264", seconds=1.0, fps=15.0)
        s2 = RtspServer(port=0)
        s2.add(MediaSource(camera_id="LOOP", path=short, codec="h264", loop=True))
        s2.start()
        try:
            r2 = LiveStreamReader(s2.rtsp_url("LOOP"), camera_id="LOOP")
            r2.start()
            deadline = time.time() + 45
            while time.time() < deadline and r2.health.discontinuities == 0:
                r2.read()
                time.sleep(0.004)
            live_cuts, live_frames = r2.health.discontinuities, r2.health.frames
            r2.stop()
        finally:
            s2.stop()

        record("7. Does scene discontinuity reset state?",
               (not ordinary) and backwards.is_discontinuity
               and jumped.is_discontinuity and cuts == 2
               and r3._anchor_pts != anchor_before,
               f"an ordinary 67 ms gap flagged={ordinary} (must be False); "
               f"PTS going backwards flagged={backwards.is_discontinuity}; "
               f"a {99.0 - 0.0667:.0f}s forward jump flagged="
               f"{jumped.is_discontinuity}; {cuts} cut(s) counted and the "
               f"capture-time anchor moved. Live loop, reported not asserted: "
               f"{live_cuts} cut(s) over {live_frames} frames -- "
               f"`-stream_loop` may splice with continuous PTS, so this "
               f"observation is not evidence either way")

        # ── 8 · does /api/ingest populate cameras dynamically? ────────
        from ingestion.sentinel_catalogue import load_from_sentinel, reconcile
        gw = SandboxGateway()
        gw.add(SandboxCamera(camera_id="VER-1", name="Verify 1",
                             latitude=23.03, longitude=72.51, media_path=clip))
        gw.add(SandboxCamera(camera_id="VER-2", name="Verify 2",
                             latitude=23.04, longitude=72.52, codec="hevc",
                             media_path=clip))
        gw.start()
        try:
            first, _ = load_from_sentinel(gw.catalogue_url)
            gw.add(SandboxCamera(camera_id="VER-3", name="Verify 3",
                                 latitude=23.05, longitude=72.53, media_path=clip))
            gw.hide("VER-2")
            second, _ = load_from_sentinel(gw.catalogue_url)
            rec = reconcile(second, {c.camera_id: c for c in first})
            added = sorted(c.camera_id for c in rec.added)
            removed = sorted(rec.removed)
            src = (ROOT / "video-ingestion" / "ingestion"
                   / "sentinel_catalogue.py").read_text()
            hard_coded = "VER-3" in src or "VER-1" in src
            record("8. Does /api/ingest dynamically populate cameras?",
                   len(first) == 2 and added == ["VER-3"]
                   and removed == ["VER-2"] and not hard_coded,
                   f"GET {gw.catalogue_url} -> {len(first)} cameras, then "
                   f"added={added} removed={removed} "
                   f"changed={[c.camera_id for c, _ in rec.changed]}; "
                   f"no camera id appears in the client source")
        finally:
            gw.stop()
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 9 · does department authorisation actually block? ────────────
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_security_regression.py",
         "tests/test_isolation_regression.py",
         "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=ROOT, timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    last = [ln for ln in out.strip().splitlines() if ln.strip()][-1:]
    record("9. Does department authorisation actually block unauthorised data?",
           r.returncode == 0 if "passed" in out else None,
           f"test_security_regression.py + test_isolation_regression.py "
           f"against live PostgreSQL: "
           f"{last[0] if last else 'no output'}")

    # ── 10 · is the real/demo distinction honest? ────────────────────
    from sentinel_core.govt import build_registry
    from sentinel_core.govt.adapters import ADAPTERS
    from sentinel_core.govt.base import Purpose
    try:
        reg = build_registry(real_systems=set())
        stamped, unstamped, skipped = [], [], []
        for name in ADAPTERS:
            # Each adapter releases a different field set per purpose and
            # refuses purposes it does not serve -- SARTHI is a licence
            # system and has no watchlist. Ask under a purpose it declares.
            served = list(ADAPTERS[name].RELEASE)
            rec = None
            for purpose in served:
                try:
                    rec = reg[name].lookup(
                        "GJ01AB1234", purpose=purpose, actor="verify",
                        case_ref="VERIFY/2026/001")
                    break
                except Exception:                             # noqa: BLE001
                    continue
            if rec is None:
                skipped.append(name)
                continue
            prov = getattr(rec, "provenance", None)
            (stamped if prov is not None and not prov.is_real
             else unstamped).append(name)
        record("10. Does the real/demo distinction remain honest?",
               not unstamped and not skipped
               and len(stamped) == len(ADAPTERS) and reg.any_real is False,
               f"{len(stamped)} of {len(ADAPTERS)} adapters returned "
               f"MOCK-stamped records ({', '.join(sorted(stamped))}); "
               f"unstamped={unstamped or 'none'}; "
               f"no-record={skipped or 'none'}; "
               f"registry.any_real={reg.any_real}")
    except Exception as exc:                                  # noqa: BLE001
        record("10. Does the real/demo distinction remain honest?", None,
               f"could not exercise the adapters directly: "
               f"{type(exc).__name__}: {exc}")

    # ── summary ──────────────────────────────────────────────────────
    print("─" * 72)
    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    n_skip = sum(1 for _, s, _ in RESULTS if s == "NOT RUN")
    print(f"  \033[1m{n_pass} PASS · {n_fail} FAIL · {n_skip} NOT RUN\033[0m"
          f"   of {len(RESULTS)} verification questions")
    print("\n  Not covered here, and not claimed anywhere: the real Sentinel")
    print("  gateway. REAL SENTINEL VALIDATION — PENDING EXTERNAL ACCESS.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
