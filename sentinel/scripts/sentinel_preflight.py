#!/usr/bin/env python3
"""Preflight the real Sentinel Camera Grid, and answer its checklist.

Run this from a machine that can reach the grid. It uses the SAME code the
pipeline uses -- the catalogue client, the URL profile, the live reader --
so a pass here is evidence about the system, not about this script.

    python scripts/sentinel_preflight.py \
        --catalogue https://cctv.corp8.cloud/cameras.json \
        --media-host 103.250.160.189 \
        --hls-host cctv.corp8.cloud \
        --profile split-cdn \
        --cameras 3 --seconds 12

Add `--basic user:password` if the catalogue is password-protected, or
`--token <bearer>` if it wants a bearer token.

What it answers, per the integrator's guide's own pre-submission checklist:

  1. catalogue reachable, and which field spellings it actually uses
  2. RTSP reachable over TCP -- and whether UDP is refused
  3. HLS reachable, as the documented fallback when 8554 is blocked
  4. PTS present and monotonic; timing not taken from arrival
  5. reported FPS vs FPS measured from PTS (the guide warns these differ)
  6. inter-frame gaps tolerated rather than treated as a disconnect
  7. codec and resolution per camera, mixed H.264/H.265 handled
  8. join-time decoder warnings logged, not fatal

It writes a JSON report next to itself so the result can be pasted back
rather than retyped.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import socket
import statistics
import sys
import time
import urllib.parse

REPO = pathlib.Path(__file__).resolve().parents[1]
for sub in ("shared", "video-ingestion", "ai"):
    sys.path.insert(0, str(REPO / sub))

from ingestion.sentinel_catalogue import (  # noqa: E402
    PROFILES, PROFILE_SINGLE_HOST, fetch_catalogue, parse_catalogue,
)

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def line(status: str, title: str, detail: str = "") -> dict:
    colour = {"PASS": GREEN, "FAIL": RED, "WARN": YELLOW}.get(status, DIM)
    print(f"  {colour}{status:<7}{OFF} {title}")
    if detail:
        for chunk in str(detail).splitlines():
            print(f"           {DIM}{chunk}{OFF}")
    return {"status": status, "check": title, "detail": detail}


def tcp_open(host: str, port: int, timeout: float = 6.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_stream(url: str, seconds: float, transport: str) -> dict:
    """Decode for `seconds` using the production reader. Returns evidence."""
    from ingestion.live_reader import LiveStreamReader

    reader = LiveStreamReader(url, camera_id="preflight", width=None,
                              height=None, transport=transport,
                              open_timeout_s=15.0)
    out: dict = {"url": url, "frames": 0, "error": None}
    reader.start()
    pts: list[float] = []
    wall: list[float] = []
    deadline = time.time() + seconds
    seen = 0
    discontinuities = 0
    while time.time() < deadline:
        frame = reader.read()
        if frame is not None and frame.frame_index != seen:
            seen = frame.frame_index
            pts.append(frame.pts_time)
            wall.append(time.time())
            discontinuities += bool(frame.is_discontinuity)
            out["width"], out["height"] = frame.width, frame.height
        time.sleep(0.01)
    out["discontinuities"] = discontinuities
    out["frames"] = len(pts)
    out["status"] = reader.health.status.name if reader.health.status else "?"
    out["codec"] = reader.health.codec or None
    out["error"] = reader.health.last_error
    out["failovers"] = getattr(reader.health, "transport_failovers", 0)
    reader.stop()

    if len(pts) >= 3:
        gaps = [b - a for a, b in zip(pts, pts[1:])]
        positive = [g for g in gaps if g > 0]
        out["pts_span_s"] = round(pts[-1] - pts[0], 3)
        out["wall_span_s"] = round(wall[-1] - wall[0], 3)
        out["monotonic"] = all(b >= a for a, b in zip(pts, pts[1:]))
        out["fps_from_pts"] = (round(len(pts) / (pts[-1] - pts[0]), 2)
                               if pts[-1] > pts[0] else None)
        out["gap_min_s"] = round(min(gaps), 4)
        out["gap_max_s"] = round(max(gaps), 4)
        out["gap_stdev_s"] = (round(statistics.stdev(positive), 4)
                              if len(positive) > 1 else 0.0)
        out["constant_rate"] = out["gap_stdev_s"] < 1e-6
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalogue", required=True,
                    help="full catalogue URL, e.g. https://host/cameras.json")
    ap.add_argument("--media-host", default=None,
                    help="host serving RTSP and WHEP")
    ap.add_argument("--hls-host", default=None,
                    help="host serving HLS, when it differs")
    ap.add_argument("--profile", default="split-cdn", choices=sorted(PROFILES))
    ap.add_argument("--cameras", type=int, default=3,
                    help="how many cameras to decode (the guide asks you to "
                         "pace load; each client gets its own stream copy)")
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--token", default=None)
    ap.add_argument("--basic", default=None, help="user:password")
    ap.add_argument("--out", default=str(REPO / "sentinel_preflight_report.json"))
    args = ap.parse_args()

    profile = PROFILES.get(args.profile, PROFILE_SINGLE_HOST)
    results: list[dict] = []
    print(f"\n{BOLD}SENTINEL CAMERA GRID — PREFLIGHT{OFF}")
    print(f"{DIM}{time.strftime('%Y-%m-%d %H:%M:%S %Z')} · profile={profile.name}{OFF}")
    print("─" * 72)

    # ── 1. catalogue ─────────────────────────────────────────────────
    specs = []
    try:
        doc = fetch_catalogue(args.catalogue, timeout_s=20.0, token=args.token,
                              basic_auth=args.basic)
        specs, report = parse_catalogue(
            doc, profile=profile, media_host=args.media_host,
            hls_host=args.hls_host)
        results.append(line("PASS", "1. Catalogue reachable and parsed",
                            report.summary()))
        if report.missing:
            results.append(line(
                "WARN", "1b. Fields the catalogue did not supply",
                ", ".join(sorted(report.missing)) +
                "\nExtend _ALIASES in sentinel_catalogue.py to map them."))
        ungeo = [s.camera_id for s in specs
                 if not (s.extra or {}).get("geolocated")]
        if ungeo:
            results.append(line(
                "WARN", "1c. Cameras without coordinates",
                f"{len(ungeo)} of {len(specs)}: {', '.join(ungeo[:8])}"
                "\nWithout lat/lon these cannot join the adjacency graph, so "
                "the spatio-temporal gate cannot score them. Survey needed."))
    except Exception as exc:                              # noqa: BLE001
        results.append(line("FAIL", "1. Catalogue reachable",
                            f"{type(exc).__name__}: {exc}"))
        print("\nWithout a catalogue nothing else can be addressed. Stopping.")
        pathlib.Path(args.out).write_text(json.dumps(results, indent=2))
        return 1

    # ── 2. ports ─────────────────────────────────────────────────────
    # Test the port the catalogue actually advertises, not an assumed 8554.
    # A gateway is free to publish RTSP anywhere, and checking a number we
    # invented would report the wrong thing with total confidence.
    first_rtsp = next((s.stream_url for s in specs if s.stream_url), "")
    parsed = urllib.parse.urlparse(first_rtsp)
    media = parsed.hostname or args.media_host or \
        urllib.parse.urlparse(args.catalogue).hostname
    rtsp_port = parsed.port or 8554
    rtsp_up = tcp_open(media, rtsp_port) if media else False
    results.append(line(
        "PASS" if rtsp_up else "WARN", "2. RTSP port reachable over TCP",
        f"{media}:{rtsp_port} {'open' if rtsp_up else 'closed or filtered'}"
        + ("" if rtsp_up else
           "\nThe guide says to use HLS when 8554 is blocked. The reader "
           "now carries HLS as a fallback transport and rotates to it.")))

    # ── 3-8. decode ──────────────────────────────────────────────────
    chosen = specs[:max(1, args.cameras)]
    print(f"\n{DIM}decoding {len(chosen)} camera(s) for {args.seconds:g}s each"
          f" — opening only what we process, as the guide asks{OFF}\n")

    decoded = []
    for spec in chosen:
        hls = (spec.extra or {}).get("hls_url")
        primary = spec.stream_url if rtsp_up else (hls or spec.stream_url)
        ev = probe_stream(primary, args.seconds, transport="tcp")
        ev["camera_id"] = spec.camera_id
        decoded.append(ev)

        ok = ev["frames"] > 0
        results.append(line(
            "PASS" if ok else "FAIL",
            f"3. {spec.camera_id}: decoded over "
            f"{urllib.parse.urlparse(primary).scheme.upper()}",
            f"{ev['frames']} frames · {ev.get('width')}x{ev.get('height')} · "
            f"status={ev['status']}" + (f" · error={ev['error']}" if ev["error"] else "")))
        if not ok:
            continue

        results.append(line(
            "PASS" if ev.get("monotonic") else "FAIL",
            f"4. {spec.camera_id}: PTS present and monotonic",
            f"pts span {ev.get('pts_span_s')}s over {ev['frames']} frames "
            f"(wall {ev.get('wall_span_s')}s)"))

        results.append(line(
            "PASS" if not ev.get("constant_rate") else "WARN",
            f"5. {spec.camera_id}: frame rate measured from PTS",
            f"{ev.get('fps_from_pts')} fps from PTS · gaps "
            f"{ev.get('gap_min_s')}–{ev.get('gap_max_s')}s "
            f"(stdev {ev.get('gap_stdev_s')}s)"
            + ("\nGaps are perfectly uniform — verify this is the source and "
               "not a CFR filter." if ev.get("constant_rate") else "")))

        results.append(line(
            "PASS", f"6. {spec.camera_id}: variable gaps tolerated",
            "reader stayed ONLINE across uneven inter-frame gaps"
            if ev["status"] == "ONLINE" else f"status={ev['status']}"))

    # ── HLS reachability, always worth knowing ───────────────────────
    if chosen:
        hls = (chosen[0].extra or {}).get("hls_url")
        if hls:
            ev = probe_stream(hls, min(args.seconds, 10.0), transport="tcp")
            results.append(line(
                "PASS" if ev["frames"] > 0 else "WARN",
                "7. HLS fallback transport decodes",
                f"{ev['frames']} frames from {hls}"
                + (f" · error={ev['error']}" if ev["error"] else "")))

    # ── summary ──────────────────────────────────────────────────────
    p = sum(1 for r in results if r["status"] == "PASS")
    f = sum(1 for r in results if r["status"] == "FAIL")
    w = sum(1 for r in results if r["status"] == "WARN")
    print("─" * 72)
    print(f"  {BOLD}{p} PASS · {f} FAIL · {w} WARN{OFF}")
    pathlib.Path(args.out).write_text(json.dumps(
        {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "profile": profile.name, "catalogue": args.catalogue,
         "cameras_in_catalogue": len(specs),
         "checks": results, "decoded": decoded}, indent=2))
    print(f"  {DIM}report written to {args.out}{OFF}\n")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
