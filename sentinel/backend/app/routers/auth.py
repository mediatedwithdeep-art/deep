"""Authentication: login, refresh, logout, current user."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from sentinel_core.config import Settings, get_settings

from .. import db, metrics
from ..deps import CurrentUserDep, write_audit
from ..security import (
    Role, create_access_token, create_refresh_token, hash_password,
    hash_refresh_token, needs_rehash, password_problems, verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Lockout after repeated failures. Five attempts then a 15-minute freeze
# stops credential stuffing without letting an attacker lock a real officer
# out of a live incident indefinitely.
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: dict


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request,
                settings: Annotated[Settings, Depends(get_settings)]):
    row = await db.fetch_one(
        "SELECT u.id::text AS id, u.username, u.full_name, u.password_hash, "
        "       u.role::text AS role, u.is_active, u.failed_logins, u.locked_until, "
        "       d.code AS department "
        "FROM app_user u LEFT JOIN department d ON d.id = u.department_id "
        "WHERE u.username = %s", (body.username,))

    # One message and one code path for every failure mode. Distinguishing
    # "no such user" from "wrong password" hands an attacker a free account
    # enumeration oracle.
    invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    if row is None:
        # Still spend the hashing time, so response latency does not reveal
        # whether the username exists.
        verify_password(body.password, hash_password("timing-equalisation"))
        metrics.AUTH_FAILURES.labels(reason="no_such_user").inc()
        await write_audit(request, user=None, action="LOGIN",
                          resource="/auth/login", resource_id=body.username,
                          result="DENIED", detail={"reason": "no_such_user"})
        raise invalid

    if row["locked_until"] and row["locked_until"] > datetime.now(timezone.utc):
        metrics.AUTH_FAILURES.labels(reason="locked").inc()
        await write_audit(request, user=None, action="LOGIN",
                          resource="/auth/login", resource_id=body.username,
                          result="DENIED", detail={"reason": "locked"})
        raise HTTPException(status.HTTP_423_LOCKED,
                            "account temporarily locked after repeated failures")

    if not row["is_active"]:
        metrics.AUTH_FAILURES.labels(reason="inactive").inc()
        raise invalid

    if not verify_password(body.password, row["password_hash"]):
        failed = (row["failed_logins"] or 0) + 1
        lock = (datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
                if failed >= MAX_FAILED_LOGINS else None)
        await db.execute(
            "UPDATE app_user SET failed_logins=%s, locked_until=%s WHERE id=%s::uuid",
            (failed, lock, row["id"]))
        metrics.AUTH_FAILURES.labels(reason="bad_password").inc()
        await write_audit(request, user=None, action="LOGIN",
                          resource="/auth/login", resource_id=body.username,
                          result="DENIED",
                          detail={"reason": "bad_password", "attempt": failed})
        raise invalid

    # Transparently upgrade the stored hash if the iteration count has been
    # raised since this user last logged in.
    if needs_rehash(row["password_hash"]):
        await db.execute("UPDATE app_user SET password_hash=%s WHERE id=%s::uuid",
                         (hash_password(body.password), row["id"]))

    role = Role.parse(row["role"])
    token, expires = create_access_token(
        user_id=row["id"], username=row["username"], role=role,
        department=row["department"], secret=settings.secret_key,
        ttl_minutes=settings.access_token_ttl_minutes)
    refresh, digest = create_refresh_token()

    await db.execute(
        "UPDATE app_user SET failed_logins=0, locked_until=NULL, last_login_at=now() "
        "WHERE id=%s::uuid", (row["id"],))
    await db.execute(
        "INSERT INTO refresh_token (user_id, token_hash, expires_at, user_agent, ip_address) "
        "VALUES (%s::uuid,%s,%s,%s,%s::inet)",
        (row["id"], digest,
         datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days),
         request.headers.get("user-agent", "")[:400],
         request.client.host if request.client else None))

    await write_audit(request, user=None, action="LOGIN", resource="/auth/login",
                      resource_id=row["username"], result="SUCCESS")

    return TokenResponse(
        access_token=token, refresh_token=refresh, expires_at=expires,
        user={"id": row["id"], "username": row["username"],
              "full_name": row["full_name"], "role": role.name,
              "department": row["department"]})


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest,
                        settings: Annotated[Settings, Depends(get_settings)],
                        request: Request):
    digest = hash_refresh_token(body.refresh_token)
    row = await db.fetch_one(
        "SELECT r.id::text AS rid, r.user_id::text AS uid, u.username, "
        "       u.role::text AS role, u.is_active, d.code AS department "
        "FROM refresh_token r JOIN app_user u ON u.id = r.user_id "
        "LEFT JOIN department d ON d.id = u.department_id "
        "WHERE r.token_hash=%s AND r.revoked_at IS NULL AND r.expires_at > now()",
        (digest,))
    if row is None or not row["is_active"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "refresh token invalid, expired or revoked")

    # Rotate on use: a refresh token is single-use, so a stolen one is
    # detectable (the legitimate holder's next refresh fails) and useless
    # once the real user has refreshed.
    await db.execute("UPDATE refresh_token SET revoked_at=now() WHERE id=%s::uuid",
                     (row["rid"],))
    new_refresh, new_digest = create_refresh_token()
    await db.execute(
        "INSERT INTO refresh_token (user_id, token_hash, expires_at) "
        "VALUES (%s::uuid,%s,%s)",
        (row["uid"], new_digest,
         datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days)))

    role = Role.parse(row["role"])
    token, expires = create_access_token(
        user_id=row["uid"], username=row["username"], role=role,
        department=row["department"], secret=settings.secret_key,
        ttl_minutes=settings.access_token_ttl_minutes)
    return TokenResponse(
        access_token=token, refresh_token=new_refresh, expires_at=expires,
        user={"id": row["uid"], "username": row["username"],
              "role": role.name, "department": row["department"]})


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(user: CurrentUserDep, request: Request):
    await db.execute(
        "UPDATE refresh_token SET revoked_at=now() "
        "WHERE user_id=%s::uuid AND revoked_at IS NULL", (user.id,))
    await write_audit(request, user=user, action="LOGOUT", resource="/auth/logout")


@router.get("/me")
async def me(user: CurrentUserDep):
    from ..security import PERMISSIONS
    return {
        "id": user.id, "username": user.username, "role": user.role.name,
        "department": user.department,
        "permissions": sorted(PERMISSIONS.get(user.role, set())),
    }


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: PasswordChange, user: CurrentUserDep,
                          request: Request,
                          settings: Annotated[Settings, Depends(get_settings)]):
    row = await db.fetch_one("SELECT password_hash FROM app_user WHERE id=%s::uuid",
                             (user.id,))
    if row is None or not verify_password(body.current_password, row["password_hash"]):
        await write_audit(request, user=user, action="PASSWORD_CHANGE",
                          resource="/auth/password", result="DENIED")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")

    problems = password_problems(body.new_password, settings.password_min_length)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "new password " + "; ".join(problems))

    await db.execute(
        "UPDATE app_user SET password_hash=%s, password_changed_at=now() "
        "WHERE id=%s::uuid", (hash_password(body.new_password), user.id))
    # Every other session for this user dies with the password change.
    await db.execute(
        "UPDATE refresh_token SET revoked_at=now() "
        "WHERE user_id=%s::uuid AND revoked_at IS NULL", (user.id,))
    await write_audit(request, user=user, action="PASSWORD_CHANGE",
                      resource="/auth/password")
