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


def dept_filter(user: CurrentUser) -> tuple[str, str | None]:
    """Department scope filter for queries.

    SYSTEM admins see all departments. All others see only their own.
    Returns (WHERE clause fragment, parameter value or None).
    """
    if user.role.name == "SYSTEM":
        return ("", None)  # no filter
    if user.department:
        return ("d.code = %s", user.department)
    # No department assignment; shouldn't happen but safe to null-filter
    return ("FALSE", None)


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
