"""Request dependencies: authentication, authorisation, audit, rate limiting."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated, Callable

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sentinel_core.config import Settings, get_settings
from sentinel_core.log import get_logger

from . import db
from .security import Role, decode_token, has_permission

log = get_logger("sentinel.api.deps")
bearer = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    id: str
    username: str
    role: Role
    department: str | None
    token_id: str

    def can(self, permission: str) -> bool:
        return has_permission(self.role, permission)


async def get_current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CurrentUser:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    payload = decode_token(creds.credentials, settings.secret_key)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})

    user = CurrentUser(
        id=payload["sub"], username=payload.get("username", "?"),
        role=Role.parse(payload.get("role")),
        department=payload.get("dept"), token_id=payload.get("jti", ""))
    # Bound to the request so the audit middleware can attribute the call
    # without every endpoint having to pass the user along.
    request.state.user = user
    return request.state.user


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require(permission: str) -> Callable:
    """Dependency factory enforcing one permission.

    Denials are written to the audit log, not just refused. An attempt to
    reach data outside one's authority is exactly the event an audit trail
    exists to capture.
    """
    async def _check(request: Request, user: CurrentUserDep) -> CurrentUser:
        if not user.can(permission):
            await write_audit(
                request, user=user, action=f"DENIED:{permission}",
                resource=request.url.path, result="DENIED")
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role {user.role.name} lacks permission '{permission}'")
        return user
    return _check


def sees_all_departments(user: CurrentUser) -> bool:
    """Whether this caller's authority spans the whole state.

    Only the State Admin. A Department Admin has full control *within* one
    department, which is a different thing and is deliberately not enough:
    26 departments sharing an estate is the problem this system exists to
    solve, and an admin of one of them is not entitled to the other 25.
    """
    return user.role is Role.SYSTEM


def dept_filter(user: CurrentUser, column: str = "d.code") -> tuple[str, list]:
    """SQL fragment restricting a query to what this caller may see.

    Returns (clause, params). The clause is never empty for a scoped user,
    so a caller that forgets to apply it produces a visibly unfiltered query
    in review rather than a silently over-broad one at runtime.

    A user with no department is denied everything rather than granted
    everything. That asymmetry is the entire point: the failure mode of
    "unassigned means unrestricted" is a full estate breach, and the
    failure mode of this is a support ticket.
    """
    if sees_all_departments(user):
        return ("TRUE", [])
    if user.department:
        return (f"{column} = %s", [user.department])
    return ("FALSE", [])


def _scope_through_camera(user: CurrentUser, camera_id_expr: str) -> tuple[str, list]:
    """EXISTS clause tying a row to a camera this caller may see.

    Every derived record in this system -- a sighting, a vehicle, an alert --
    inherits its department from the camera that produced it. Scoping the
    camera therefore scopes everything downstream, and doing it as an
    EXISTS keeps the clause composable into any WHERE without disturbing
    the outer query's joins or its row count.
    """
    if sees_all_departments(user):
        return ("TRUE", [])
    if not user.department:
        return ("FALSE", [])
    return (
        f"EXISTS (SELECT 1 FROM camera _sc JOIN department _sd "
        f"ON _sd.id = _sc.department_id "
        f"WHERE _sc.id = {camera_id_expr} AND _sd.code = %s)",
        [user.department],
    )


def dept_scope_sighting(user: CurrentUser, alias: str = "s") -> tuple[str, list]:
    """Restrict a sighting row to the caller's department.

    Uses `vehicle_sighting.department_code`, which migration 0011 maintains
    by trigger from the owning camera. It is a predicate rather than a
    subquery so the planner can still use the (department_code, timestamp
    DESC) index to satisfy ORDER BY ... LIMIT and stop early: measured at
    200k sightings, the EXISTS form cost 38 ms against 0.4 ms unscoped,
    because it had to gather every row for every camera in the department
    before sorting. The trigger, not the application, writes the column --
    an authorisation key that app code is trusted to fill is one missed
    INSERT path away from being a leak.
    """
    if sees_all_departments(user):
        return ("TRUE", [])
    if not user.department:
        return ("FALSE", [])
    return (f"{alias}.department_code = %s", [user.department])


def dept_scope_alert(user: CurrentUser, alias: str = "a") -> tuple[str, list]:
    """Restrict an alert row to the caller's department.

    An alert with no camera is not attributable to a department, so only
    the state admin sees it. Failing closed here matters: the alternative
    is that a NULL camera_id becomes a channel through which every
    department reads every other one's alerts.
    """
    if sees_all_departments(user):
        return ("TRUE", [])
    if not user.department:
        return ("FALSE", [])
    clause, params = _scope_through_camera(user, f"{alias}.camera_id")
    return (f"({alias}.camera_id IS NOT NULL AND {clause})", params)


def dept_scope_vehicle(user: CurrentUser, alias: str = "v") -> tuple[str, list]:
    """Restrict a vehicle to one this caller's own cameras have seen.

    A vehicle crossing a department boundary is visible to both, because
    both genuinely observed it. What each may then read is still only its
    own hops -- see `dept_scope_sighting`, which the timeline applies -- so
    this grants the existence of the vehicle, never another department's
    view of where it went.
    """
    if sees_all_departments(user):
        return ("TRUE", [])
    if not user.department:
        return ("FALSE", [])
    return (
        f"EXISTS (SELECT 1 FROM vehicle_sighting _vs "
        f"JOIN camera _vc ON _vc.id = _vs.camera_id "
        f"JOIN department _vd ON _vd.id = _vc.department_id "
        f"WHERE _vs.vehicle_track_id = {alias}.vehicle_track_id "
        f"AND _vd.code = %s)",
        [user.department],
    )


def require_department(user: CurrentUser, department: str | None) -> None:
    """Authorise access to one resource owned by `department`.

    Raises 404, not 403, when the caller is outside it. 403 would confirm
    that a camera with this id exists in another department, which is
    itself information an operator of one department should not be able to
    harvest by enumeration.
    """
    if sees_all_departments(user):
        return
    if department is not None and user.department == department:
        return
    raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


async def write_audit(request: Request, *, user: CurrentUser | None,
                      action: str, resource: str, resource_id: str | None = None,
                      reason: str | None = None, result: str = "SUCCESS",
                      detail: dict | None = None) -> None:
    """Append to the audit log.

    DPDP Act 2023 requires purpose limitation and accountability for
    personal data, and surveillance video of identifiable people is
    personal data. Auditing must never be able to fail the request it is
    recording -- a broken audit table would otherwise take the whole system
    down -- so failures here are logged loudly and swallowed.
    """
    import json
    try:
        await db.execute(
            """INSERT INTO audit_log (user_id, username, department, action,
                   resource, resource_id, reason, ip_address, user_agent,
                   result, detail)
               VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s::inet,%s,%s,%s::jsonb)""",
            (user.id if user else None,
             user.username if user else None,
             user.department if user else None,
             action, resource, resource_id, reason,
             request.client.host if request.client else None,
             request.headers.get("user-agent", "")[:400],
             result, json.dumps(detail or {})))
    except Exception as e:                                # pragma: no cover
        log.error("audit write failed", extra={"error": str(e), "action": action})


class RateLimiter:
    """Fixed-window per-identity rate limiting.

    In-process, which is correct for the MVP and explicitly not correct for
    a multi-replica deployment -- N replicas would allow N times the limit.
    At that point this moves to Redis; the interface does not change. Said
    plainly here rather than discovered later.
    """

    def __init__(self, per_minute: int = 300):
        self.per_minute = per_minute
        self._counts: dict[str, tuple[int, float]] = {}

    def check(self, identity: str) -> tuple[bool, int]:
        now = time.time()
        window = now - (now % 60)
        count, w = self._counts.get(identity, (0, window))
        if w != window:
            count, w = 0, window
        count += 1
        self._counts[identity] = (count, w)
        if len(self._counts) > 20_000:
            self._counts = {k: v for k, v in self._counts.items() if v[1] == window}
        return count <= self.per_minute, max(0, self.per_minute - count)


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(get_settings().rate_limit_per_minute)
    return _limiter


async def rate_limit(request: Request) -> None:
    ident = request.headers.get("authorization") or (
        request.client.host if request.client else "anonymous")
    ok, remaining = get_limiter().check(ident[:120])
    request.state.rate_remaining = remaining
    if not ok:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "rate limit exceeded",
                            headers={"Retry-After": "60"})


def require_reason(x_reason: Annotated[str | None, Header()] = None) -> str:
    """Require a stated purpose for reading personal data.

    DPDP Act purpose limitation, enforced at the edge rather than trusted
    to callers. Endpoints that expose identifiable movement history depend
    on this, so the reason lands in the audit log alongside the query.
    """
    if not x_reason or len(x_reason.strip()) < 4:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "an X-Reason header stating the purpose of this access is required "
            "(DPDP Act 2023 purpose limitation)")
    return x_reason.strip()[:500]
