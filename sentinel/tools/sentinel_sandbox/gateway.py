"""The local Sentinel sandbox gateway: catalogue + RTSP, on the real contract.

Serves `GET /api/ingest` alongside the RTSP server in `rtsp_server.py`, so
the whole discovery → connect → decode path can be exercised end to end
without the real sandbox host.

  catalogue   GET  http://<host>:<api_port>/api/ingest
  RTSP        rtsp://<host>:8554/stream/<id>
  WHEP        http://<host>:8889/stream/<id>/whep
  HLS         http://<host>:8888/live/stream/<id>/index.m3u8

CONTRACT UNCERTAINTY, STATED PLAINLY
────────────────────────────────────
The Sentinel sandbox host, credentials and API documentation were not
available to this team. The URL *shapes* above come from the challenge
brief and are implemented exactly. The catalogue's **JSON field names are
an assumption**, because the brief does not specify them.

Two things follow, and both are deliberate:

  1. This server emits the most conventional shape (`{"streams": [...]}`
     with `id` / `name` / `rtsp_url` ...).
  2. The *client* that consumes it (`ingestion/sentinel_catalogue.py`)
     accepts a range of plausible spellings and tells you loudly which
     ones it found. Adapting to the real gateway should be a mapping
     change in one file, not a rewrite.

Nothing here ever publishes to, or issues control calls against, a real
Sentinel gateway. It is a local fixture that stands in for one.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .rtsp_server import MediaSource, RtspServer

log = logging.getLogger("sentinel.sandbox.gateway")


@dataclass
class SandboxCamera:
    """One catalogue entry. Mirrors what a real gateway would advertise."""
    camera_id: str
    name: str
    latitude: float
    longitude: float
    heading_deg: float | None = None
    codec: str = "h264"
    fps: float = 15.0
    width: int = 640
    height: int = 360
    location: str = ""
    anpr_capable: bool = False
    media_path: str = ""
    drop_after_s: float | None = None

    def to_catalogue(self, host: str, rtsp_port: int,
                     whep_port: int, hls_port: int) -> dict:
        return {
            "id": self.camera_id,
            "name": self.name,
            "location": self.location or self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "heading_deg": self.heading_deg,
            "codec": self.codec,
            "fps": self.fps,
            "resolution": f"{self.width}x{self.height}",
            "anpr_capable": self.anpr_capable,
            "status": "online",
            "rtsp_url": f"rtsp://{host}:{rtsp_port}/stream/{self.camera_id}",
            "whep_url": f"http://{host}:{whep_port}/stream/{self.camera_id}/whep",
            "hls_url": f"http://{host}:{hls_port}/live/stream/{self.camera_id}/index.m3u8",
        }


class SandboxGateway:
    """Catalogue HTTP + RTSP, started and stopped together."""

    def __init__(self, host: str = "127.0.0.1", api_port: int = 0,
                 rtsp_port: int = 0, whep_port: int = 8889,
                 hls_port: int = 8888):
        self.host = host
        self.cameras: list[SandboxCamera] = []
        self.rtsp = RtspServer(host=host, port=rtsp_port)
        self._api_port = api_port
        self.whep_port = whep_port
        self.hls_port = hls_port
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.request_count = 0
        # Lets a test simulate a camera being added or removed upstream.
        self._hidden: set[str] = set()

    # ── registration ──────────────────────────────────────────────────

    def add(self, cam: SandboxCamera) -> None:
        self.cameras.append(cam)
        self.rtsp.add(MediaSource(
            camera_id=cam.camera_id, path=cam.media_path, codec=cam.codec,
            loop=True, drop_after_s=cam.drop_after_s))

    def hide(self, camera_id: str) -> None:
        """Remove a camera from the catalogue without stopping its stream —
        the reconciler must notice and retire it."""
        self._hidden.add(camera_id)

    def unhide(self, camera_id: str) -> None:
        self._hidden.discard(camera_id)

    # ── contract ──────────────────────────────────────────────────────

    @property
    def api_port(self) -> int:
        return self._http.server_address[1] if self._http else self._api_port

    @property
    def catalogue_url(self) -> str:
        return f"http://{self.host}:{self.api_port}/api/ingest"

    def catalogue(self) -> dict:
        visible = [c for c in self.cameras if c.camera_id not in self._hidden]
        return {
            "streams": [c.to_catalogue(self.host, self.rtsp.port,
                                       self.whep_port, self.hls_port)
                        for c in visible],
            "count": len(visible),
        }

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        self.rtsp.start()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # keep test output readable
                pass

            def do_GET(self):
                gateway.request_count += 1
                if self.path.rstrip("/") in ("/api/ingest", "/api/ingest"):
                    body = json.dumps(gateway.catalogue()).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

        self._http = ThreadingHTTPServer((self.host, self._api_port), Handler)
        self._thread = threading.Thread(target=self._http.serve_forever,
                                        daemon=True, name="sandbox-api")
        self._thread.start()
        log.info("sandbox catalogue at %s (%d cameras)",
                 self.catalogue_url, len(self.cameras))

    def stop(self) -> None:
        if self._http:
            self._http.shutdown()
            self._http.server_close()
        self.rtsp.stop()

    def __enter__(self) -> "SandboxGateway":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
