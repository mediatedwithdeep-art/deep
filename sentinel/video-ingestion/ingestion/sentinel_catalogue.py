"""Sentinel gateway catalogue client — discovery, not configuration.

THE RULE THIS FILE EXISTS TO ENFORCE
────────────────────────────────────
The camera catalogue is **never hard-coded**. `GET /api/ingest` on the
Sentinel gateway is the source of truth: which cameras exist, what they are
called, where they are, and which URLs serve them. A camera added upstream
appears here without an edit; one removed upstream is retired here. There is
no list of camera identifiers anywhere in this repository that a live
deployment depends on.

CONTRACT UNCERTAINTY, STATED PLAINLY
────────────────────────────────────
The sandbox host, credentials and API documentation were not available to
this team. The challenge brief specifies the URL *shapes*:

    catalogue   GET  <base>/api/ingest
    RTSP             rtsp://<host>:8554/stream/<id>
    WHEP             http://<host>:8889/stream/<id>/whep
    HLS              http://<host>:8888/live/stream/<id>/index.m3u8

but not the catalogue's JSON field names. Rather than guess one spelling
and fail silently against the real gateway, the parser accepts a range of
plausible spellings for each field and **reports which ones it actually
found** (`FieldReport`). Pointing at the real sandbox is then a mapping
change in one dictionary, made with evidence, not a rewrite.

Where a URL is absent from the catalogue it is derived from the documented
shape using the catalogue's own base host — derivation is a fallback, never
the primary source.

WHAT THIS CLIENT WILL NOT DO
────────────────────────────
It performs read-only discovery. It never publishes a stream to the
gateway, never calls a control API, and never writes to it. The only verb
it uses is GET.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from sentinel_core.domain import CameraRole, Protocol
from sentinel_core.log import get_logger

from .camera_config import CameraSpec

log = get_logger("sentinel.ingest.catalogue")

DEFAULT_RTSP_PORT = 8554
DEFAULT_WHEP_PORT = 8889
DEFAULT_HLS_PORT = 8888

# Accepted spellings, most specific first. The real gateway will use one of
# these or something close; whichever it is, `FieldReport` will name it.
_ALIASES: dict[str, tuple[str, ...]] = {
    "id":        ("id", "stream_id", "camera_id", "cameraId", "streamId",
                  "name", "key", "path"),
    "name":      ("name", "title", "label", "display_name", "description",
                  "location", "camera_name"),
    "location":  ("location", "place", "address", "site", "junction", "area"),
    "latitude":  ("latitude", "lat", "y"),
    "longitude": ("longitude", "lon", "lng", "long", "x"),
    "heading":   ("heading_deg", "heading", "bearing", "azimuth", "direction_deg"),
    "rtsp":      ("rtsp_url", "rtsp", "rtspUrl", "url", "source", "source_url",
                  "stream_url", "uri"),
    "whep":      ("whep_url", "whep", "whepUrl", "webrtc_url", "webrtc"),
    "hls":       ("hls_url", "hls", "hlsUrl", "m3u8", "llhls_url", "playlist"),
    "codec":     ("codec", "video_codec", "encoding", "vcodec"),
    "fps":       ("fps", "frame_rate", "framerate", "frames_per_second"),
    "width":     ("width", "w"),
    "height":    ("height", "h"),
    "resolution": ("resolution", "res", "size"),
    "anpr":      ("anpr_capable", "anpr", "is_anpr", "lpr_capable", "lpr"),
    "status":    ("status", "state", "health", "online"),
    "department": ("department", "dept", "owner", "agency", "organisation"),
}

# Where the list of cameras might live in the response envelope.
_LIST_KEYS = ("streams", "items", "cameras", "data", "results", "paths",
              "feeds", "sources")


@dataclass
class FieldReport:
    """Which spellings this gateway actually used.

    Logged at startup so a mismatch against the real Sentinel API is a
    visible, diagnosable line rather than a silently empty estate.
    """
    resolved: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    envelope_key: str | None = None
    entry_count: int = 0

    def summary(self) -> str:
        found = ", ".join(f"{k}<-{v}" for k, v in sorted(self.resolved.items()))
        miss = ",".join(sorted(self.missing)) or "none"
        return (f"catalogue: {self.entry_count} entries under "
                f"{self.envelope_key or '<root list>'}; mapped [{found}]; "
                f"absent [{miss}]")


class CatalogueError(RuntimeError):
    pass


def _pick(entry: dict, key: str, report: FieldReport | None = None):
    for alias in _ALIASES[key]:
        if alias in entry and entry[alias] not in (None, ""):
            if report is not None:
                report.resolved.setdefault(key, alias)
            return entry[alias]
    if report is not None and key not in report.resolved:
        if key not in report.missing:
            report.missing.append(key)
    return None


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "on", "online")
    return bool(v)


def _split_resolution(v) -> tuple[int | None, int | None]:
    if not isinstance(v, str) or "x" not in v.lower():
        return None, None
    a, _, b = v.lower().partition("x")
    try:
        return int(a.strip()), int(b.strip())
    except ValueError:
        return None, None


def _host_of(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname or "127.0.0.1"


def fetch_catalogue(base_url: str, timeout_s: float = 10.0,
                    token: str | None = None) -> dict:
    """GET the catalogue. Read-only, always.

    `base_url` may be the gateway root or the full endpoint; both are
    accepted because operators will paste either.
    """
    url = base_url.rstrip("/")
    if not url.endswith("/api/ingest"):
        url = f"{url}/api/ingest"
    req = urllib.request.Request(url, method="GET",
                                 headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise CatalogueError(f"catalogue HTTP {exc.code} from {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CatalogueError(f"catalogue unreachable at {url}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CatalogueError(f"catalogue at {url} is not JSON") from exc


def _entries(doc) -> tuple[list[dict], str | None]:
    """Find the camera list inside whatever envelope the gateway uses."""
    if isinstance(doc, list):
        return [e for e in doc if isinstance(e, dict)], None
    if isinstance(doc, dict):
        for key in _LIST_KEYS:
            v = doc.get(key)
            if isinstance(v, list):
                return [e for e in v if isinstance(e, dict)], key
        # A mapping of id -> entry is also plausible.
        vals = list(doc.values())
        if vals and all(isinstance(v, dict) for v in vals):
            out = []
            for k, v in doc.items():
                e = dict(v)
                e.setdefault("id", k)
                out.append(e)
            return out, "<mapping>"
    return [], None


def parse_catalogue(doc, gateway_host: str | None = None,
                    department: str = "GP_SENTINEL",
                    rtsp_port: int = DEFAULT_RTSP_PORT,
                    whep_port: int = DEFAULT_WHEP_PORT,
                    hls_port: int = DEFAULT_HLS_PORT,
                    credential_ref: str | None = None,
                    ) -> tuple[list[CameraSpec], FieldReport]:
    """Turn a catalogue document into CameraSpecs.

    One malformed entry must never take the estate offline: it is reported
    and skipped, exactly as the YAML loader does for a bad row in a
    2,000-camera import.
    """
    raw, envelope = _entries(doc)
    report = FieldReport(envelope_key=envelope, entry_count=len(raw))
    specs: list[CameraSpec] = []

    for entry in raw:
        cam_id = _pick(entry, "id", report)
        if not cam_id:
            log.warning("catalogue entry without an identifier, skipped: %s",
                        list(entry)[:6])
            continue
        cam_id = str(cam_id).strip().strip("/")

        rtsp = _pick(entry, "rtsp", report)
        whep = _pick(entry, "whep", report)
        hls = _pick(entry, "hls", report)

        # Derive only what the gateway did not advertise, and derive it from
        # the gateway's own host rather than from anything hard-coded.
        host = gateway_host or (_host_of(rtsp) if rtsp else None)
        if host:
            if not rtsp:
                rtsp = f"rtsp://{host}:{rtsp_port}/stream/{cam_id}"
            if not whep:
                whep = f"http://{host}:{whep_port}/stream/{cam_id}/whep"
            if not hls:
                hls = f"http://{host}:{hls_port}/live/stream/{cam_id}/index.m3u8"

        width = _pick(entry, "width", report)
        height = _pick(entry, "height", report)
        if width is None or height is None:
            rw, rh = _split_resolution(_pick(entry, "resolution", report))
            width = width if width is not None else rw
            height = height if height is not None else rh

        lat = _as_float(_pick(entry, "latitude", report))
        lon = _as_float(_pick(entry, "longitude", report))
        if lat is None or lon is None:
            # A camera without coordinates cannot join the adjacency graph,
            # so it cannot participate in the spatio-temporal gate. It is
            # still worth ingesting -- it just has to be visible as
            # ungeolocated rather than silently placed at (0, 0), which is
            # in the Atlantic and would corrupt every travel-time estimate.
            log.warning("camera %s has no coordinates in the catalogue; "
                        "it cannot join the adjacency graph until surveyed",
                        cam_id)

        name = _pick(entry, "name", report) or cam_id
        anpr = _as_bool(_pick(entry, "anpr", report))
        fps = _as_float(_pick(entry, "fps", report))
        dept = _pick(entry, "department", report) or department

        spec = CameraSpec(
            camera_id=cam_id,
            name=str(name),
            latitude=lat if lat is not None else 0.0,
            longitude=lon if lon is not None else 0.0,
            protocol=Protocol.RTSP,
            role=CameraRole.ANPR if anpr else CameraRole.SURVEILLANCE,
            stream_url=rtsp,
            credential_ref=credential_ref,
            heading_deg=_as_float(_pick(entry, "heading", report)),
            width=int(width) if width else 1280,
            height=int(height) if height else 720,
            fps=fps if fps else 12.0,
            anpr_capable=anpr,
            department=str(dept),
            enabled=True,
            tags=["sentinel"],
            extra={
                "source": "sentinel-catalogue",
                "whep_url": whep,
                "hls_url": hls,
                "codec": _pick(entry, "codec", report),
                "catalogue_status": _pick(entry, "status", report),
                "geolocated": lat is not None and lon is not None,
            },
        )
        specs.append(spec)

    return specs, report


def load_from_sentinel(base_url: str, timeout_s: float = 10.0,
                       token: str | None = None,
                       credential_ref: str | None = None,
                       department: str = "GP_SENTINEL",
                       ) -> tuple[list[CameraSpec], FieldReport]:
    doc = fetch_catalogue(base_url, timeout_s=timeout_s, token=token)
    specs, report = parse_catalogue(
        doc, gateway_host=_host_of(base_url), department=department,
        credential_ref=credential_ref)
    log.info("%s", report.summary())
    if report.missing:
        log.warning("catalogue did not supply: %s -- if this is the real "
                    "gateway, extend _ALIASES in sentinel_catalogue.py",
                    ", ".join(sorted(report.missing)))
    return specs, report


# ── reconciliation ───────────────────────────────────────────────────

@dataclass
class Reconciliation:
    """What changed between the catalogue and what we are running."""
    added: list[CameraSpec] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[tuple[CameraSpec, dict[str, tuple]]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def summary(self) -> str:
        return (f"+{len(self.added)} added, -{len(self.removed)} removed, "
                f"~{len(self.changed)} changed, {len(self.unchanged)} unchanged")


# Fields whose change actually requires action. Deliberately not every
# field: a cosmetic rename must not tear down a working stream.
_MATERIAL = ("stream_url", "latitude", "longitude", "heading_deg",
             "anpr_capable", "fps", "width", "height", "enabled")


def reconcile(catalogue: list[CameraSpec],
              running: dict[str, CameraSpec]) -> Reconciliation:
    """Diff the catalogue against what is currently configured.

    The catalogue is the source of truth: a camera it no longer lists is
    retired here. But retirement is reported, never silent -- an operator
    needs to know the estate shrank, because the usual cause is an upstream
    fault rather than a decommissioning.
    """
    result = Reconciliation()
    seen = set()

    for spec in catalogue:
        seen.add(spec.camera_id)
        current = running.get(spec.camera_id)
        if current is None:
            result.added.append(spec)
            continue
        diffs = {}
        for f in _MATERIAL:
            a, b = getattr(current, f, None), getattr(spec, f, None)
            if a != b:
                diffs[f] = (a, b)
        if diffs:
            result.changed.append((spec, diffs))
        else:
            result.unchanged.append(spec.camera_id)

    for cam_id in running:
        if cam_id not in seen:
            result.removed.append(cam_id)

    return result
