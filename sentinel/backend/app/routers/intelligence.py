"""Government record-system lookups.

Every response from this router is labelled with the provenance of the
data in it. There is no code path here that can return a record without
saying whether it describes a real citizen or a demonstration.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from sentinel_core.govt import (
    AuthorizationRequired, RateLimited, UpstreamTimeout, build_registry,
)
from sentinel_core.govt.base import AdapterError, Purpose
from sentinel_core.govt.intelligence import screen_plate

from ..deps import CurrentUserDep, require, require_reason, write_audit

router = APIRouter(prefix="/intelligence", tags=["intelligence"])


def _audit_sink(request: Request, user):
    """Adapter-level audit, written to the same ledger as everything else.

    A lookup against a citizen's registration record is at least as
    sensitive as viewing a camera, so it lands in `audit_log` rather than
    in an integration-specific log nobody reviews.
    """
    def sink(*, system, query, purpose, provenance, result, actor,
             case_ref, latency_ms):
        import asyncio
        coro = write_audit(
            request, user=user, action=f"GOVT_LOOKUP:{system}",
            resource=f"/intelligence/{system.lower()}", resource_id=query,
            reason=case_ref, result="SUCCESS" if result == "HIT" else result,
            detail={"purpose": purpose, "provenance": provenance,
                    "result": result, "latency_ms": round(latency_ms, 2)})
        # The adapter's sink is synchronous by design -- it is called from
        # inside the lookup, which must not depend on an event loop. Inside
        # the API there is always a running loop, so the write is scheduled
        # rather than awaited; failures are swallowed by write_audit itself.
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:                                # pragma: no cover
            coro.close()
    return sink


_REGISTRY_CACHE = None


def _registry(request: Request, user):
    """One registry per process, rebound to this request's audit sink.

    The adapters carry rate-limit state, so they must outlive a request;
    the audit sink must not, because it names the caller.
    """
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = build_registry()
    for adapter in _REGISTRY_CACHE.adapters.values():
        adapter._audit = _audit_sink(request, user)
    return _REGISTRY_CACHE


class ScreenRequest(BaseModel):
    plate: str = Field(min_length=1, max_length=32)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


@router.get("/status")
async def integration_status(
    request: Request, user: CurrentUserDep,
    _perm: Annotated[object, Depends(require("vehicle:read"))],
):
    """Which record systems are connected, and which are not.

    Exposed as a first-class endpoint so the UI can show the answer
    permanently rather than relying on an operator having read a document.
    """
    reg = _registry(request, user)
    return {
        "any_real_integration": reg.any_real,
        "notice": ("No government record system is connected in this "
                   "deployment. Every record below is generated locally for "
                   "demonstration and describes no real person or vehicle."
                   if not reg.any_real else
                   "Some systems are connected to authorised endpoints. "
                   "Check `provenance` on every individual record."),
        "systems": reg.status(),
    }


@router.post("/screen")
async def screen(
    body: ScreenRequest, request: Request, user: CurrentUserDep,
    _perm: Annotated[object, Depends(require("watchlist:read"))],
):
    """Screen one plate for a reason to raise an alert.

    Releases a status flag and nothing else: this is the path a passing
    vehicle takes, and a vehicle of no interest must leave no trace of its
    owner in this system.
    """
    reg = _registry(request, user)
    result = screen_plate(body.plate, body.confidence, reg,
                          actor=user.username)
    return result.as_dict()


@router.get("/{system}/{query}")
async def lookup(
    system: str, query: str, request: Request, user: CurrentUserDep,
    _perm: Annotated[object, Depends(require("evidence:export"))],
    reason: Annotated[str, Depends(require_reason)],
    purpose: Literal["VEHICLE_VERIFICATION", "REGISTERED_INVESTIGATION"]
        = "VEHICLE_VERIFICATION",
    case_ref: str | None = Query(default=None,
                                 description="Required for REGISTERED_INVESTIGATION"),
):
    """A named lookup against one system.

    Gated on `evidence:export` rather than on a read permission: this is
    the path that can release a citizen's personal data, and it belongs to
    investigators, not to everyone with a login. A stated reason is
    mandatory, and the case reference is recorded with the query.
    """
    reg = _registry(request, user)
    adapter = reg.get(system)
    if adapter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"unknown system '{system}'")
    try:
        record = adapter.lookup(query, purpose=Purpose(purpose),
                                actor=user.username, case_ref=case_ref)
    except AuthorizationRequired as e:
        # 501, not 503. This deployment has never had access; retrying
        # cannot help, and a 5xx that implies an outage would have an
        # operator retrying all shift.
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(e))
    except UpstreamTimeout as e:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(e))
    except RateLimited as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e))
    except AdapterError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))

    if record is None:
        return {"system": adapter.system, "query": query, "found": False,
                "provenance": adapter.provenance.value,
                "is_real_data": adapter.provenance.is_real,
                "banner": adapter.provenance.banner}
    return {"found": True, **record.as_dict()}
