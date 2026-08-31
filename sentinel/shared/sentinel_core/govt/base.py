"""The adapter contract every government record system is reached through.

One shape for five very different systems, because the things that must be
true of a lookup are the same in all five cases and are easy to get wrong
individually: it must be authorised, bounded in time, rate limited,
audited, minimised, and unambiguously labelled as real or mock.

DATA MINIMISATION IS ENFORCED HERE, NOT REQUESTED
─────────────────────────────────────────────────
A VAHAN response carries far more about a citizen than a traffic
investigation needs. `GovtAdapter.lookup` takes the *purpose* of the query
and returns only the fields that purpose justifies; the full upstream
payload is never stored and never leaves this module. DPDP Act 2023 makes
purpose limitation a legal obligation rather than a preference, and a
system that fetches everything and filters at the UI has already
processed everything.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


class Provenance(enum.Enum):
    """Where a record came from. Travels with the record everywhere.

    There is deliberately no `UNKNOWN`. A record whose origin cannot be
    stated is not a record this system will carry: the whole point is that
    an operator can always tell whether what they are reading describes a
    real citizen.
    """

    #: Returned by a real, authorised government endpoint.
    AUTHORITATIVE = "AUTHORITATIVE"
    #: Fabricated locally for demonstration. Not a real person or vehicle.
    MOCK = "MOCK"
    #: A real endpoint answered, but from its own sandbox/test dataset.
    SANDBOX = "SANDBOX"

    @property
    def is_real(self) -> bool:
        return self is Provenance.AUTHORITATIVE

    @property
    def banner(self) -> str:
        """Text an interface must show beside anything from this source."""
        return {
            Provenance.AUTHORITATIVE: "",
            Provenance.MOCK: "DEMO DATA — not a real record",
            Provenance.SANDBOX: "SANDBOX DATA — upstream test dataset",
        }[self]


class Purpose(enum.Enum):
    """Why a lookup is being made. Selects which fields come back.

    Named after the operational act, not after the field set, so that
    widening a purpose is a visible decision in this file rather than an
    extra key silently appearing in a response.
    """

    #: Is this vehicle stolen / wanted / on a watchlist? Needs status only.
    WATCHLIST_SCREENING = "WATCHLIST_SCREENING"
    #: Confirm the vehicle matches what the camera saw (make/model/colour).
    VEHICLE_VERIFICATION = "VEHICLE_VERIFICATION"
    #: A named investigation with a case reference. The widest set.
    REGISTERED_INVESTIGATION = "REGISTERED_INVESTIGATION"


@dataclass(frozen=True)
class GovtRecord:
    """One record from one system, with its origin attached.

    Frozen: a record's provenance must not be mutable by anything
    downstream. Re-labelling mock data as authoritative should require
    constructing a new object, which is greppable.
    """

    system: str
    provenance: Provenance
    query: str
    fields: dict[str, Any]
    retrieved_at: datetime
    #: Which purpose this was fetched under, so the audit log can show that
    #: the field set matched the justification.
    purpose: Purpose
    #: Upstream's own reference, where it gives one.
    upstream_ref: str | None = None
    latency_ms: float = 0.0

    @property
    def is_real(self) -> bool:
        return self.provenance.is_real

    def as_dict(self) -> dict:
        """Serialised for the API. Provenance is never optional."""
        return {
            "system": self.system,
            "provenance": self.provenance.value,
            "is_real_data": self.is_real,
            "banner": self.provenance.banner,
            "query": self.query,
            "purpose": self.purpose.value,
            "fields": dict(self.fields),
            "retrieved_at": self.retrieved_at.isoformat(),
            "upstream_ref": self.upstream_ref,
            "latency_ms": round(self.latency_ms, 2),
        }


# ── failures, each distinguishable ───────────────────────────────────
# Distinguishable because they need different responses: a timeout is
# retryable, a rate limit is retryable later, and a missing authorisation
# is not retryable at all and must not be dressed up as an outage.

class AdapterError(Exception):
    """Base for every adapter failure."""


class AuthorizationRequired(AdapterError):
    """No authorised access to this system exists in this deployment.

    Deliberately not an outage. An operator seeing "VAHAN unavailable"
    would reasonably retry; the truthful message is that this deployment
    has never been granted access and retrying cannot help.
    """


class UpstreamTimeout(AdapterError):
    """The upstream system did not answer within its budget."""


class RateLimited(AdapterError):
    """This deployment's own quota for the system is exhausted."""


# ── rate limiting ────────────────────────────────────────────────────

class _TokenBucket:
    """Per-system quota, enforced on OUR side of the call.

    Government systems publish quotas and withdraw access from integrators
    that exceed them. Enforcing locally means an ANPR storm -- a plate
    misread that generates the same lookup fifty times a second -- exhausts
    a counter here rather than the integration agreement.
    """

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self.per_minute = per_minute
        self._clock = clock
        self._tokens = float(per_minute)
        self._last = clock()

    def take(self) -> bool:
        now = self._clock()
        self._tokens = min(
            float(self.per_minute),
            self._tokens + (now - self._last) * (self.per_minute / 60.0))
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


@dataclass
class AdapterStats:
    calls: int = 0
    hits: int = 0
    misses: int = 0
    timeouts: int = 0
    rate_limited: int = 0
    unauthorized: int = 0
    total_ms: float = 0.0

    @property
    def mean_ms(self) -> float:
        return self.total_ms / max(self.calls, 1)


class AuditSink(Protocol):
    """Where lookups are recorded. The API supplies one that writes to
    `audit_log`; tests supply one that collects in memory."""

    def __call__(self, *, system: str, query: str, purpose: str,
                 provenance: str, result: str, actor: str | None,
                 case_ref: str | None, latency_ms: float) -> None: ...


# ── the adapter ──────────────────────────────────────────────────────

class GovtAdapter:
    """One government system, reached through one backend.

    Subclasses supply `system`, the field projection per purpose, and a
    mock generator. Everything the brief requires around the call --
    authorisation, timeout, rate limiting, audit, minimisation, provenance
    -- happens here so that no individual adapter can forget one of them.
    """

    system: str = "UNSET"
    #: Fields released for each purpose. A purpose absent from this map is
    #: refused rather than defaulted, so adding a purpose is a deliberate
    #: act per system.
    RELEASE: dict[Purpose, tuple[str, ...]] = {}

    def __init__(self, backend: "Backend", *, timeout_s: float = 3.0,
                 per_minute: int = 60, audit: AuditSink | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.backend = backend
        self.timeout_s = timeout_s
        self._bucket = _TokenBucket(per_minute, clock)
        self._audit = audit
        self._clock = clock
        self.stats = AdapterStats()

    @property
    def provenance(self) -> Provenance:
        return self.backend.provenance

    def lookup(self, query: str, *, purpose: Purpose,
               actor: str | None = None,
               case_ref: str | None = None) -> GovtRecord | None:
        """Look one identifier up, and record that it happened.

        `case_ref` is required for REGISTERED_INVESTIGATION, which is the
        only purpose that releases personal data. An investigation without
        a reference is not an investigation, and the audit trail must be
        able to answer "under which case was this citizen's address
        retrieved?" years later.
        """
        if purpose not in self.RELEASE:
            raise AdapterError(
                f"{self.system} does not serve purpose {purpose.value}")
        if purpose is Purpose.REGISTERED_INVESTIGATION and not case_ref:
            raise AdapterError(
                f"{self.system}: REGISTERED_INVESTIGATION requires a case_ref")

        if not self._bucket.take():
            self.stats.rate_limited += 1
            self._record(query, purpose, "RATE_LIMITED", actor, case_ref, 0.0)
            raise RateLimited(f"{self.system}: local quota exhausted")

        t0 = self._clock()
        self.stats.calls += 1
        try:
            raw = self.backend.fetch(query, timeout_s=self.timeout_s)
        except AuthorizationRequired:
            self.stats.unauthorized += 1
            self._record(query, purpose, "UNAUTHORIZED", actor, case_ref,
                         (self._clock() - t0) * 1000)
            raise
        except UpstreamTimeout:
            self.stats.timeouts += 1
            self._record(query, purpose, "TIMEOUT", actor, case_ref,
                         (self._clock() - t0) * 1000)
            raise

        latency_ms = (self._clock() - t0) * 1000
        self.stats.total_ms += latency_ms

        if raw is None:
            self.stats.misses += 1
            self._record(query, purpose, "NOT_FOUND", actor, case_ref, latency_ms)
            return None

        self.stats.hits += 1
        # Minimise HERE, before the record exists. Nothing downstream is
        # ever handed the full upstream payload to filter for itself.
        released = {k: v for k, v in raw.items() if k in self.RELEASE[purpose]}
        self._record(query, purpose, "HIT", actor, case_ref, latency_ms)
        return GovtRecord(
            system=self.system,
            provenance=self.backend.provenance,
            query=query,
            fields=released,
            retrieved_at=datetime.now(timezone.utc),
            purpose=purpose,
            upstream_ref=raw.get("_upstream_ref"),
            latency_ms=latency_ms,
        )

    def _record(self, query: str, purpose: Purpose, result: str,
                actor: str | None, case_ref: str | None, latency_ms: float) -> None:
        if self._audit is None:
            return
        try:
            self._audit(system=self.system, query=query, purpose=purpose.value,
                        provenance=self.backend.provenance.value, result=result,
                        actor=actor, case_ref=case_ref, latency_ms=latency_ms)
        except Exception:                                   # pragma: no cover
            # Auditing must never fail the lookup it is recording, but a
            # silent audit failure is its own problem, so it is logged.
            from ..log import get_logger
            get_logger("sentinel.govt").exception(
                "audit sink failed for %s lookup", self.system)


class Backend(Protocol):
    """Where an adapter's records actually come from."""

    provenance: Provenance

    def fetch(self, query: str, *, timeout_s: float) -> dict[str, Any] | None: ...


class RealBackend:
    """A real authorised endpoint. Not implemented in this deployment.

    Left unimplemented on purpose. A plausible-looking client written
    against an API specification nobody here has read would produce code
    that appears integrated, passes its own tests, and fails on first
    contact -- while making the submission look more complete than it is.
    The honest artefact is this class, naming exactly what is missing.
    """

    provenance = Provenance.AUTHORITATIVE

    def __init__(self, system: str, requirement: str):
        self.system = system
        self.requirement = requirement

    def fetch(self, query: str, *, timeout_s: float) -> dict[str, Any] | None:
        raise AuthorizationRequired(
            f"{self.system}: no authorised endpoint is configured in this "
            f"deployment. Required: {self.requirement}")
