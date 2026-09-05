"""The five systems, each with its field-release policy and a mock backend.

Every mock record is derived deterministically from the query string, so a
demo is reproducible and two lookups of the same plate agree — but nothing
here corresponds to a real vehicle, licence, person or fingerprint.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from .base import (
    AuthorizationRequired, Backend, GovtAdapter, Provenance, Purpose,
    RealBackend, UpstreamTimeout,
)

# ── deterministic fabrication ────────────────────────────────────────
# Seeded from the query so the same plate yields the same fake record in
# every run of the demo. Names are drawn from a small fixed list that is
# obviously a list, and the "documents" are structurally valid but
# reserved-range values.

_FIRST = ["Ramesh", "Priya", "Suresh", "Anita", "Vikram", "Meera",
          "Jayesh", "Kavita", "Nilesh", "Bhavna"]
_LAST = ["Patel", "Shah", "Desai", "Mehta", "Joshi", "Trivedi",
         "Chauhan", "Parmar", "Solanki", "Rana"]
_MAKES = [("Maruti Suzuki", "Swift"), ("Hyundai", "i20"), ("Tata", "Nexon"),
          ("Mahindra", "Bolero"), ("Honda", "City"), ("Toyota", "Innova")]
_COLOURS = ["white", "silver", "red", "blue", "black", "grey"]
_RTO = ["GJ01 Ahmedabad", "GJ05 Surat", "GJ06 Vadodara", "GJ03 Rajkot"]


def _rng(query: str, salt: str) -> random.Random:
    h = hashlib.sha256(f"{salt}:{query}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def _person(r: random.Random) -> str:
    return f"{r.choice(_FIRST)} {r.choice(_LAST)}"


class MockBackend:
    """Fabricates a record from the query. Never a real person's data."""

    provenance = Provenance.MOCK

    def __init__(self, generator, *, hit_rate: float = 1.0, salt: str = ""):
        self._generate = generator
        self._hit_rate = hit_rate
        self._salt = salt

    def fetch(self, query: str, *, timeout_s: float) -> dict[str, Any] | None:
        r = _rng(query, self._salt)
        if r.random() > self._hit_rate:
            return None
        return self._generate(query, r)


class FailingBackend:
    """Simulates an upstream that is reachable but not answering.

    Present because "the integration works" and "the integration degrades
    correctly" are different claims, and only the second one matters at
    03:00 when NIC's link is down.
    """

    provenance = Provenance.MOCK

    def __init__(self, error: type[Exception] = UpstreamTimeout):
        self._error = error

    def fetch(self, query: str, *, timeout_s: float) -> dict[str, Any] | None:
        raise self._error(f"upstream did not answer within {timeout_s}s")


# ── VAHAN · vehicle registration ─────────────────────────────────────

def _vahan_record(plate: str, r: random.Random) -> dict[str, Any]:
    make, model = r.choice(_MAKES)
    stolen = r.random() < 0.06
    return {
        "_upstream_ref": f"MOCK-VAHAN-{r.randrange(10**9):09d}",
        "registration_number": plate,
        "registration_status": "ACTIVE" if not stolen else "REPORTED_STOLEN",
        "is_stolen": stolen,
        "blacklist_status": "BLACKLISTED" if stolen else "CLEAR",
        "make": make,
        "model": model,
        "colour": r.choice(_COLOURS),
        "vehicle_class": "LMV",
        "fuel_type": r.choice(["PETROL", "DIESEL", "CNG", "ELECTRIC"]),
        "manufacture_year": r.randrange(2012, 2026),
        "rto": r.choice(_RTO),
        # Personal data. Released only under REGISTERED_INVESTIGATION.
        "owner_name": _person(r),
        "owner_address": f"{r.randrange(1, 400)}, "
                         f"{r.choice(['Satellite', 'Navrangpura', 'Maninagar'])}, "
                         f"Ahmedabad",
        "chassis_number": f"MA3MOCK{r.randrange(10**8):08d}",
        "engine_number": f"MOCK{r.randrange(10**7):07d}",
        "insurance_valid_upto": f"{r.randrange(2026, 2029)}-{r.randrange(1,13):02d}-15",
        "puc_valid_upto": f"{r.randrange(2026, 2028)}-{r.randrange(1,13):02d}-10",
    }


class VahanAdapter(GovtAdapter):
    """National vehicle registration (MoRTH).

    The field release is the point of this class. Screening a plate against
    a stolen-vehicle list needs a status flag, not an owner's home address.
    A system that fetches the owner's address in order to decide whether to
    raise a watchlist alert has processed a citizen's personal data for
    every vehicle that drove past a camera.
    """

    system = "VAHAN"
    RELEASE = {
        Purpose.WATCHLIST_SCREENING: (
            "registration_number", "registration_status", "is_stolen",
            "blacklist_status"),
        Purpose.VEHICLE_VERIFICATION: (
            "registration_number", "registration_status", "is_stolen",
            "blacklist_status", "make", "model", "colour", "vehicle_class",
            "fuel_type", "manufacture_year", "rto"),
        Purpose.REGISTERED_INVESTIGATION: (
            "registration_number", "registration_status", "is_stolen",
            "blacklist_status", "make", "model", "colour", "vehicle_class",
            "fuel_type", "manufacture_year", "rto", "owner_name",
            "owner_address", "chassis_number", "engine_number",
            "insurance_valid_upto", "puc_valid_upto"),
    }


# ── SARTHI · driving licence ─────────────────────────────────────────

def _sarthi_record(dl: str, r: random.Random) -> dict[str, Any]:
    suspended = r.random() < 0.08
    return {
        "_upstream_ref": f"MOCK-SARTHI-{r.randrange(10**9):09d}",
        "licence_number": dl,
        "licence_status": "SUSPENDED" if suspended else "ACTIVE",
        "is_suspended": suspended,
        "holder_name": _person(r),
        "date_of_birth": f"{r.randrange(1960, 2006)}-{r.randrange(1,13):02d}-"
                         f"{r.randrange(1,29):02d}",
        "address": f"{r.randrange(1, 400)}, Ahmedabad",
        "vehicle_classes": r.choice([["LMV"], ["LMV", "MCWG"], ["MCWG"]]),
        "valid_upto": f"{r.randrange(2026, 2040)}-06-30",
        "issuing_rto": r.choice(_RTO),
    }


class SarthiAdapter(GovtAdapter):
    """National driving licence (MoRTH).

    No screening purpose. A licence record is about a PERSON, and there is
    no traffic-camera workflow that justifies pulling one for every vehicle
    that passes: a camera reads a plate, not a driver. Licence lookups
    belong to a named investigation, so that is the only purpose offered.
    """

    system = "SARTHI"
    RELEASE = {
        Purpose.REGISTERED_INVESTIGATION: (
            "licence_number", "licence_status", "is_suspended", "holder_name",
            "date_of_birth", "vehicle_classes", "valid_upto", "issuing_rto",
            "address"),
    }


# ── eGujCop · Gujarat Police records ─────────────────────────────────

def _egujcop_record(plate: str, r: random.Random) -> dict[str, Any]:
    wanted = r.random() < 0.05
    return {
        "_upstream_ref": f"MOCK-EGC-{r.randrange(10**9):09d}",
        "identifier": plate,
        "wanted": wanted,
        "alert_category": r.choice(
            ["THEFT", "ABSCONDING", "SUSPECT_VEHICLE"]) if wanted else None,
        "linked_firs": ([f"MOCK/FIR/{r.randrange(2023, 2027)}/"
                         f"{r.randrange(1, 9999):04d}"] if wanted else []),
        "police_station": r.choice(
            ["Satellite PS", "Navrangpura PS", "Maninagar PS"]) if wanted else None,
        "case_summary": ("Vehicle listed in a mock theft report."
                         if wanted else None),
        "last_updated": f"{r.randrange(2024, 2027)}-{r.randrange(1,13):02d}-01",
    }


class EGujCopAdapter(GovtAdapter):
    """Gujarat Police records (CCTNS-linked)."""

    system = "EGUJCOP"
    RELEASE = {
        Purpose.WATCHLIST_SCREENING: ("identifier", "wanted", "alert_category"),
        Purpose.VEHICLE_VERIFICATION: (
            "identifier", "wanted", "alert_category", "last_updated"),
        Purpose.REGISTERED_INVESTIGATION: (
            "identifier", "wanted", "alert_category", "linked_firs",
            "police_station", "case_summary", "last_updated"),
    }


# ── AFIS / NAFIS · fingerprint identification ────────────────────────

def _afis_record(ref: str, r: random.Random) -> dict[str, Any]:
    return {
        "_upstream_ref": f"MOCK-AFIS-{r.randrange(10**9):09d}",
        "query_ref": ref,
        "candidate_count": r.randrange(0, 4),
        "top_score": round(r.uniform(0.30, 0.94), 3),
        "decision": "REFER_TO_EXAMINER",
        "subject_ref": f"MOCK-SUBJ-{r.randrange(10**6):06d}",
    }


class _FingerprintAdapter(GovtAdapter):
    """Shared behaviour for AFIS and NAFIS.

    Neither ever returns an identification, only a candidate list and a
    referral. A fingerprint match is an examiner's determination in Indian
    practice, and a VMS that printed "IDENTIFIED: <name>" from an
    algorithmic score would be manufacturing evidence. The decision field
    is fixed at REFER_TO_EXAMINER for that reason.

    No screening purpose exists on either: biometric identification is
    never a routine screen against passing traffic.
    """

    RELEASE = {
        Purpose.REGISTERED_INVESTIGATION: (
            "query_ref", "candidate_count", "top_score", "decision",
            "subject_ref"),
    }


class AfisAdapter(_FingerprintAdapter):
    """Gujarat state fingerprint identification."""
    system = "AFIS"


class NafisAdapter(_FingerprintAdapter):
    """National Automated Fingerprint Identification System (NCRB)."""
    system = "NAFIS"


# ── what a real deployment would need ────────────────────────────────
#
# Named per system so the gap is specific rather than "credentials
# required". Each of these is an institutional process, not a config value.

REAL_REQUIREMENTS = {
    "VAHAN": "MoRTH/NIC VAHAN 4.0 API agreement, a registered integrator "
             "identity, and an IP allow-listed gateway",
    "SARTHI": "MoRTH/NIC SARATHI API agreement; personal-data access "
              "additionally requires a DPDP-compliant purpose registration",
    "EGUJCOP": "Gujarat Police CCTNS integration approval and a "
               "state-network (GSWAN) route",
    "AFIS": "Gujarat State FSL authorisation; biometric queries are "
            "examiner-initiated and cannot be automated from a VMS",
    "NAFIS": "NCRB NAFIS authorisation; same examiner-initiated constraint",
}


def real_backend(system: str) -> RealBackend:
    return RealBackend(system, REAL_REQUIREMENTS[system])


def mock_backend(system: str) -> MockBackend:
    return {
        "VAHAN": lambda: MockBackend(_vahan_record, salt="vahan"),
        "SARTHI": lambda: MockBackend(_sarthi_record, salt="sarthi"),
        "EGUJCOP": lambda: MockBackend(_egujcop_record, salt="egujcop"),
        "AFIS": lambda: MockBackend(_afis_record, salt="afis"),
        "NAFIS": lambda: MockBackend(_afis_record, salt="nafis"),
    }[system]()


ADAPTERS: dict[str, type[GovtAdapter]] = {
    "VAHAN": VahanAdapter,
    "SARTHI": SarthiAdapter,
    "EGUJCOP": EGujCopAdapter,
    "AFIS": AfisAdapter,
    "NAFIS": NafisAdapter,
}
