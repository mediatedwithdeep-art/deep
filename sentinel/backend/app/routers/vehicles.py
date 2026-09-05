"""Vehicle search, movement history and cross-camera tracking."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from sentinel_core import plate_rules

from .. import db
from ..deps import (
    CurrentUserDep, dept_scope_sighting, dept_scope_vehicle, require,
    require_reason, sees_all_departments, write_audit,
)

router = APIRouter(prefix="/vehicles", tags=["vehicles"])


@router.get("/search")
async def search_vehicles(
    user: Annotated[object, Depends(require("vehicle:read"))],
    plate: Annotated[str | None, Query(description="Fuzzy: tolerates O/0, I/1, 8/B, 5/S, 2/Z, 6/G")] = None,
    vehicle_type: str | None = None,
    color: str | None = None,
    camera_id: str | None = None,
    zone: str | None = None,
    hours: int = Query(24, ge=1, le=8760),
    min_cameras: int = Query(1, ge=1, le=50),
    limit: int = Query(100, ge=1, le=1000),
):
    """Search vehicles.

    Plate search is FUZZY by design. OCR confusions are systematic, so
    exact matching would miss most real reads: an officer typing the plate
    from a witness statement should still find the vehicle even if the
    camera read one character differently. Matching happens on the
    confusion-collapsed canonical form, which is indexed, so this stays an
    index lookup rather than a scan with a distance function.
    """
    # A vehicle is visible only if one of THIS caller's cameras saw it.
    # Without this the plate search is a state-wide lookup for every
    # operator in every department.
    scope_sql, scope_params = dept_scope_vehicle(user, "v")
    where = [scope_sql, "v.last_seen > now() - make_interval(hours => %s)"]
    params: list = [*scope_params, hours]

    canonical = None
    if plate:
        canonical = plate_rules.sql_canonical(plate)
        if not canonical:
            raise HTTPException(422, "plate contains no alphanumeric characters")
        where.append("plate_canon(v.best_plate) = %s")
        params.append(canonical)
    if vehicle_type:
        where.append("v.vehicle_type = %s::vehicle_type")
        params.append(vehicle_type)
    if color:
        where.append("v.vehicle_color = %s")
        params.append(color)
    if min_cameras > 1:
        where.append("v.camera_count >= %s")
        params.append(min_cameras)
    if camera_id:
        where.append("EXISTS (SELECT 1 FROM vehicle_sighting s "
                     "WHERE s.vehicle_track_id = v.vehicle_track_id AND s.camera_ref = %s)")
        params.append(camera_id)
    if zone:
        where.append("EXISTS (SELECT 1 FROM vehicle_sighting s JOIN camera c "
                     "ON c.id = s.camera_id WHERE s.vehicle_track_id = v.vehicle_track_id "
                     "AND c.zone = %s)")
        params.append(zone)

    # Counts and the camera list are recomputed over the caller's own
    # sightings. The stored aggregates on `vehicle` are estate-wide, and
    # publishing them would tell one department how many other cameras saw
    # the vehicle, and name them -- the row itself becoming the disclosure
    # the WHERE clause just prevented.
    agg_sql, agg_params = dept_scope_sighting(user, "s")
    rows = await db.fetch_all(f"""
        SELECT v.vehicle_track_id, v.best_plate, v.best_plate_conf,
               v.vehicle_type::text AS vehicle_type, v.vehicle_color,
               v.first_seen, v.last_seen,
               (SELECT count(*) FROM vehicle_sighting s
                 WHERE s.vehicle_track_id = v.vehicle_track_id
                   AND {agg_sql})::int AS sighting_count,
               (SELECT count(DISTINCT s.camera_ref) FROM vehicle_sighting s
                 WHERE s.vehicle_track_id = v.vehicle_track_id
                   AND {agg_sql})::int AS camera_count,
               v.plate_read_count, v.is_watchlisted,
               {'round(v.total_distance_m::numeric)' if sees_all_departments(user)
                else 'NULL::numeric'} AS total_distance_m,
               EXTRACT(EPOCH FROM (v.last_seen - v.first_seen))::int AS duration_seconds,
               (SELECT array_agg(DISTINCT s.camera_ref)
                  FROM vehicle_sighting s
                 WHERE s.vehicle_track_id = v.vehicle_track_id
                   AND {agg_sql}) AS cameras
        FROM vehicle v
        WHERE {' AND '.join(where)}
        ORDER BY v.last_seen DESC
        LIMIT %s""",
        # Placeholder order follows the SQL text: the three scoped
        # sub-selects bind before the WHERE clause, which binds before LIMIT.
        [*agg_params, *agg_params, *agg_params, *params, limit])

    result = {"items": rows, "count": len(rows)}
    if plate:
        # Say plainly that this was a fuzzy search and what it matched on.
        # An operator who thinks they got an exact match may act on a
        # vehicle the system never actually read.
        result["search"] = {
            "query": plate,
            "canonical": canonical,
            "match_type": "fuzzy",
            "note": ("Matched on the confusion-collapsed form, so reads differing "
                     "only by O/0, I/1, 8/B, 5/S, 2/Z or 6/G are included. "
                     "Verify the plate before acting."),
        }
    return result


@router.get("/{vehicle_track_id}")
async def get_vehicle(vehicle_track_id: str,
                      user: Annotated[object, Depends(require("vehicle:read"))]):
    scope_sql, scope_params = dept_scope_vehicle(user, "v")
    agg_sql, agg_params = dept_scope_sighting(user, "s")
    # `total_distance_m` is computed over the whole trajectory, most of
    # which a scoped caller cannot see, so it is withheld rather than
    # reported as if it described their own observations.
    row = await db.fetch_one(f"""
        SELECT v.vehicle_track_id, v.best_plate, v.best_plate_conf,
               v.vehicle_type::text AS vehicle_type, v.vehicle_color, v.make_model,
               v.first_seen, v.last_seen,
               (SELECT count(*) FROM vehicle_sighting s
                 WHERE s.vehicle_track_id = v.vehicle_track_id
                   AND {agg_sql})::int AS sighting_count,
               (SELECT count(DISTINCT s.camera_ref) FROM vehicle_sighting s
                 WHERE s.vehicle_track_id = v.vehicle_track_id
                   AND {agg_sql})::int AS camera_count,
               v.plate_read_count, v.is_watchlisted,
               {'round(v.total_distance_m::numeric)' if sees_all_departments(user)
                else 'NULL::numeric'} AS total_distance_m,
               v.embedding_model
        FROM vehicle v WHERE v.vehicle_track_id = %s AND {scope_sql}""",
        (*agg_params, *agg_params, vehicle_track_id, *scope_params))
    if row is None:
        # 404 rather than 403: confirming the vehicle exists elsewhere is
        # itself a cross-department disclosure an operator could enumerate.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vehicle not found")
    return row


@router.get("/{vehicle_track_id}/timeline")
async def vehicle_timeline(
    vehicle_track_id: str, request: Request,
    user: CurrentUserDep,
    _perm: Annotated[object, Depends(require("vehicle:read"))],
    reason: Annotated[str, Depends(require_reason)],
):
    """Movement history: Camera A -> B -> C with timestamps and confidence.

    Requires a stated purpose (X-Reason header). This endpoint reveals an
    identifiable person's movements, which is exactly the access DPDP Act
    2023 purpose limitation exists to govern, so the reason is recorded in
    the audit log alongside the query.
    """
    # Scoped to the caller's own cameras. A vehicle that crossed a
    # boundary is legitimately visible to both departments, but each sees
    # only the hops its own estate observed -- the other department's
    # camera positions and timings are not theirs to read.
    scope_sql, scope_params = dept_scope_sighting(user, "s")
    sightings = await db.fetch_all(f"""
        SELECT s.sighting_id, s.timestamp, s.camera_ref, c.name AS camera_name,
               c.zone, s.latitude, s.longitude, s.heading_deg, s.speed_kmph,
               s.vehicle_type::text AS vehicle_type, s.vehicle_color,
               s.plate_normalized, s.plate_confidence, s.quality_score,
               s.detection_count
        FROM vehicle_sighting s
        LEFT JOIN camera c ON c.id = s.camera_id
        WHERE s.vehicle_track_id = %s AND {scope_sql}
        ORDER BY s.timestamp""", (vehicle_track_id, *scope_params))
    if not sightings:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no sightings for this vehicle")

    links = await db.fetch_all("""
        SELECT to_sighting_id, decision::text AS decision, score_total, score_plate,
               score_reid, score_color, score_type, score_spatiotemporal,
               travel_expected_s, travel_actual_s, reasons, operator_verdict
        FROM track_link WHERE vehicle_track_id = %s ORDER BY timestamp""",
        (vehicle_track_id,))
    by_sighting = {l["to_sighting_id"]: l for l in links}

    hops = []
    previous = None
    for s in sightings:
        link = by_sighting.get(s["sighting_id"])
        hop = {
            **s,
            "gap_seconds": (int((s["timestamp"] - previous["timestamp"]).total_seconds())
                            if previous else None),
            # How the system decided this sighting belongs to this vehicle,
            # shown per hop. An operator must be able to see which links are
            # certain and which are inferred.
            "association": ({
                "decision": link["decision"],
                "confidence": round(link["score_total"], 3),
                "scores": {"plate": round(link["score_plate"], 3),
                           "appearance": round(link["score_reid"], 3),
                           "colour": round(link["score_color"], 3),
                           "type": round(link["score_type"], 3),
                           "reachability": round(link["score_spatiotemporal"], 3)},
                "travel_expected_s": link["travel_expected_s"],
                "travel_actual_s": link["travel_actual_s"],
                "reasons": link["reasons"],
                "operator_verdict": link["operator_verdict"],
            } if link else {"decision": "SEED", "confidence": 1.0,
                            "reasons": ["first sighting of this vehicle"]}),
        }
        hops.append(hop)
        previous = s

    await write_audit(request, user=user, action="VEHICLE_TIMELINE_READ",
                      resource=f"/vehicles/{vehicle_track_id}/timeline",
                      resource_id=vehicle_track_id, reason=reason,
                      detail={"sightings": len(hops)})

    confirmed = sum(1 for h in hops if h["association"]["decision"] in ("AUTO", "SEED"))
    return {
        "vehicle_track_id": vehicle_track_id,
        "hop_count": len(hops),
        "camera_count": len({h["camera_ref"] for h in hops}),
        "first_seen": hops[0]["timestamp"],
        "last_seen": hops[-1]["timestamp"],
        "confirmed_hops": confirmed,
        "probable_hops": len(hops) - confirmed,
        "hops": hops,
    }


@router.get("/{vehicle_track_id}/track.geojson")
async def vehicle_track_geojson(vehicle_track_id: str,
                                user: Annotated[object, Depends(require("vehicle:read"))]):
    """Trajectory as GeoJSON, ready for MapLibre.

    Sighting points and the observed path are separate features so the map
    can style them differently. Nothing here is an inferred corridor:
    drawing an unobserved segment the same way as an observed one would
    misrepresent evidence.
    """
    scope_sql, scope_params = dept_scope_sighting(user, "s")
    visible = await db.fetch_all(
        f"SELECT DISTINCT s.camera_ref FROM vehicle_sighting s "
        f"WHERE s.vehicle_track_id = %s AND {scope_sql}",
        (vehicle_track_id, *scope_params))
    if not visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vehicle not found")

    row = await db.fetch_one("SELECT vehicle_track_geojson(%s) AS fc", (vehicle_track_id,))
    if row is None or not row["fc"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vehicle not found")
    fc = row["fc"]
    if not sees_all_departments(user):
        # The SQL function is estate-wide by design. Drop the points this
        # caller may not see, and drop the path with them: a line drawn
        # through withheld points would redraw exactly what was withheld.
        allowed = {r["camera_ref"] for r in visible}
        fc = dict(fc)
        fc["features"] = [
            f for f in fc.get("features", [])
            if (f.get("properties") or {}).get("kind") != "path"
            and ((f.get("properties") or {}).get("camera_ref") in allowed)
        ]
    return fc


@router.get("/{vehicle_track_id}/next-cameras")
async def predicted_next_cameras(
    vehicle_track_id: str,
    user: Annotated[object, Depends(require("vehicle:read"))],
    max_travel_s: int = Query(600, ge=60, le=1800),
):
    """Where to look next.

    Given the last confirmed sighting, returns downstream cameras with
    expected arrival windows from the road-network adjacency graph. This is
    the same gate the matcher uses, surfaced for the operator: the map can
    highlight cameras ahead of the vehicle rather than behind it.
    """
    scope_sql, scope_params = dept_scope_sighting(user, "s")
    last = await db.fetch_one(f"""
        SELECT s.camera_id::text AS cid, s.camera_ref, s.timestamp
        FROM vehicle_sighting s
        WHERE s.vehicle_track_id = %s AND {scope_sql}
        ORDER BY s.timestamp DESC LIMIT 1""", (vehicle_track_id, *scope_params))
    if last is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no sightings for this vehicle")

    rows = await db.fetch_all("""
        SELECT camera_ref, camera_name, travel_s, road_dist_m,
               window_start, expected_at, window_end, source
        FROM candidate_cameras(%s::uuid, %s::timestamptz, %s::real, 1.0::real)
        ORDER BY expected_at LIMIT 12""",
        (last["cid"], last["timestamp"], float(max_travel_s)))

    return {
        "vehicle_track_id": vehicle_track_id,
        "from_camera": last["camera_ref"],
        "seen_at": last["timestamp"],
        "candidates": rows,
        "note": ("Reachability from the road network, not a prediction of intent. "
                 f"{len(rows)} of the estate's cameras are reachable in this window."),
    }


class SimilarRequest(BaseModel):
    sighting_id: str
    top_k: int = Field(default=25, ge=1, le=100)
    apply_gate: bool = True
    hours: int = Field(default=24, ge=1, le=720)


@router.post("/similar")
async def find_similar(body: SimilarRequest, request: Request,
                       user: CurrentUserDep,
                       _perm: Annotated[object, Depends(require("vehicle:read"))],
                       reason: Annotated[str, Depends(require_reason)]):
    """Visual search: find this vehicle elsewhere.

    kNN over the ReID embedding index. With `apply_gate` on, results are
    restricted to cameras reachable from the seed sighting -- without it
    this returns every similar-looking vehicle in the estate, which for a
    white hatchback is most of them.
    """
    seed = await db.fetch_one("""
        SELECT s.sighting_id, s.camera_id::text AS cid, s.camera_ref, s.timestamp,
               s.embedding::text AS emb, s.vehicle_type::text AS vehicle_type,
               s.vehicle_color, s.vehicle_track_id
        FROM vehicle_sighting s WHERE s.sighting_id = %s""", (body.sighting_id,))
    if seed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "sighting not found")
    if not seed["emb"]:
        raise HTTPException(422, "this sighting has no appearance embedding "
                                 "(the crop was below the quality gate)")

    where = ["s.embedding IS NOT NULL",
             "s.sighting_id <> %s",
             "s.timestamp > now() - make_interval(hours => %s)"]
    params: list = [seed["emb"], body.sighting_id, body.hours]

    if body.apply_gate:
        where.append("""s.camera_id IN (
            SELECT camera_id FROM candidate_cameras(%s::uuid, %s::timestamptz))""")
        params += [seed["cid"], seed["timestamp"]]

    sc_sql, sc_params = dept_scope_sighting(user, "s")
    where.append(sc_sql)
    params += sc_params

    rows = await db.fetch_all(f"""
        SELECT s.sighting_id, s.timestamp, s.camera_ref, s.vehicle_track_id,
               s.vehicle_type::text AS vehicle_type, s.vehicle_color,
               s.plate_normalized, s.latitude, s.longitude,
               1 - (s.embedding <=> %s::vector) AS cosine
        FROM vehicle_sighting s
        WHERE {' AND '.join(where)}
        ORDER BY s.embedding <=> %s::vector
        LIMIT %s""", params + [seed["emb"], body.top_k])

    await write_audit(request, user=user, action="VISUAL_SEARCH",
                      resource="/vehicles/similar", resource_id=body.sighting_id,
                      reason=reason, detail={"gated": body.apply_gate,
                                             "results": len(rows)})
    return {
        "seed": {k: seed[k] for k in
                 ("sighting_id", "camera_ref", "timestamp", "vehicle_type",
                  "vehicle_color", "vehicle_track_id")},
        "gate_applied": body.apply_gate,
        "matches": rows,
        "note": ("Appearance similarity is not identity. Cosine above ~0.7 is a "
                 "strong candidate; treat anything lower as a lead to verify."
                 if body.apply_gate else
                 "UNGATED search: results are not restricted to reachable cameras "
                 "and will include look-alike vehicles that cannot be the same one."),
    }


class VerdictRequest(BaseModel):
    verdict: Literal["CONFIRMED", "REJECTED"]
    note: str | None = None


@router.post("/links/{link_id}/verdict")
async def link_verdict(link_id: str, body: VerdictRequest, request: Request,
                       user: CurrentUserDep,
                       _perm: Annotated[object, Depends(require("link:verdict"))]):
    """Operator confirms or rejects a PROBABLE association.

    Human verdicts are the training signal that tunes the fusion weights.
    Recording them is what lets the system improve after deployment instead
    of staying frozen at its hackathon accuracy.
    """
    # Scoped in the UPDATE itself: a check-then-write would let the link
    # change hands between the two statements.
    scope_sql, scope_params = dept_scope_vehicle(user, "v")
    n = await db.execute(f"""
        UPDATE track_link tl
           SET operator_verdict=%s, operator_id=%s::uuid, verdict_at=now()
        WHERE tl.id=%s::uuid
          AND EXISTS (SELECT 1 FROM vehicle v
                       WHERE v.vehicle_track_id = tl.vehicle_track_id
                         AND {scope_sql})""",
        (body.verdict, user.id, link_id, *scope_params))
    if n == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    await write_audit(request, user=user, action=f"LINK_{body.verdict}",
                      resource="/vehicles/links", resource_id=link_id,
                      detail={"note": body.note})
    return {"link_id": link_id, "verdict": body.verdict}


@router.get("/links/pending")
async def pending_links(user: Annotated[object, Depends(require("link:verdict"))],
                        limit: int = Query(50, ge=1, le=500)):
    """Probable associations awaiting operator confirmation."""
    scope_sql, scope_params = dept_scope_vehicle(user, "v")
    rows = await db.fetch_all(f"""
        SELECT tl.id::text AS link_id, tl.vehicle_track_id, tl.timestamp,
               tl.score_total, tl.score_plate, tl.score_reid, tl.score_color,
               tl.score_spatiotemporal, tl.travel_expected_s, tl.travel_actual_s,
               tl.reasons, cf.camera_id AS from_camera, ct.camera_id AS to_camera,
               v.best_plate, v.vehicle_type::text AS vehicle_type, v.vehicle_color
        FROM track_link tl
        LEFT JOIN camera cf ON cf.id = tl.from_camera_id
        LEFT JOIN camera ct ON ct.id = tl.to_camera_id
        LEFT JOIN vehicle v ON v.vehicle_track_id = tl.vehicle_track_id
        WHERE tl.decision = 'PROBABLE' AND tl.operator_verdict IS NULL
          AND {scope_sql}
        ORDER BY tl.timestamp DESC LIMIT %s""", (*scope_params, limit))
    return {"items": rows, "count": len(rows)}
