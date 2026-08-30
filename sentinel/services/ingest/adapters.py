"""
Source adapters: absorb camera heterogeneity at the edge so that everything
downstream sees one shape -- a camera_id and a stream URL.

The rule that keeps this maintainable: adapters answer exactly three
questions and nothing else.
    discover()   -> what devices are on this network?
    stream_urls()-> main and sub stream URLs for this device
    probe()      -> is it alive, and what are its actual properties?

Anything vendor-specific that leaks past this boundary becomes a permanent
tax on every service downstream, so it does not leak past this boundary.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import json
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote, urlparse, urlunparse


# ─────────────────────────────────────────────────────────────────────────
# Vendor URL templates.
#
# The `{ch}` channel and the SUB-stream selector are the two things people
# get wrong. Note every template below resolves to the SUB-stream: that is
# what the AI pipeline consumes, and it cuts decode+network cost ~8x versus
# the main stream (see docs/01-architecture.md 1.3).
# ─────────────────────────────────────────────────────────────────────────

VENDOR_TEMPLATES: dict[str, dict[str, str]] = {
    "hikvision": {
        "main": "rtsp://{host}:{port}/Streaming/Channels/{ch}01",
        "sub":  "rtsp://{host}:{port}/Streaming/Channels/{ch}02",
    },
    "dahua": {
        "main": "rtsp://{host}:{port}/cam/realmonitor?channel={ch}&subtype=0",
        "sub":  "rtsp://{host}:{port}/cam/realmonitor?channel={ch}&subtype=1",
    },
    "cpplus": {   # Dahua OEM, very common in Indian government estates
        "main": "rtsp://{host}:{port}/cam/realmonitor?channel={ch}&subtype=0",
        "sub":  "rtsp://{host}:{port}/cam/realmonitor?channel={ch}&subtype=1",
    },
    "axis": {
        "main": "rtsp://{host}:{port}/axis-media/media.amp",
        "sub":  "rtsp://{host}:{port}/axis-media/media.amp?resolution=640x480",
    },
    "uniview": {
        "main": "rtsp://{host}:{port}/media/video1",
        "sub":  "rtsp://{host}:{port}/media/video2",
    },
    "hi3520_oem": {   # the huge Sofia/XMEye OEM long tail
        "main": "rtsp://{host}:{port}/user={user}_password={pwd}_channel={ch}_stream=0.sdp",
        "sub":  "rtsp://{host}:{port}/user={user}_password={pwd}_channel={ch}_stream=1.sdp",
    },
    "onvif_generic": {
        "main": "rtsp://{host}:{port}/onvif{ch}",
        "sub":  "rtsp://{host}:{port}/onvif{ch}",
    },
}

# Order matters: try the specific vendors before the generic fallbacks.
PROBE_ORDER = ["hikvision", "dahua", "cpplus", "axis", "uniview",
               "hi3520_oem", "onvif_generic"]


def build_url(vendor: str, host: str, port: int, channel: int,
              user: str | None, pwd: str | None, quality: str = "sub") -> str:
    """Render a vendor template.

    Credentials are URL-encoded, because government DVR passwords contain
    '@', '/' and ':' with cheerful regularity and an unencoded one silently
    produces a URL that parses to a different host.
    """
    tpl = VENDOR_TEMPLATES[vendor][quality]
    url = tpl.format(host=host, port=port, ch=channel,
                     user=quote(user or "", safe=""), pwd=quote(pwd or "", safe=""))
    if user and "{user}" not in tpl:
        p = urlparse(url)
        netloc = f"{quote(user, safe='')}:{quote(pwd or '', safe='')}@{p.hostname}"
        if p.port:
            netloc += f":{p.port}"
        url = urlunparse(p._replace(netloc=netloc))
    return url


def redact(url: str) -> str:
    """Strip credentials before logging. Nothing that touches a stream URL
    may log it raw -- DVR credentials in a log file is how estates get
    compromised after the fact."""
    return re.sub(r"://[^/@]*@", "://***:***@", url or "")


# ─────────────────────────────────────────────────────────────────────────
# Probe
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ProbeResult:
    url: str
    reachable: bool = False
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    gop_size: int | None = None
    rtt_ms: float | None = None
    anpr_capable: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["url"] = redact(self.url)
        return d


def probe_stream(url: str, timeout_s: int = 12) -> ProbeResult:
    """Connect once with ffprobe and record what the stream actually is.

    A camera is not ACTIVE until this succeeds. Registering 50 cameras from
    a spreadsheet and discovering on demo day that 11 of the URLs were wrong
    is the single most common way this kind of project fails.
    """
    r = ProbeResult(url=url)
    cmd = (
        f"ffprobe -v error -rtsp_transport tcp "
        f"-timeout {timeout_s * 1_000_000} "
        f"-select_streams v:0 "
        f"-show_entries stream=codec_name,width,height,avg_frame_rate,has_b_frames "
        f"-of json {shlex.quote(url)}"
    )
    t0 = time.monotonic()
    try:
        out = subprocess.run(shlex.split(cmd), capture_output=True,
                             timeout=timeout_s + 5, text=True)
    except subprocess.TimeoutExpired:
        r.error = f"timeout after {timeout_s}s"
        return r
    r.rtt_ms = (time.monotonic() - t0) * 1000

    if out.returncode != 0:
        r.error = (out.stderr or "").strip()[:400] or f"ffprobe exit {out.returncode}"
        return r

    try:
        streams = json.loads(out.stdout).get("streams") or []
    except json.JSONDecodeError:
        r.error = "unparseable ffprobe output"
        return r
    if not streams:
        r.error = "no video stream"
        return r

    s = streams[0]
    r.reachable = True
    r.codec = s.get("codec_name")
    r.width = s.get("width")
    r.height = s.get("height")
    num, _, den = (s.get("avg_frame_rate") or "0/1").partition("/")
    try:
        r.fps = round(int(num) / int(den), 2) if int(den) else None
    except (ValueError, ZeroDivisionError):
        r.fps = None

    # --- Warnings that predict downstream pain
    if r.codec not in ("h264", "hevc"):
        r.warnings.append(f"codec {r.codec}: no NVDEC hardware decode path, will burn CPU")
    if r.fps and r.fps > 15:
        r.warnings.append(f"{r.fps} fps: sample to 10 fps for AI, 3x cheaper and loses nothing")
    if s.get("has_b_frames"):
        r.warnings.append("B-frames present: adds decode latency, disable on the camera if possible")
    if r.width and r.width > 1280:
        r.warnings.append(f"{r.width}px wide: this looks like a MAIN stream. "
                          "Use the sub-stream for AI unless this is a dedicated ANPR camera.")

    # --- ANPR capability heuristic.
    # Plate width in pixels roughly = image_width * (plate_m / scene_width_m).
    # An Indian plate is ~0.5 m; a 90-degree FOV at 25 m covers ~50 m of
    # scene. So a 1280px stream gives ~13px of plate -- unreadable. Being
    # honest about this per camera stops the CV tier wasting GPU on crops no
    # model can read.
    if r.width:
        r.anpr_capable = r.width >= 1280
        if not r.anpr_capable:
            r.warnings.append("resolution too low for ANPR; camera contributes via ReID + attributes only")
    return r


def autodetect_vendor(host: str, port: int, channel: int,
                      user: str | None, pwd: str | None,
                      timeout_s: int = 8) -> tuple[str, ProbeResult] | None:
    """Walk the template table until something answers.

    This is what makes bulk onboarding of a mixed estate tractable: an
    operator supplies host + credentials, and the system works out the rest
    instead of demanding an RTSP path nobody at the site knows.
    """
    for vendor in PROBE_ORDER:
        url = build_url(vendor, host, port, channel, user, pwd, "sub")
        res = probe_stream(url, timeout_s)
        if res.reachable:
            return vendor, res
    return None
