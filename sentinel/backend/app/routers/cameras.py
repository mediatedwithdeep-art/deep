"""Camera registry: onboarding, listing, health, playback URLs."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .. import db
from ..deps import (
    CurrentUserDep, dept_filter, require, require_department,
    sees_all_departments, write_audit,
)

router = APIRouter(prefix="/cameras", tags=["cameras"])

# The Sentinel contract's published ports. Used ONLY to derive a URL the
# catalogue did not supply -- never to rewrite one it did.
WHEP_PORT = 8889
HLS_PORT = 8888


class Location(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_m: float | None = None


class Optics(BaseModel):
    # Not optional in practice. Without a heading a camera is a dot on a
    # map with no field of view, and because the adjacency graph is
    # directional the spatio-temporal gate is materially weaker. Capturing
    # the bearing costs one compass reading per camera during survey and is
    # very expensive to retrofit across thousands of sites.
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    fov_deg: float = Field(default=90, gt=0, le=360)
    range_m: float = Field(default=60, gt=0, le=1000)


class CameraCreate(BaseModel):
    camera_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    department_code: str
    protocol: Literal["RTSP", "ONVIF", "HLS", "DVR", "FILE", "SIMULATED"] = "RTSP"
    role: Literal["SURVEILLANCE", "ANPR", "PTZ", "THERMAL"] = "SURVEILLANCE"
    location: Location
    optics: Optics = Optics()
    stream_url: str | None = None
    substream_url: str | None = None
    # A reference to a secret, never a secret. There is no field on this
    # model that can carry a password.
    credential_ref: str | None = None
    vendor: str | None = None
    model: str | None = None
    signal_class: Literal["CVBS", "AHD", "TVI", "CVI", "IP"] | None = None
    width: int | None = Field(default=None, gt=0, le=16000)
    height: int | None = Field(default=None, gt=0, le=16000)
    fps: float | None = Field(default=None, gt=0, le=240)
    anpr_capable: bool = False
    zone: str | None = None
    district: str | None = None
    tags: list[str] = []


class CameraUpdate(BaseModel):
    name: str | None = None
    location: Location | None = None
    optics: Optics | None = None
    stream_url: str | None = None
    substream_url: str | None = None
    credential_ref: str | None = None
    status: Literal["PENDING", "PROBING", "ONLINE", "DEGRADED",
                    "OFFLINE", "DISABLED"] | None = None
    anpr_capable: bool | None = None
    zone: str | None = None
    tags: list[str] | None = None


_SELECT = """
    SELECT c.camera_id, c.name, c.protocol::text AS protocol, c.role::text AS role,
           c.status::text AS status, c.latitude, c.longitude, c.heading_deg,
           c.fov_deg, c.range_m, c.zone, c.district, d.code AS department,
           c.vendor, c.model, c.signal_class, c.firmware_risk,
           c.codec, c.width, c.height, c.fps, c.anpr_capable,
           c.trust_score, c.last_seen, c.consecutive_failures, c.tags,
           EXTRACT(EPOCH FROM (now() - c.last_seen))::int AS seconds_since_seen,
           ST_AsGeoJSON(c.fov_geom::geometry)::json AS fov_geojson
    FROM camera c JOIN department d ON d.id = c.department_id
"""


@router.get("")
async def list_cameras(
    user: Annotated[object, Depends(require("camera:read"))],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    department: str | None = None,
    zone: str | None = None,
    role: str | None = None,
    anpr_only: bool = False,
    bbox: Annotated[str | None, Query(description="minLon,minLat,maxLon,maxLat")] = None,
    near: Annotated[str | None, Query(description="lon,lat,radius_m")] = None,
    search: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    # Department scope first, so every later clause narrows an already
    # authorised set rather than widening an unauthorised one.
    scope_sql, scope_params = dept_filter(user)
    where: list[str] = ["c.status <> 'DISABLED'", scope_sql]
    params: list = list(scope_params)

    if status_filter:
        where.append("c.status = %s::camera_status")
        params.append(status_filter)
    if department:
        # `?department=` narrows within the caller's scope; it cannot escape
        # it, because scope_sql is already ANDed in above. A department
        # admin asking for someone else's department gets an empty list,
        # which is the truthful answer to "show me what I may see there".
        where.append("d.code = %s")
        params.append(department)
    if zone:
        where.append("c.zone = %s")
        params.append(zone)
    if role:
        where.append("c.role = %s::camera_role")
        params.append(role)
    if anpr_only:
        where.append("c.anpr_capable")
    if search:
        # Trigram index on name makes this an index lookup rather than a scan.
        where.append("(c.name ILIKE %s OR c.camera_id ILIKE %s)")
        params += [f"%{search}%", f"%{search}%"]
    if bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
        except ValueError:
            raise HTTPException(422, "bbox must be minLon,minLat,maxLon,maxLat")
        where.append("c.geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)::geography")
        params += [min_lon, min_lat, max_lon, max_lat]
    if near:
        try:
            lon, lat, radius = (float(v) for v in near.split(","))
        except ValueError:
            raise HTTPException(422, "near must be lon,lat,radius_m")
        where.append("ST_DWithin(c.geom, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, %s)")
        params += [lon, lat, radius]

    sql = f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY c.camera_id LIMIT %s OFFSET %s"
    rows = await db.fetch_all(sql, params + [limit, offset])
    total = await db.fetch_one(
        f"SELECT count(*) AS n FROM camera c JOIN department d ON d.id=c.department_id "
        f"WHERE {' AND '.join(where)}", params)
    return {"items": rows, "total": total["n"] if total else len(rows),
            "limit": limit, "offset": offset}


@router.get("/geojson")
async def cameras_geojson(user: Annotated[object, Depends(require("camera:read"))]):
    """The whole estate as GeoJSON, ready for MapLibre.

    Returns points and field-of-view polygons in one payload so the map
    makes a single request instead of one per camera.
    """
    scope_sql, scope_params = dept_filter(user)
    rows = await db.fetch_all(
        f"{_SELECT} WHERE c.status <> 'DISABLED' AND {scope_sql} "
        f"ORDER BY c.camera_id", scope_params)
    features = []
    for r in rows:
        props = {k: v for k, v in r.items() if k != "fov_geojson"}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
            "properties": {**props, "kind": "camera"},
        })
        if r.get("fov_geojson"):
            features.append({
                "type": "Feature", "geometry": r["fov_geojson"],
                "properties": {"kind": "fov", "camera_id": r["camera_id"],
                               "status": r["status"]},
            })
    return {"type": "FeatureCollection", "features": features}


@router.get("/health")
async def camera_health(user: Annotated[object, Depends(require("camera:read"))]):
    """Estate health.

    Roughly a fifth of a real government camera estate is dead, frozen or
    misaimed at any moment. A VMS that does not surface that is lying to
    its operators, so this is a first-class view rather than a diagnostic.
    """
    scope_sql, scope_params = dept_filter(user)
    summary = await db.fetch_one(f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE c.status='ONLINE')   AS online,
               count(*) FILTER (WHERE c.status='OFFLINE')  AS offline,
               count(*) FILTER (WHERE c.status='DEGRADED') AS degraded,
               count(*) FILTER (WHERE c.last_seen IS NULL OR
                                c.last_seen < now() - INTERVAL '90 seconds') AS stale,
               count(*) FILTER (WHERE c.anpr_capable)      AS anpr_capable,
               count(*) FILTER (WHERE c.firmware_risk IN ('EOL','KNOWN_CVE')) AS firmware_at_risk,
               round(avg(c.trust_score)::numeric, 3)       AS mean_trust
        FROM camera c JOIN department d ON d.id = c.department_id
        WHERE c.status <> 'DISABLED' AND {scope_sql}""", scope_params)
    detail = await db.fetch_all(f"""
        SELECT c.camera_id, c.name, c.status::text AS status, c.zone,
               c.latitude, c.longitude, c.trust_score, c.last_seen,
               c.consecutive_failures, c.firmware_risk, c.anpr_capable,
               h.fps_actual, h.scene_change, h.decode_errors, h.inference_ms, h.message
        FROM camera c
        JOIN department d ON d.id = c.department_id
        LEFT JOIN v_camera_health_latest h ON h.camera_ref = c.camera_id
        WHERE c.status <> 'DISABLED' AND {scope_sql}
        ORDER BY c.trust_score ASC, c.camera_id
        LIMIT 500""", scope_params)
    return {"summary": summary, "cameras": detail}


@router.get("/{camera_id}")
async def get_camera(camera_id: str,
                     user: Annotated[object, Depends(require("camera:read"))]):
    scope_sql, scope_params = dept_filter(user)
    row = await db.fetch_one(
        f"{_SELECT} WHERE c.camera_id = %s AND {scope_sql}",
        [camera_id, *scope_params])
    # 404 rather than 403 for a camera in another department: 403 would
    # confirm the id exists, which is itself harvestable by enumeration.
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "camera not found")
    return row


@router.get("/{camera_id}/stream")
async def camera_stream(camera_id: str,
                        user: Annotated[object, Depends(require("camera:read"))],
                        quality: Literal["sub", "main"] = "sub"):
    """Playback URLs, as published by the camera's source catalogue.

    The API never proxies video. It returns a WHEP endpoint that the
    browser negotiates directly with the media server, so video bytes never
    traverse the API and one slow client cannot affect anyone else.

    It also never returns an RTSP URL. A browser cannot play RTSP, so
    handing one to the frontend can only produce a dead player or an
    operator pasting a credential-bearing URL into VLC. RTSP is the AI
    pipeline's transport and stays server-side; see `stream_url` on the
    camera row, which this endpoint deliberately does not expose.
    """
    row = await db.fetch_one(
        "SELECT c.camera_id, c.name, c.status::text AS status, "
        "       c.protocol::text AS protocol, c.whep_url, c.hls_url, d.code AS department "
        "FROM camera c JOIN department d ON d.id = c.department_id "
        "WHERE c.camera_id=%s", (camera_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "camera not found")
    require_department(user, row["department"])

    whep, hls = row["whep_url"], row["hls_url"]
    # Derive only what the catalogue did not supply, and say which happened.
    # A derived URL is a guess at another system's routing; labelling it
    # means an operator debugging a black player knows whether to look at
    # our configuration or at theirs.
    derived: list[str] = []
    if not whep or not hls:
        import os
        base = os.environ.get("MEDIA_BASE_URL", "http://localhost:8889").rstrip("/")
        host = urlsplit(base).hostname or "localhost"
        scheme = urlsplit(base).scheme or "http"
        if not whep:
            whep = f"{scheme}://{host}:{WHEP_PORT}/stream/{camera_id}/whep"
            derived.append("whep_url")
        if not hls:
            hls = f"{scheme}://{host}:{HLS_PORT}/live/stream/{camera_id}/index.m3u8"
            derived.append("hls_url")

    return {
        "camera_id": camera_id,
        "status": row["status"],
        "whep_url": whep,
        "llhls_url": hls,
        "quality": quality,
        "url_source": "catalogue" if not derived else "derived",
        "derived_fields": derived,
        # Sub-second WebRTC keeps the alert and the picture aligned. With
        # HLS the alert would beat the video by ten seconds and operators
        # would stop trusting both.
        "note": "WHEP (WebRTC) gives 200-500 ms glass-to-glass; LL-HLS is a 2-4 s fallback.",
    }


@router.get("/{camera_id}/sightings")
async def camera_sightings(camera_id: str,
                           user: Annotated[object, Depends(require("sighting:read"))],
                           hours: int = Query(1, ge=1, le=168),
                           limit: int = Query(200, ge=1, le=2000)):
    # Authorise the CAMERA before returning anything observed through it.
    # Sightings inherit their camera's department: a plate read is as
    # sensitive as the lens that read it.
    scope_sql, scope_params = dept_filter(user)
    owner = await db.fetch_one(
        f"SELECT 1 AS ok FROM camera c JOIN department d ON d.id = c.department_id "
        f"WHERE c.camera_id = %s AND {scope_sql}", [camera_id, *scope_params])
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "camera not found")
    rows = await db.fetch_all("""
        SELECT sighting_id, timestamp, vehicle_track_id, vehicle_type::text AS vehicle_type,
               vehicle_color, plate_normalized, plate_confidence, speed_kmph,
               heading_deg, latitude, longitude, quality_score
        FROM vehicle_sighting
        WHERE camera_ref = %s AND timestamp > now() - make_interval(hours => %s)
        ORDER BY timestamp DESC LIMIT %s""", (camera_id, hours, limit))
    return {"camera_id": camera_id, "items": rows}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_camera(body: CameraCreate, request: Request,
                        user: Annotated[object, Depends(require("camera:write"))]):
    dept = await db.fetch_one("SELECT id FROM department WHERE code=%s",
                              (body.department_code,))
    if dept is None:
        raise HTTPException(422, f"unknown department '{body.department_code}'")
    # A department admin onboards into their OWN department. Without this a
    # scoped admin could plant a camera in another department's estate and
    # then read everything it sees -- privilege escalation by INSERT.
    if not sees_all_departments(user) and body.department_code != user.department:
        # Audited before it is refused. An attempt to plant a camera in
        # another department's estate is precisely the event an audit trail
        # exists to capture, and a refusal that leaves no record is one an
        # attacker can retry indefinitely without ever appearing in a review.
        await write_audit(
            request, user=user, action="DENIED:camera:create:cross-department",
            resource="/cameras", resource_id=body.camera_id, result="DENIED",
            detail={"requested_department": body.department_code,
                    "caller_department": user.department})
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"cannot create a camera in department '{body.department_code}'")
    exists = await db.fetch_one("SELECT 1 FROM camera WHERE camera_id=%s",
                                (body.camera_id,))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"camera '{body.camera_id}' already exists")

    await db.execute("""
        INSERT INTO camera (camera_id, name, department_id, protocol, role, status,
            latitude, longitude, altitude_m, heading_deg, fov_deg, range_m,
            stream_url, substream_url, credential_ref, vendor, model, signal_class,
            width, height, fps, anpr_capable, zone, district, tags)
        VALUES (%s,%s,%s,%s::camera_protocol,%s::camera_role,'PENDING',
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (body.camera_id, body.name, dept["id"], body.protocol, body.role,
         body.location.latitude, body.location.longitude, body.location.altitude_m,
         body.optics.heading_deg, body.optics.fov_deg, body.optics.range_m,
         body.stream_url, body.substream_url, body.credential_ref,
         body.vendor, body.model, body.signal_class,
         body.width, body.height, body.fps, body.anpr_capable,
         body.zone, body.district, body.tags))

    await write_audit(request, user=user, action="CAMERA_CREATE",
                      resource="/cameras", resource_id=body.camera_id)
    warnings = []
    if body.optics.heading_deg is None:
        warnings.append("no heading_deg: this camera will have no field-of-view "
                        "polygon and will contribute a weaker spatio-temporal gate")
    if body.anpr_capable and (body.width or 0) < 1280:
        warnings.append("anpr_capable is set but width < 1280px; a plate is "
                        "unlikely to exceed the 90px readability floor")
    return {"camera_id": body.camera_id, "status": "PENDING", "warnings": warnings}


@router.patch("/{camera_id}")
async def update_camera(camera_id: str, body: CameraUpdate, request: Request,
                        user: Annotated[object, Depends(require("camera:write"))]):
    sets: list[str] = []
    params: list = []
    if body.name is not None:
        sets.append("name=%s"); params.append(body.name)
    if body.location is not None:
        sets += ["latitude=%s", "longitude=%s"]
        params += [body.location.latitude, body.location.longitude]
    if body.optics is not None:
        sets += ["heading_deg=%s", "fov_deg=%s", "range_m=%s"]
        params += [body.optics.heading_deg, body.optics.fov_deg, body.optics.range_m]
    for field, value in (("stream_url", body.stream_url),
                         ("substream_url", body.substream_url),
                         ("credential_ref", body.credential_ref),
                         ("zone", body.zone), ("anpr_capable", body.anpr_capable)):
        if value is not None:
            sets.append(f"{field}=%s"); params.append(value)
    if body.status is not None:
        sets.append("status=%s::camera_status"); params.append(body.status)
    if body.tags is not None:
        sets.append("tags=%s"); params.append(body.tags)
    if not sets:
        raise HTTPException(422, "no fields to update")

    # Scope the UPDATE itself rather than checking first and writing after:
    # a check-then-write leaves a window in which the camera moves
    # department between the two statements.
    scope_sql, scope_params = dept_filter(user, "d.code")
    params.append(camera_id)
    params += scope_params
    n = await db.execute(
        f"UPDATE camera c SET {', '.join(sets)} FROM department d "
        f"WHERE d.id = c.department_id AND c.camera_id=%s AND {scope_sql}", params)
    if n == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "camera not found")
    await write_audit(request, user=user, action="CAMERA_UPDATE",
                      resource="/cameras", resource_id=camera_id,
                      detail={"fields": list(body.model_dump(exclude_none=True))})
    return await get_camera(camera_id, user)


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_camera(camera_id: str, request: Request,
                         user: Annotated[object, Depends(require("camera:write"))]):
    """Soft-disable. Registry rows are never hard-deleted, because the
    sightings and evidence that reference them must remain resolvable."""
    scope_sql, scope_params = dept_filter(user, "d.code")
    n = await db.execute(
        f"UPDATE camera c SET status='DISABLED' FROM department d "
        f"WHERE d.id = c.department_id AND c.camera_id=%s AND {scope_sql}",
        [camera_id, *scope_params])
    if n == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "camera not found")
    await write_audit(request, user=user, action="CAMERA_DISABLE",
                      resource="/cameras", resource_id=camera_id)
