"""Alerts, watchlist and alert rules."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from sentinel_core import plate_rules

from .. import db
from ..deps import (
    CurrentUserDep, dept_scope_alert, dept_scope_vehicle, require,
    sees_all_departments, write_audit,
)

router = APIRouter(tags=["alerts"])


@router.get("/alerts")
async def list_alerts(
    user: Annotated[object, Depends(require("alert:read"))],
    state: str | None = None,
    severity: str | None = None,
    alert_type: str | None = None,
    vehicle_track_id: str | None = None,
    camera_id: str | None = None,
    hours: int = Query(24, ge=1, le=8760),
    limit: int = Query(100, ge=1, le=1000),
):
    # An alert inherits its department from the camera that raised it.
    scope_sql, scope_params = dept_scope_alert(user, "a")
    where = [scope_sql, "a.timestamp > now() - make_interval(hours => %s)"]
    params: list = [*scope_params, hours]
    for column, value, cast in (("state", state, "::alert_state"),
                                ("severity", severity, "::alert_severity"),
                                ("alert_type", alert_type, "::alert_type"),
                                ("vehicle_track_id", vehicle_track_id, ""),
                                ("camera_ref", camera_id, "")):
        if value:
            where.append(f"a.{column} = %s{cast}")
            params.append(value)

    rows = await db.fetch_all(f"""
        SELECT a.alert_id, a.timestamp, a.alert_type::text AS alert_type,
               a.severity::text AS severity, a.state::text AS state,
               a.title, a.message, a.camera_ref, a.camera_name,
               a.vehicle_track_id, a.sighting_id, a.plate,
               a.latitude, a.longitude, a.confidence, a.evidence,
               a.acknowledged_at, a.note
        FROM alert a
        WHERE {' AND '.join(where)}
        ORDER BY a.timestamp DESC LIMIT %s""", params + [limit])
    return {"items": rows, "count": len(rows)}


@router.get("/alerts/summary")
async def alert_summary(user: Annotated[object, Depends(require("alert:read"))]):
    # Counts are scoped too. An unscoped total tells one department how
    # busy another's estate is, and a false-positive rate computed over
    # alerts the caller may not read is not a number about their estate.
    scope_sql, scope_params = dept_scope_alert(user, "a")
    counts = await db.fetch_one(f"""
        SELECT count(*) FILTER (WHERE a.state='NEW') AS open,
               count(*) FILTER (WHERE a.state='NEW'
                                AND a.severity IN ('HIGH','CRITICAL')) AS urgent,
               count(*) FILTER (WHERE a.state='ACKNOWLEDGED') AS acknowledged,
               count(*) FILTER (WHERE a.state='FALSE_POSITIVE') AS false_positive,
               count(*) AS total_24h
        FROM alert a
        WHERE a.timestamp > now() - INTERVAL '24 hours' AND {scope_sql}""",
        tuple(scope_params))
    by_type = await db.fetch_all(f"""
        SELECT a.alert_type::text AS alert_type, a.severity::text AS severity,
               count(*) AS n
        FROM alert a
        WHERE a.timestamp > now() - INTERVAL '24 hours' AND {scope_sql}
        GROUP BY 1,2 ORDER BY n DESC""", tuple(scope_params))
    # The false-positive rate is the number that determines whether
    # operators keep trusting the system. Surfacing it makes the honest
    # thing the visible thing.
    fp = counts["false_positive"] or 0
    resolved = fp + (counts["acknowledged"] or 0)
    return {"counts": counts, "by_type": by_type,
            "false_positive_rate": round(fp / resolved, 3) if resolved else None}


class AckRequest(BaseModel):
    state: Literal["ACKNOWLEDGED", "RESOLVED", "FALSE_POSITIVE"] = "ACKNOWLEDGED"
    note: str | None = Field(default=None, max_length=1000)


@router.post("/alerts/{alert_id}/ack")
async def acknowledge_alert(alert_id: str, body: AckRequest, request: Request,
                            user: CurrentUserDep,
                            _perm: Annotated[object, Depends(require("alert:ack"))]):
    # Scoped in the UPDATE itself. Acknowledging is a write: an operator
    # who could ack a neighbouring department's CRITICAL alert could
    # silence a stolen-vehicle hit in an estate they have no authority over.
    scope_sql, scope_params = dept_scope_alert(user, "alert")
    n = await db.execute(f"""
        UPDATE alert SET state=%s::alert_state, acknowledged_by=%s::uuid,
               acknowledged_at=now(),
               resolved_at = CASE WHEN %s IN ('RESOLVED','FALSE_POSITIVE')
                                  THEN now() ELSE resolved_at END,
               note=COALESCE(%s, note)
        WHERE alert_id=%s AND {scope_sql}""",
        (body.state, user.id, body.state, body.note, alert_id, *scope_params))
    if n == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert not found")
    await write_audit(request, user=user, action=f"ALERT_{body.state}",
                      resource="/alerts", resource_id=alert_id,
                      detail={"note": body.note})
    return {"alert_id": alert_id, "state": body.state}


# ── watchlist ────────────────────────────────────────────────────────

class WatchlistCreate(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    plate_query: str | None = None
    seed_sighting_id: str | None = None
    vehicle_type: str | None = None
    vehicle_color: str | None = None
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"
    case_ref: str | None = None
    # DPDP Act purpose limitation. A watchlist entry authorises continuous
    # tracking of an identifiable vehicle, so it must carry its reason.
    reason: str = Field(min_length=8, max_length=500)
    expires_at: str | None = None


@router.get("/watchlist")
async def list_watchlist(user: Annotated[object, Depends(require("watchlist:read"))],
                         active_only: bool = True):
    # A watchlist entry carries the case reference and the stated reason
    # for tracking a vehicle. Listing every department's entries would tell
    # one force which vehicles another is working and under which case,
    # which is investigation metadata, not shared estate data. Scoped by
    # the department of the officer who created it.
    clauses = ["w.is_active"] if active_only else []
    params: list = []
    if not sees_all_departments(user):
        if not user.department:
            clauses.append("FALSE")
        else:
            clauses.append("_wd.code = %s")
            params.append(user.department)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await db.fetch_all(f"""
        SELECT w.id::text AS id, w.label, w.plate_query, w.vehicle_type::text AS vehicle_type,
               w.vehicle_color, w.severity::text AS severity, w.reason, w.case_ref,
               w.is_active, w.created_at, w.expires_at, w.hit_count, w.last_hit_at,
               u.username AS created_by
        FROM watchlist w
        LEFT JOIN app_user u ON u.id = w.created_by
        LEFT JOIN department _wd ON _wd.id = u.department_id
        {where} ORDER BY w.created_at DESC""", tuple(params))
    return {"items": rows, "count": len(rows)}


@router.post("/watchlist", status_code=status.HTTP_201_CREATED)
async def create_watchlist_entry(body: WatchlistCreate, request: Request,
                                 user: CurrentUserDep,
                                 _perm: Annotated[object, Depends(require("watchlist:write"))]):
    if not body.plate_query and not body.seed_sighting_id:
        raise HTTPException(422, "supply plate_query, seed_sighting_id, or both. "
                                 "An entry with neither would match every vehicle.")

    seed_embedding = None
    if body.seed_sighting_id:
        row = await db.fetch_one(
            "SELECT embedding::text AS emb FROM vehicle_sighting WHERE sighting_id=%s",
            (body.seed_sighting_id,))
        if row is None:
            raise HTTPException(404, "seed sighting not found")
        seed_embedding = row["emb"]

    warnings = []
    if body.plate_query:
        parsed = plate_rules.correct(body.plate_query)
        if not parsed.valid:
            # Do not reject: a partially known plate is still useful, and
            # fuzzy matching will do the work. But say so plainly.
            warnings.append(
                f"'{body.plate_query}' is not a valid Indian plate format. "
                "Fuzzy matching will still run, but expect more false positives.")

    row = await db.fetch_one("""
        INSERT INTO watchlist (label, plate_query, plate_canonical, seed_sighting_id,
            seed_embedding, vehicle_type, vehicle_color, severity, reason,
            case_ref, created_by, expires_at)
        VALUES (%s,%s,plate_canon(%s),%s,%s::vector,%s::vehicle_type,%s,
                %s::alert_severity,%s,%s,%s::uuid,%s::timestamptz)
        RETURNING id::text AS id""",
        (body.label, body.plate_query, body.plate_query, body.seed_sighting_id,
         seed_embedding, body.vehicle_type, body.vehicle_color, body.severity,
         body.reason, body.case_ref, user.id, body.expires_at))

    await write_audit(request, user=user, action="WATCHLIST_CREATE",
                      resource="/watchlist", resource_id=row["id"],
                      reason=body.reason,
                      detail={"plate": body.plate_query, "case_ref": body.case_ref})
    return {"id": row["id"], "label": body.label, "warnings": warnings}


@router.delete("/watchlist/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_watchlist(watchlist_id: str, request: Request,
                               user: CurrentUserDep,
                               _perm: Annotated[object, Depends(require("watchlist:write"))]):
    n = await db.execute("UPDATE watchlist SET is_active=false WHERE id=%s::uuid",
                         (watchlist_id,))
    if n == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watchlist entry not found")
    await write_audit(request, user=user, action="WATCHLIST_DEACTIVATE",
                      resource="/watchlist", resource_id=watchlist_id)


# ── alert rules ──────────────────────────────────────────────────────

@router.get("/alert-rules")
async def list_rules(user: Annotated[object, Depends(require("alert:read"))]):
    rows = await db.fetch_all("""
        SELECT code, name, alert_type::text AS alert_type, severity::text AS severity,
               is_enabled, params, dedup_seconds, description
        FROM alert_rule ORDER BY code""")
    return {"items": rows}


class RuleUpdate(BaseModel):
    is_enabled: bool | None = None
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"] | None = None
    params: dict | None = None
    dedup_seconds: int | None = Field(default=None, ge=0, le=86400)


@router.patch("/alert-rules/{code}")
async def update_rule(code: str, body: RuleUpdate, request: Request,
                      user: CurrentUserDep,
                      _perm: Annotated[object, Depends(require("alert:rule:write"))]):
    """Rules are runtime configuration.

    The event processor re-reads them every 30 seconds, so tuning a
    threshold during an incident takes effect without a deployment.
    """
    import json
    sets, params = [], []
    if body.is_enabled is not None:
        sets.append("is_enabled=%s"); params.append(body.is_enabled)
    if body.severity is not None:
        sets.append("severity=%s::alert_severity"); params.append(body.severity)
    if body.params is not None:
        sets.append("params=%s::jsonb"); params.append(json.dumps(body.params))
    if body.dedup_seconds is not None:
        sets.append("dedup_seconds=%s"); params.append(body.dedup_seconds)
    if not sets:
        raise HTTPException(422, "no fields to update")
    sets.append("updated_at=now()")

    params.append(code)
    n = await db.execute(f"UPDATE alert_rule SET {', '.join(sets)} WHERE code=%s", params)
    if n == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    await write_audit(request, user=user, action="ALERT_RULE_UPDATE",
                      resource="/alert-rules", resource_id=code,
                      detail=body.model_dump(exclude_none=True))
    return {"code": code, "updated": True,
            "note": "takes effect within 30 seconds; no restart needed"}
