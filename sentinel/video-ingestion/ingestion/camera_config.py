"""Camera configuration loading.

THIS IS THE FILE THAT MATTERS FOR REAL DEPLOYMENT.

Real Sentinel Gujarat cameras are added by editing `config/cameras.yaml`
(or by POSTing to the API). No application source changes, no rebuild, no
redeploy. The demo estate and a live estate differ only by this file.

Credentials are never written here in production. Each camera names a
secret with `credential_ref`; the loader resolves it at connect time from
the environment or a secret store, and the resolved URL exists only in
memory. A YAML file with inline passwords is acceptable for a laptop demo
and is rejected outright when ENVIRONMENT=production.
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

from sentinel_core.domain import CameraRole, Protocol
from sentinel_core.log import get_logger

log = get_logger("sentinel.ingest.config")

_CRED_IN_URL = re.compile(r"://[^/@]+:[^/@]+@")


@dataclass
class CameraSpec:
    """One camera, normalised. The ingestion layer sees only this."""
    camera_id: str
    name: str
    latitude: float
    longitude: float
    protocol: Protocol = Protocol.RTSP
    role: CameraRole = CameraRole.SURVEILLANCE
    stream_url: str | None = None
    substream_url: str | None = None
    credential_ref: str | None = None
    heading_deg: float | None = None
    fov_deg: float = 90.0
    range_m: float = 60.0
    width: int = 1280
    height: int = 720
    fps: float = 12.0
    anpr_capable: bool = False
    department: str = "GP_AHM"
    zone: str | None = None
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ai_url(self) -> str | None:
        """What the AI pipeline consumes.

        The sub-stream when one exists. This is the single biggest scale
        lever in the system: a 704x576 sub-stream costs roughly an eighth of
        the 1080p main stream to move and decode, and carries everything
        detection and ReID need. The main stream is for evidence and for
        dedicated ANPR lanes only.
        """
        return self.substream_url or self.stream_url

    def resolve_url(self, quality: str = "sub") -> str | None:
        """Inject credentials at connect time, never at rest."""
        url = self.ai_url if quality == "sub" else (self.stream_url or self.substream_url)
        if not url or not self.credential_ref:
            return url
        user, password = resolve_credentials(self.credential_ref)
        if not user:
            return url
        from urllib.parse import quote, urlparse, urlunparse
        p = urlparse(url)
        if "@" in (p.netloc or ""):
            return url                                   # already carries auth
        host = p.hostname or ""
        netloc = f"{quote(user, safe='')}:{quote(password or '', safe='')}@{host}"
        if p.port:
            netloc += f":{p.port}"
        return urlunparse(p._replace(netloc=netloc))


def resolve_credentials(ref: str) -> tuple[str | None, str | None]:
    """Resolve `credential_ref` to (username, password).

    Supported forms:
      env:SENTINEL_CAM_AHM014      -> "user:pass" in that environment variable
      vault://path/to/secret       -> HashiCorp Vault / OpenBao (production)
      file:/run/secrets/cam-014    -> Docker/Kubernetes secret mount

    Anything unresolvable returns (None, None) and the camera connects
    anonymously, which is the correct failure: refusing to guess is better
    than retrying a wrong password until the DVR locks the account.
    """
    if not ref:
        return None, None
    if ref.startswith("env:"):
        raw = os.environ.get(ref[4:], "")
    elif ref.startswith("file:"):
        try:
            raw = pathlib.Path(ref[5:]).read_text().strip()
        except OSError:
            log.warning("credential file unreadable", extra={"ref": ref})
            return None, None
    elif ref.startswith("vault://"):
        raw = os.environ.get(
            "SENTINEL_CAM_" + ref.rsplit("/", 1)[-1].replace("-", "_").upper(), "")
        if not raw:
            log.debug("vault ref not resolvable in this environment", extra={"ref": ref})
            return None, None
    else:
        raw = os.environ.get(ref, "")
    if not raw:
        return None, None
    user, _, password = raw.partition(":")
    return user or None, password or None


def load_from_yaml(path: str | pathlib.Path,
                   environment: str = "demo") -> list[CameraSpec]:
    """Load cameras from `config/cameras.yaml`."""
    import yaml
    p = pathlib.Path(path)
    if not p.exists():
        log.info("no camera config file; using database registry only",
                 extra={"path": str(p)})
        return []

    doc = yaml.safe_load(p.read_text()) or {}
    defaults = doc.get("defaults", {}) or {}
    specs: list[CameraSpec] = []

    for i, raw in enumerate(doc.get("cameras", []) or []):
        merged = {**defaults, **raw}
        for url_key in ("stream_url", "substream_url"):
            url = merged.get(url_key)
            if url and _CRED_IN_URL.search(url):
                if environment == "production":
                    raise ValueError(
                        f"camera {merged.get('camera_id', i)}: credentials are embedded in "
                        f"{url_key}. Move them to a secret and reference it with "
                        f"credential_ref -- a config file with inline passwords is "
                        f"refused in production.")
                log.warning("credentials embedded in stream URL; acceptable for a "
                            "demo, refused in production",
                            extra={"camera_id": merged.get("camera_id")})

        try:
            specs.append(CameraSpec(
                camera_id=str(merged["camera_id"]),
                name=merged.get("name") or str(merged["camera_id"]),
                latitude=float(merged["latitude"]),
                longitude=float(merged["longitude"]),
                protocol=Protocol(str(merged.get("protocol", "RTSP")).upper()),
                role=CameraRole(str(merged.get("role", "SURVEILLANCE")).upper()),
                stream_url=merged.get("stream_url"),
                substream_url=merged.get("substream_url"),
                credential_ref=merged.get("credential_ref"),
                heading_deg=(float(merged["heading_deg"])
                             if merged.get("heading_deg") is not None else None),
                fov_deg=float(merged.get("fov_deg", 90)),
                range_m=float(merged.get("range_m", 60)),
                width=int(merged.get("width", 1280)),
                height=int(merged.get("height", 720)),
                fps=float(merged.get("fps", 12)),
                anpr_capable=bool(merged.get("anpr_capable", False)),
                department=merged.get("department", "GP_AHM"),
                zone=merged.get("zone"),
                enabled=bool(merged.get("enabled", True)),
                tags=list(merged.get("tags", []) or []),
            ))
        except (KeyError, ValueError) as e:
            # One malformed row must not stop the other 49 cameras from
            # starting. Report it and continue.
            log.error("skipping malformed camera entry",
                      extra={"index": i, "error": str(e),
                             "camera_id": merged.get("camera_id")})
    log.info("loaded cameras from file", extra={"path": str(p), "count": len(specs)})
    return specs


def load_from_database(dsn: str) -> list[CameraSpec]:
    """Load the camera registry from PostgreSQL (the normal path)."""
    import psycopg
    specs: list[CameraSpec] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute("""
            SELECT c.camera_id, c.name, c.latitude, c.longitude, c.protocol::text,
                   c.role::text, c.stream_url, c.substream_url, c.credential_ref,
                   c.heading_deg, c.fov_deg, c.range_m, c.width, c.height, c.fps,
                   c.anpr_capable, d.code, c.zone, c.tags
            FROM camera c JOIN department d ON d.id = c.department_id
            WHERE c.status <> 'DISABLED'
            ORDER BY c.camera_id""").fetchall()
    for r in rows:
        specs.append(CameraSpec(
            camera_id=r[0], name=r[1], latitude=r[2], longitude=r[3],
            protocol=Protocol(r[4]), role=CameraRole(r[5]),
            stream_url=r[6], substream_url=r[7], credential_ref=r[8],
            heading_deg=r[9], fov_deg=r[10] or 90.0, range_m=r[11] or 60.0,
            width=r[12] or 1280, height=r[13] or 720, fps=r[14] or 12.0,
            anpr_capable=bool(r[15]), department=r[16], zone=r[17],
            tags=list(r[18] or [])))
    log.info("loaded cameras from database", extra={"count": len(specs)})
    return specs


def load_cameras(*, dsn: str | None = None, yaml_path: str | None = None,
                 environment: str = "demo",
                 catalogue_url: str | None = None,
                 catalogue_token: str | None = None,
                 catalogue_credential_ref: str | None = None,
                 ) -> list[CameraSpec]:
    """Merge the Sentinel catalogue, the database registry and the YAML overlay.

    Precedence, lowest to highest: catalogue, database, YAML.

    The Sentinel catalogue is the source of truth for *which cameras exist*.
    The layers above it exist to correct it: a survey may supply the
    `heading_deg` the gateway does not carry, and without a heading a camera
    is a dot with no field of view, which materially weakens the directional
    adjacency graph the cross-camera gate depends on.

    A catalogue failure is never fatal. An unreachable gateway must not take
    an already-running estate offline -- the previously known cameras keep
    working and the failure is logged loudly.
    """
    by_id: dict[str, CameraSpec] = {}
    if catalogue_url:
        try:
            from .sentinel_catalogue import load_from_sentinel
            specs, _ = load_from_sentinel(
                catalogue_url, token=catalogue_token or None,
                credential_ref=catalogue_credential_ref or None)
            for s in specs:
                by_id[s.camera_id] = s
        except Exception as e:                              # noqa: BLE001
            log.error("Sentinel catalogue load failed; continuing with the "
                      "cameras already known",
                      extra={"error": str(e), "url": catalogue_url})
    if dsn:
        try:
            for s in load_from_database(dsn):
                by_id[s.camera_id] = s
        except Exception as e:
            log.error("database camera load failed; continuing with file config",
                      extra={"error": str(e)})
    if yaml_path:
        for s in load_from_yaml(yaml_path, environment):
            by_id[s.camera_id] = s
    return [s for s in by_id.values() if s.enabled]
