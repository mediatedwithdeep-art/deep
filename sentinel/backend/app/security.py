"""Authentication, password hashing and role-based access control.

Password hashing is PBKDF2-HMAC-SHA256 from the standard library rather
than bcrypt or argon2. That is a deliberate trade: those are stronger per
unit of work, but they are C extensions that complicate the container
build, and PBKDF2 at 260,000 iterations is FIPS-approved, is what Django
ships as its default, and is well within what this threat model needs. The
format string records the iteration count, so raising it later re-hashes
users transparently on their next login.

Roles are hierarchical. A rank check beats a set membership test because
it makes "at least OPERATOR" expressible without enumerating every role
above it, which is where permission bugs come from.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from enum import IntEnum

from jose import JWTError, jwt

PBKDF2_ITERATIONS = 260_000
ALGORITHM = "HS256"


class Role(IntEnum):
    """Ordered by authority. Comparisons are meaningful: OPERATOR >= VIEWER."""
    VIEWER = 10
    OPERATOR = 20
    INVESTIGATOR = 30
    ADMIN = 40
    SYSTEM = 50

    @classmethod
    def parse(cls, name: str | None) -> "Role":
        try:
            return cls[str(name).upper()]
        except KeyError:
            # An unknown role must never be treated as privileged. Failing
            # closed to VIEWER is the only safe default.
            return cls.VIEWER


# What each role may do. Kept explicit rather than derived, because a
# derived permission matrix is impossible to audit and this one will be
# read by people deciding whether to deploy the system.
PERMISSIONS: dict[Role, set[str]] = {
    Role.VIEWER: {
        "camera:read", "sighting:read", "vehicle:read", "alert:read",
        "analytics:read",
    },
    Role.OPERATOR: {
        "camera:read", "sighting:read", "vehicle:read", "alert:read",
        "alert:ack", "analytics:read", "watchlist:read", "link:verdict",
    },
    Role.INVESTIGATOR: {
        "camera:read", "sighting:read", "vehicle:read", "alert:read",
        "alert:ack", "analytics:read", "watchlist:read", "watchlist:write",
        "link:verdict", "evidence:export", "audit:read",
    },
    Role.ADMIN: {
        "camera:read", "camera:write", "sighting:read", "vehicle:read",
        "alert:read", "alert:ack", "alert:rule:write", "analytics:read",
        "watchlist:read", "watchlist:write", "link:verdict",
        "evidence:export", "audit:read", "user:write", "system:admin",
    },
    Role.SYSTEM: {"*"},
}


def has_permission(role: Role, permission: str) -> bool:
    perms = PERMISSIONS.get(role, set())
    return "*" in perms or permission in perms


# ─────────────────────────────────────────────────────────────────────
# Passwords
# ─────────────────────────────────────────────────────────────────────

def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, AttributeError):
        return False
    # Constant-time comparison. A timing side channel here leaks the hash
    # one byte at a time.
    return hmac.compare_digest(dk.hex(), hash_hex)


def needs_rehash(stored: str) -> bool:
    try:
        _, iterations, _, _ = stored.split("$")
        return int(iterations) < PBKDF2_ITERATIONS
    except (ValueError, AttributeError):
        return True


def password_problems(password: str, min_length: int = 12) -> list[str]:
    """Complaints about a password, or an empty list.

    Length is the requirement that actually matters; the character-class
    rules are here because government password policy invariably demands
    them, not because they add much.
    """
    problems = []
    if len(password) < min_length:
        problems.append(f"must be at least {min_length} characters")
    if not any(c.islower() for c in password):
        problems.append("must contain a lowercase letter")
    if not any(c.isupper() for c in password):
        problems.append("must contain an uppercase letter")
    if not any(c.isdigit() for c in password):
        problems.append("must contain a digit")
    if password.lower() in {"password123", "sentinel123", "admin1234567",
                            "gujaratpolice"}:
        problems.append("is a commonly used password")
    return problems


# ─────────────────────────────────────────────────────────────────────
# Tokens
# ─────────────────────────────────────────────────────────────────────

def create_access_token(*, user_id: str, username: str, role: Role,
                        department: str | None, secret: str,
                        ttl_minutes: int = 60) -> tuple[str, datetime]:
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    payload = {
        "sub": user_id, "username": username, "role": role.name,
        "dept": department, "type": "access",
        "exp": expires, "iat": datetime.now(timezone.utc),
        # A unique id per token, so a specific token can be revoked and so
        # audit entries can be tied to one session.
        "jti": secrets.token_urlsafe(12),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM), expires


def create_refresh_token() -> tuple[str, str]:
    """Return (token, sha256 digest).

    Only the digest is stored. A database leak must not hand the attacker
    working sessions for every logged-in officer.
    """
    token = secrets.token_urlsafe(48)
    return token, hashlib.sha256(token.encode()).hexdigest()


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def decode_token(token: str, secret: str) -> dict | None:
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM])
    except JWTError:
        return None
