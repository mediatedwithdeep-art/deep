"""ANPR read -> normalised plate -> authorised lookup -> rule -> alert.

This is the join between what a camera saw and what the state already
knows, and it is where a VMS stops being a video player. It is also the
most dangerous path in the system, so three rules are enforced here rather
than left to callers.

1. A LOW-CONFIDENCE READ IS NEVER LOOKED UP.
   OCR on a 720p sub-stream is wrong often enough that screening every
   read would query a citizen's registration record on the strength of a
   misread character. The confidence floor is applied before the call, not
   after.

2. SCREENING RELEASES NO PERSONAL DATA.
   Deciding whether to raise an alert needs a status flag. It does not
   need an owner's name or address, and this module never asks for them
   under a screening purpose -- so a false-positive alert cannot expose a
   citizen who was never relevant.

3. AN ALERT CARRIES ITS PROVENANCE.
   An alert raised from a mock record says so, in the alert itself, all
   the way to the operator's screen. An operator acting on "REPORTED
   STOLEN" must be able to tell in one glance whether that came from
   VAHAN or from a demo generator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..plate_rules import correct as correct_plate
from .base import (
    AdapterError, AuthorizationRequired, GovtRecord, Provenance, Purpose,
    RateLimited, UpstreamTimeout,
)
from .registry import AdapterRegistry

#: Below this, a plate read is not trustworthy enough to query a
#: government record system with. Chosen to match the ANPR quality gate's
#: own publish threshold: a read too weak to store is too weak to act on.
MIN_LOOKUP_CONFIDENCE = 0.72


@dataclass
class IntelligenceHit:
    """A reason to raise an alert, with everything needed to justify it."""
    plate: str
    system: str
    provenance: Provenance
    category: str
    severity: str
    detail: dict
    record: GovtRecord

    @property
    def is_real(self) -> bool:
        return self.provenance.is_real

    def as_dict(self) -> dict:
        return {
            "plate": self.plate,
            "system": self.system,
            "provenance": self.provenance.value,
            "is_real_data": self.is_real,
            "banner": self.provenance.banner,
            "category": self.category,
            "severity": self.severity,
            "detail": dict(self.detail),
            "record": self.record.as_dict(),
        }


@dataclass
class ScreeningResult:
    plate_raw: str
    plate: str | None
    looked_up: bool
    hits: list[IntelligenceHit] = field(default_factory=list)
    #: Systems that could not answer, and why. Surfaced rather than
    #: swallowed: "no hit" and "VAHAN was down" must never look the same
    #: to an operator deciding whether to stop a vehicle.
    degraded: dict[str, str] = field(default_factory=dict)
    skipped_reason: str | None = None

    @property
    def any_real(self) -> bool:
        return any(h.is_real for h in self.hits)

    def as_dict(self) -> dict:
        return {
            "plate_raw": self.plate_raw,
            "plate": self.plate,
            "looked_up": self.looked_up,
            "skipped_reason": self.skipped_reason,
            "hits": [h.as_dict() for h in self.hits],
            "degraded": dict(self.degraded),
            "contains_real_data": self.any_real,
        }


def screen_plate(plate_raw: str, confidence: float, registry: AdapterRegistry,
                 *, actor: str | None = None) -> ScreeningResult:
    """Screen one ANPR read against the systems that answer a status query.

    Only VAHAN and eGujCop are screened. SARTHI, AFIS and NAFIS are about
    people, not vehicles, and have no screening purpose at all -- a camera
    reads a plate, never a driver, so there is nothing a passing vehicle
    justifies asking about a person.
    """
    # Lexicon-constrained correction first: the grammar knows which slots
    # are alphabetic and which numeric, so an O/0 confusion is repaired
    # before it becomes a query for a plate that does not exist.
    parsed = correct_plate(plate_raw)
    normalised = parsed.normalized if parsed.valid else None
    result = ScreeningResult(plate_raw=plate_raw, plate=normalised,
                             looked_up=False)

    if not normalised:
        result.skipped_reason = (
            "read did not resolve to a valid Indian plate, even after "
            "lexicon-constrained correction")
        return result
    if confidence < MIN_LOOKUP_CONFIDENCE:
        result.skipped_reason = (
            f"read confidence {confidence:.2f} below the "
            f"{MIN_LOOKUP_CONFIDENCE:.2f} floor for a record-system query")
        return result

    result.looked_up = True
    for system in ("VAHAN", "EGUJCOP"):
        adapter = registry.get(system)
        if adapter is None:
            continue
        try:
            record = adapter.lookup(normalised,
                                    purpose=Purpose.WATCHLIST_SCREENING,
                                    actor=actor)
        except AuthorizationRequired as e:
            # Not an outage. Retrying cannot help, and saying "unavailable"
            # would invite an operator to try again all shift.
            result.degraded[system] = f"not authorised in this deployment: {e}"
            continue
        except UpstreamTimeout as e:
            result.degraded[system] = f"timeout: {e}"
            continue
        except RateLimited as e:
            result.degraded[system] = f"rate limited: {e}"
            continue
        except AdapterError as e:                           # pragma: no cover
            result.degraded[system] = str(e)
            continue

        if record is None:
            continue
        hit = _to_hit(normalised, record)
        if hit is not None:
            result.hits.append(hit)

    return result


def _to_hit(plate: str, record: GovtRecord) -> IntelligenceHit | None:
    """Turn a record into a hit, or into nothing.

    A record is not a hit. Most vehicles are registered, current and of no
    interest; raising an alert for every successful lookup would bury the
    handful that matter.
    """
    f = record.fields
    if record.system == "VAHAN":
        if f.get("is_stolen"):
            return IntelligenceHit(
                plate=plate, system=record.system, provenance=record.provenance,
                category="STOLEN_VEHICLE", severity="CRITICAL",
                detail={"registration_status": f.get("registration_status"),
                        "blacklist_status": f.get("blacklist_status")},
                record=record)
        if f.get("blacklist_status") == "BLACKLISTED":
            return IntelligenceHit(
                plate=plate, system=record.system, provenance=record.provenance,
                category="BLACKLISTED_VEHICLE", severity="HIGH",
                detail={"blacklist_status": f.get("blacklist_status")},
                record=record)
        return None

    if record.system == "EGUJCOP":
        if f.get("wanted"):
            return IntelligenceHit(
                plate=plate, system=record.system, provenance=record.provenance,
                category="WANTED_VEHICLE", severity="CRITICAL",
                detail={"alert_category": f.get("alert_category")},
                record=record)
        return None

    return None
