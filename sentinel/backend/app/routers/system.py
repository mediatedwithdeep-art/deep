"""Dashboard statistics, analytics, sightings, system health and audit."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from .. import db
from ..deps import (
    CurrentUserDep, dept_filter, dept_scope_sighting, require, require_reason,
    sees_all_departments, write_audit,
)

router = APIRouter(tags=["system"])


@router.get("/dashboard")
async def dashboard(user: Annotated[object, Depends(require("analytics:read"))]):
    """The command-centre headline numbers.

    One query against a view rather than a dozen counts, because this is
    polled every few seconds by every connected wall and the naive version
    is how a dashboard takes its own database down.
    """
    stats = await db.fetch_one("SELECT * FROM v_dashboard_stats")
    recent = await db.fetch_all("""
        SELECT alert_id, timestamp, alert_type::text AS alert_type,
               severity::text AS severity, title, camera_ref, camera_name,
               vehicle_track_id, plate, latitude, longitude, confidence
        FROM alert WHERE state='NEW'
        ORDER BY timestamp DESC LIMIT 12""")
    top = await db.fetch_all("""
        SELECT vehicle_track_id, best_plate, vehicle_type::text AS vehicle_type,
               vehicle_color, camera_count, sighting_count, last_seen,
               round(total_distance_m::numeric) AS total_distance_m,
               EXTRACT(EPOCH FROM (last_seen - first_seen))::int AS duration_seconds
        FROM vehicle WHERE camera_count >= 2
        ORDER BY last_seen DESC LIMIT 10""")
    return {"stats": stats, "recent_alerts": recent, "active_tracks": top}


@router.get("/analytics/timeline")
async def analytics_timeline(
    user: Annotated[object, Depends(require("analytics:read"))],
    hours: int = Query(6, ge=1, le=168),
    bucket_minutes: int = Query(15, ge=1, le=1440),
):
    """Sightings, plate reads and alerts over time."""
    rows = await db.fetch_all("""
        WITH buckets AS (
            SELECT generate_series(
                date_trunc('hour', now() - make_interval(hours => %s)),
                now(), make_interval(mins => %s)) AS bucket)
        SELECT b.bucket,
               (SELECT count(*) FROM vehicle_sighting s
                 WHERE s.timestamp >= b.bucket
                   AND s.timestamp < b.bucket + make_interval(mins => %s)) AS sightings,
               (SELECT count(*) FROM plate_read p
                 WHERE p.timestamp >= b.bucket
                   AND p.timestamp < b.bucket + make_interval(mins => %s)) AS plate_reads,
               (SELECT count(*) FROM alert a
                 WHERE a.timestamp >= b.bucket
                   AND a.timestamp < b.bucket + make_interval(mins => %s)) AS alerts
        FROM buckets b ORDER BY b.bucket""",
        (hours, bucket_minutes, bucket_minutes, bucket_minutes, bucket_minutes))
    return {"buckets": rows, "bucket_minutes": bucket_minutes}


@router.get("/analytics/cameras")
async def analytics_cameras(user: Annotated[object, Depends(require("analytics:read"))]):
    # The view is estate-wide by design. Naming every camera in every
    # department, with its activity, is a full inventory disclosure, so
    # it is joined back to the registry and scoped here.
    scope_sql, scope_params = dept_filter(user, "d.code")
    rows = await db.fetch_all(f"""
        SELECT va.* FROM v_camera_activity_1h va
        JOIN camera c ON c.camera_id = va.camera_id
        JOIN department d ON d.id = c.department_id
        WHERE {scope_sql}
        ORDER BY va.sightings DESC""", tuple(scope_params))
    return {"items": rows}


@router.get("/analytics/vehicle-mix")
async def analytics_vehicle_mix(
    user: Annotated[object, Depends(require("analytics:read"))],
    hours: int = Query(24, ge=1, le=168)):
    scope_sql, scope_params = dept_scope_sighting(user, "s")
    by_type = await db.fetch_all(f"""
        SELECT s.vehicle_type::text AS vehicle_type, count(*) AS n
        FROM vehicle_sighting s
        WHERE s.timestamp > now() - make_interval(hours => %s) AND {scope_sql}
        GROUP BY 1 ORDER BY n DESC""", (hours, *scope_params))
    by_color = await db.fetch_all(f"""
        SELECT COALESCE(s.vehicle_color,'unknown') AS color, count(*) AS n
        FROM vehicle_sighting s
        WHERE s.timestamp > now() - make_interval(hours => %s) AND {scope_sql}
        GROUP BY 1 ORDER BY n DESC LIMIT 12""", (hours, *scope_params))
    return {"by_type": by_type, "by_color": by_color}


@router.get("/analytics/anpr")
async def analytics_anpr(user: Annotated[object, Depends(require("analytics:read"))],
                         hours: int = Query(24, ge=1, le=168)):
    """ANPR performance, reported honestly.

    Read rate is shown per camera CLASS, because a wide-angle surveillance
    camera physically cannot resolve a plate and averaging it together with
    a dedicated ANPR lane produces a number that describes nothing.
    """
    overall = await db.fetch_one("""
        SELECT count(*) AS reads,
               count(*) FILTER (WHERE valid_format) AS valid_format,
               count(*) FILTER (WHERE corrected) AS lexicon_corrected,
               round(avg(confidence)::numeric, 3) AS mean_confidence,
               count(DISTINCT normalized_plate) AS distinct_plates
        FROM plate_read WHERE timestamp > now() - make_interval(hours => %s)""", (hours,))
    by_class = await db.fetch_all("""
        SELECT c.anpr_capable,
               count(DISTINCT c.camera_id) AS cameras,
               count(s.*) AS sightings,
               count(s.plate_normalized) AS plate_reads,
               round(100.0 * count(s.plate_normalized) / NULLIF(count(s.*),0), 1) AS read_rate_pct
        FROM camera c
        LEFT JOIN vehicle_sighting s ON s.camera_id = c.id
             AND s.timestamp > now() - make_interval(hours => %s)
        WHERE c.status <> 'DISABLED'
        GROUP BY c.anpr_capable ORDER BY c.anpr_capable DESC""", (hours,))
    top = await db.fetch_all("""
        SELECT normalized_plate, count(*) AS reads,
               count(DISTINCT camera_ref) AS cameras,
               max(timestamp) AS last_seen
        FROM plate_read WHERE timestamp > now() - make_interval(hours => %s)
        GROUP BY 1 HAVING count(DISTINCT camera_ref) > 1
        ORDER BY cameras DESC, reads DESC LIMIT 20""", (hours,))
    return {"overall": overall, "by_camera_class": by_class,
            "cross_camera_plates": top,
            "note": ("Read rate is reported per camera class deliberately. "
                     "Wide-angle surveillance cameras cannot resolve a plate at "
                     "any settings, so a blended estate-wide figure would be "
                     "meaningless.")}


@router.get("/sightings")
async def list_sightings(
    user: Annotated[object, Depends(require("sighting:read"))],
    camera_id: str | None = None,
    plate: str | None = None,
    vehicle_type: str | None = None,
    minutes: int = Query(15, ge=1, le=10080),
    limit: int = Query(200, ge=1, le=2000),
):
    scope_sql, scope_params = dept_scope_sighting(user, "s")
    where = [scope_sql, "s.timestamp > now() - make_interval(mins => %s)"]
    params: list = [*scope_params, minutes]
    if camera_id:
        where.append("s.camera_ref = %s"); params.append(camera_id)
    if vehicle_type:
        where.append("s.vehicle_type = %s::vehicle_type"); params.append(vehicle_type)
    if plate:
        from sentinel_core import plate_rules
        where.append("plate_canon(s.plate_normalized) = %s")
        params.append(plate_rules.sql_canonical(plate))
    rows = await db.fetch_all(f"""
        SELECT s.sighting_id, s.timestamp, s.camera_ref, c.name AS camera_name,
               s.vehicle_track_id, s.vehicle_type::text AS vehicle_type,
               s.vehicle_color, s.plate_normalized, s.plate_confidence,
               s.latitude, s.longitude, s.speed_kmph, s.heading_deg, s.quality_score
        FROM vehicle_sighting s LEFT JOIN camera c ON c.id = s.camera_id
        WHERE {' AND '.join(where)}
        ORDER BY s.timestamp DESC LIMIT %s""", params + [limit])
    return {"items": rows, "count": len(rows)}


@router.get("/sightings/live.geojson")
async def live_sightings_geojson(
    user: Annotated[object, Depends(require("sighting:read"))],
    minutes: int = Query(5, ge=1, le=120)):
    """Recent sightings as GeoJSON points for the live map layer."""
    scope_sql, scope_params = dept_scope_sighting(user, "s")
    rows = await db.fetch_all(f"""
        SELECT s.sighting_id, s.timestamp, s.camera_ref, s.vehicle_track_id,
               s.vehicle_type::text AS vehicle_type, s.vehicle_color,
               s.plate_normalized, s.latitude, s.longitude, s.heading_deg, s.speed_kmph
        FROM vehicle_sighting s
        WHERE {scope_sql}
          AND s.timestamp > now() - make_interval(mins => %s)
          AND s.latitude IS NOT NULL
        ORDER BY s.timestamp DESC LIMIT 3000""", (*scope_params, minutes))
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
         "properties": {k: v for k, v in r.items() if k not in ("latitude", "longitude")}}
        for r in rows]}


@router.get("/system/status")
async def system_status(user: Annotated[object, Depends(require("analytics:read"))]):
    """Service health for the monitoring page."""
    from .. import ws
    database = await db.health()
    ingestion = await db.fetch_one("""
        SELECT count(*) FILTER (WHERE last_seen > now() - INTERVAL '90 seconds') AS reporting,
               count(*) AS total,
               max(last_seen) AS most_recent
        FROM camera WHERE status <> 'DISABLED'""")
    throughput = await db.fetch_one("""
        SELECT (SELECT count(*) FROM vehicle_sighting
                 WHERE timestamp > now() - INTERVAL '1 minute') AS sightings_per_min,
               (SELECT count(*) FROM plate_read
                 WHERE timestamp > now() - INTERVAL '1 minute') AS plate_reads_per_min,
               (SELECT round(avg(inference_ms)::numeric,2) FROM camera_health
                 WHERE timestamp > now() - INTERVAL '5 minutes') AS mean_inference_ms,
               (SELECT round(avg(fps_actual)::numeric,2) FROM camera_health
                 WHERE timestamp > now() - INTERVAL '5 minutes') AS mean_fps""")
    partitions = await db.fetch_one("""
        SELECT count(*) AS n FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid""")
    return {
        "database": database,
        "ingestion": ingestion,
        "throughput": throughput,
        "websocket": ws.manager.stats(),
        "partitions": partitions["n"] if partitions else 0,
    }


@router.get("/system/audit")
async def audit_log(request: Request, user: CurrentUserDep,
                    _perm: Annotated[object, Depends(require("audit:read"))],
                    reason: Annotated[str, Depends(require_reason)],
                    hours: int = Query(24, ge=1, le=8760),
                    action: str | None = None,
                    username: str | None = None,
                    denied_only: bool = False,
                    limit: int = Query(200, ge=1, le=2000)):
    """Read the audit log.

    Reading the audit log is itself audited. That is not decoration: an
    audit trail nobody can tamper with silently is the point, and the first
    thing an insider does is check what was recorded about them.
    """
    # Scoped like everything else. An audit trail that shows one
    # department what another department's officers looked at is itself a
    # disclosure -- and the reason to read it, investigating misuse, is
    # satisfied within one's own department. Only the state admin spans all.
    #
    # Rows carrying no department are those written before authentication
    # succeeded, chiefly failed logins. Those are released to a scoped
    # caller only when the username they name belongs to that caller's own
    # department, so a department admin can still see brute-force attempts
    # against their own officers without learning of attempts elsewhere.
    where = ["al.timestamp > now() - make_interval(hours => %s)"]
    params: list = [hours]
    if not sees_all_departments(user):
        if not user.department:
            where.append("FALSE")
        else:
            where.append(
                "(al.department = %s OR (al.department IS NULL AND EXISTS ("
                "  SELECT 1 FROM app_user _au"
                "    JOIN department _ad ON _ad.id = _au.department_id"
                "   WHERE _au.username = al.resource_id AND _ad.code = %s)))")
            params += [user.department, user.department]
    if action:
        where.append("al.action ILIKE %s"); params.append(f"%{action}%")
    if username:
        where.append("al.username = %s"); params.append(username)
    if denied_only:
        where.append("al.result = 'DENIED'")
    rows = await db.fetch_all(f"""
        SELECT al.timestamp, al.username, al.department, al.action, al.resource,
               al.resource_id, al.reason, al.ip_address::text AS ip_address,
               al.result, al.detail
        FROM audit_log al WHERE {' AND '.join(where)}
        ORDER BY al.timestamp DESC LIMIT %s""", params + [limit])
    await write_audit(request, user=user, action="AUDIT_READ",
                      resource="/system/audit", reason=reason,
                      detail={"rows": len(rows), "filter_action": action})
    return {"items": rows, "count": len(rows)}
