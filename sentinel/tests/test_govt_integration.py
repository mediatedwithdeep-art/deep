"""Government record-system adapters.

The properties under test are mostly NEGATIVE -- what the adapters refuse
to do, refuse to release, and refuse to claim. That is deliberate. An
integration that returns data is easy to demonstrate; one that cannot be
talked into returning a citizen's address to a false-positive alert is the
one worth having.
"""

from __future__ import annotations

import pytest

from sentinel_core.govt import (
    AuthorizationRequired, Provenance, RateLimited, build_registry,
)
from sentinel_core.govt.adapters import (
    ADAPTERS, FailingBackend, REAL_REQUIREMENTS, VahanAdapter, mock_backend,
)
from sentinel_core.govt.base import AdapterError, Purpose, UpstreamTimeout
from sentinel_core.govt.intelligence import (
    MIN_LOOKUP_CONFIDENCE, screen_plate,
)

from conftest import _auth

#: A plate the mock VAHAN generator marks stolen. Found by generating, not
#: chosen: the generator is seeded from the plate string.
STOLEN_PLATE = "GJ05XY9999"


@pytest.fixture
def registry():
    return build_registry(real_systems=set())


# ── provenance: the property everything else rests on ────────────────

def test_every_mock_record_is_stamped_mock(registry):
    """An operator must never have to guess whether what they are reading
    describes a real citizen."""
    rec = registry["VAHAN"].lookup("GJ01AB1234",
                                   purpose=Purpose.VEHICLE_VERIFICATION)
    assert rec is not None
    assert rec.provenance is Provenance.MOCK
    assert rec.is_real is False
    assert "DEMO DATA" in rec.provenance.banner
    assert rec.as_dict()["is_real_data"] is False


def test_provenance_cannot_be_edited_off_a_record(registry):
    """Re-labelling mock data as authoritative must require constructing a
    new object, which is greppable, rather than assigning an attribute."""
    rec = registry["VAHAN"].lookup("GJ01AB1234",
                                   purpose=Purpose.VEHICLE_VERIFICATION)
    with pytest.raises(Exception):
        rec.provenance = Provenance.AUTHORITATIVE


def test_the_registry_reports_that_nothing_is_really_connected(registry):
    assert registry.any_real is False
    for row in registry.status():
        assert row["is_real_data"] is False
        assert row["requirement_for_real_access"], (
            f"{row['system']} does not say what real access would require")


def test_a_real_backend_refuses_and_names_what_is_missing():
    """"Credentials required" is not useful. Each of these is an
    institutional process, and the message says which one."""
    reg = build_registry(real_systems={"VAHAN"})
    with pytest.raises(AuthorizationRequired) as e:
        reg["VAHAN"].lookup("GJ01AB1234", purpose=Purpose.WATCHLIST_SCREENING)
    assert "VAHAN 4.0 API agreement" in str(e.value)


def test_configuring_an_unknown_system_fails_loudly():
    with pytest.raises(ValueError, match="unknown government system"):
        build_registry(real_systems={"VAHAAN"})


def test_the_default_deployment_is_mock_not_real(monkeypatch):
    """The default direction matters: defaulting to real would mean a
    misconfiguration silently produced AUTHORITATIVE-stamped records from a
    backend nobody verified."""
    monkeypatch.delenv("SENTINEL_GOVT_REAL", raising=False)
    assert build_registry().any_real is False


# ── data minimisation ────────────────────────────────────────────────

def test_screening_releases_a_status_flag_and_no_personal_data(registry):
    """The core DPDP property. Deciding whether to raise an alert needs to
    know the vehicle is stolen. It does not need the owner's home address,
    and a false-positive alert must not expose a citizen who was never
    relevant to anything."""
    rec = registry["VAHAN"].lookup(STOLEN_PLATE,
                                   purpose=Purpose.WATCHLIST_SCREENING)
    assert rec.fields["is_stolen"] is True
    for forbidden in ("owner_name", "owner_address", "chassis_number",
                      "engine_number"):
        assert forbidden not in rec.fields, (
            f"screening released {forbidden}")


def test_verification_releases_the_vehicle_but_still_not_the_owner(registry):
    rec = registry["VAHAN"].lookup("GJ01AB1234",
                                   purpose=Purpose.VEHICLE_VERIFICATION)
    assert "make" in rec.fields and "colour" in rec.fields
    assert "owner_name" not in rec.fields
    assert "owner_address" not in rec.fields


def test_personal_data_requires_a_registered_investigation(registry):
    rec = registry["VAHAN"].lookup("GJ01AB1234",
                                   purpose=Purpose.REGISTERED_INVESTIGATION,
                                   case_ref="FIR/2026/0042")
    assert "owner_name" in rec.fields
    assert "owner_address" in rec.fields


def test_an_investigation_without_a_case_reference_is_refused(registry):
    """The audit trail must be able to answer "under which case was this
    citizen's address retrieved?" years later. Without a reference it
    cannot, so the lookup does not happen."""
    with pytest.raises(AdapterError, match="requires a case_ref"):
        registry["VAHAN"].lookup("GJ01AB1234",
                                 purpose=Purpose.REGISTERED_INVESTIGATION)


def test_the_full_upstream_payload_never_reaches_the_caller(registry):
    """Minimisation happens before the record object exists. Nothing
    downstream is handed everything and trusted to filter."""
    rec = registry["VAHAN"].lookup(STOLEN_PLATE,
                                   purpose=Purpose.WATCHLIST_SCREENING)
    allowed = set(VahanAdapter.RELEASE[Purpose.WATCHLIST_SCREENING])
    assert set(rec.fields) <= allowed
    assert "_upstream_ref" not in rec.fields


@pytest.mark.parametrize("system", ["SARTHI", "AFIS", "NAFIS"])
def test_people_systems_cannot_be_screened_against_passing_traffic(registry, system):
    """A camera reads a plate, never a driver. There is no traffic workflow
    that justifies pulling a licence or a fingerprint record for every
    vehicle that passes a junction, so the purpose does not exist."""
    with pytest.raises(AdapterError, match="does not serve purpose"):
        registry[system].lookup("X", purpose=Purpose.WATCHLIST_SCREENING)


def test_a_fingerprint_system_never_returns_an_identification(registry):
    """A fingerprint match is an examiner's determination. A VMS printing
    "IDENTIFIED: <name>" from an algorithmic score would be manufacturing
    evidence."""
    rec = registry["NAFIS"].lookup("LATENT-001",
                                   purpose=Purpose.REGISTERED_INVESTIGATION,
                                   case_ref="FIR/2026/0042")
    assert rec.fields["decision"] == "REFER_TO_EXAMINER"
    assert "holder_name" not in rec.fields


# ── operational safety: quotas, timeouts, degradation ────────────────

def test_the_local_quota_is_enforced_on_our_side_of_the_call():
    """Government systems withdraw access from integrators that exceed
    published quotas. An ANPR storm must exhaust a counter here rather than
    the integration agreement."""
    adapter = VahanAdapter(mock_backend("VAHAN"), per_minute=3)
    for _ in range(3):
        adapter.lookup("GJ01AB1234", purpose=Purpose.WATCHLIST_SCREENING)
    with pytest.raises(RateLimited):
        adapter.lookup("GJ01AB1234", purpose=Purpose.WATCHLIST_SCREENING)
    assert adapter.stats.rate_limited == 1


def test_a_timeout_is_recorded_and_raised_not_swallowed():
    adapter = VahanAdapter(FailingBackend(UpstreamTimeout), timeout_s=0.1)
    with pytest.raises(UpstreamTimeout):
        adapter.lookup("GJ01AB1234", purpose=Purpose.WATCHLIST_SCREENING)
    assert adapter.stats.timeouts == 1


def test_a_degraded_system_is_reported_not_reported_as_no_hit(registry):
    """"No hit" and "VAHAN was down" must never look the same to an
    operator deciding whether to stop a vehicle."""
    reg = build_registry(real_systems={"VAHAN"})     # unauthorised => degraded
    result = screen_plate(STOLEN_PLATE, 0.95, reg)
    assert result.hits == [] or all(h.system != "VAHAN" for h in result.hits)
    assert "VAHAN" in result.degraded
    assert "not authorised" in result.degraded["VAHAN"]


# ── audit ────────────────────────────────────────────────────────────

def test_every_lookup_is_audited_with_its_purpose_and_actor():
    seen = []
    adapter = VahanAdapter(mock_backend("VAHAN"),
                           audit=lambda **kw: seen.append(kw))
    adapter.lookup("GJ01AB1234", purpose=Purpose.REGISTERED_INVESTIGATION,
                   actor="inspector", case_ref="FIR/2026/0042")
    assert len(seen) == 1
    e = seen[0]
    assert e["system"] == "VAHAN"
    assert e["purpose"] == "REGISTERED_INVESTIGATION"
    assert e["actor"] == "inspector"
    assert e["case_ref"] == "FIR/2026/0042"
    assert e["provenance"] == "MOCK"
    assert e["result"] == "HIT"


def test_a_refused_lookup_is_audited_too():
    """A refusal that leaves no record is one an attacker can retry
    indefinitely without ever appearing in a review."""
    seen = []
    adapter = VahanAdapter(mock_backend("VAHAN"), per_minute=1,
                           audit=lambda **kw: seen.append(kw))
    adapter.lookup("GJ01AB1234", purpose=Purpose.WATCHLIST_SCREENING)
    with pytest.raises(RateLimited):
        adapter.lookup("GJ01AB1234", purpose=Purpose.WATCHLIST_SCREENING)
    assert [e["result"] for e in seen] == ["HIT", "RATE_LIMITED"]


def test_an_audit_sink_failure_never_fails_the_lookup():
    """A broken audit table must not take the estate down with it."""
    def boom(**kw):
        raise RuntimeError("audit table is gone")
    adapter = VahanAdapter(mock_backend("VAHAN"), audit=boom)
    rec = adapter.lookup("GJ01AB1234", purpose=Purpose.WATCHLIST_SCREENING)
    assert rec is not None


# ── ANPR -> plate -> adapter -> rule -> alert ────────────────────────

def test_the_full_intelligence_path_produces_a_provenance_stamped_hit(registry):
    """The demonstration the brief asks for, end to end."""
    result = screen_plate(STOLEN_PLATE, 0.95, registry, actor="controller")
    assert result.looked_up
    assert result.plate == STOLEN_PLATE
    assert result.hits, "a stolen vehicle produced no hit"
    hit = result.hits[0]
    assert hit.category == "STOLEN_VEHICLE"
    assert hit.severity == "CRITICAL"
    assert hit.provenance is Provenance.MOCK
    assert result.as_dict()["contains_real_data"] is False


def test_a_low_confidence_read_never_reaches_a_government_system(registry):
    """OCR on a 720p sub-stream is wrong often enough that screening every
    read would query a citizen's registration record on the strength of a
    misread character."""
    result = screen_plate(STOLEN_PLATE, MIN_LOOKUP_CONFIDENCE - 0.01, registry)
    assert result.looked_up is False
    assert result.hits == []
    assert "confidence" in result.skipped_reason
    assert registry["VAHAN"].stats.calls == 0, "a weak read was looked up anyway"


def test_an_ungrammatical_read_never_reaches_a_government_system(registry):
    result = screen_plate("!!!!", 0.99, registry)
    assert result.looked_up is False
    assert registry["VAHAN"].stats.calls == 0


def test_a_misread_plate_is_corrected_before_it_becomes_a_query(registry):
    """A single O/0 confusion would otherwise query a plate that does not
    exist, and return a confident "no record" for a vehicle that has one."""
    result = screen_plate("GJ 1 8 CD 4 5 6 7", 0.95, registry)
    assert result.plate == "GJ18CD4567"


def test_an_ordinary_vehicle_produces_no_alert(registry):
    """A record is not a hit. Most vehicles are registered, current and of
    no interest; alerting on every successful lookup would bury the handful
    that matter."""
    quiet = [p for p in ("GJ01AB1234", "GJ01AB1235", "GJ01AB1236")
             if not screen_plate(p, 0.95, registry).hits]
    assert quiet, "every test plate was flagged; the rule is not selective"


# ── the adapter set itself ───────────────────────────────────────────

def test_all_five_named_systems_exist_with_a_stated_requirement():
    assert set(ADAPTERS) == {"VAHAN", "SARTHI", "EGUJCOP", "AFIS", "NAFIS"}
    assert set(REAL_REQUIREMENTS) == set(ADAPTERS)


def test_mock_records_are_deterministic(registry):
    """A demo must be reproducible, and two lookups of one plate within an
    investigation must not disagree about the vehicle."""
    a = registry["VAHAN"].lookup("GJ01AB1234", purpose=Purpose.VEHICLE_VERIFICATION)
    b = registry["VAHAN"].lookup("GJ01AB1234", purpose=Purpose.VEHICLE_VERIFICATION)
    assert a.fields == b.fields


# ── the API surface ──────────────────────────────────────────────────
# asyncio_mode=auto, so these are collected as async tests without a marker.

async def test_the_status_endpoint_says_plainly_that_nothing_is_connected(api, users):
    h = await _auth(api, users, "OPERATOR")
    r = await api.get("/api/v1/intelligence/status", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["any_real_integration"] is False
    assert "No government record system is connected" in body["notice"]
    assert {s["system"] for s in body["systems"]} == set(ADAPTERS)
    for s in body["systems"]:
        assert s["is_real_data"] is False
        assert s["requirement_for_real_access"]


async def test_screening_over_the_api_labels_its_provenance(api, users):
    h = await _auth(api, users, "OPERATOR")
    r = await api.post("/api/v1/intelligence/screen", headers=h,
                       json={"plate": STOLEN_PLATE, "confidence": 0.95})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contains_real_data"] is False
    assert body["hits"], "the stolen-vehicle path produced no hit over the API"
    hit = body["hits"][0]
    assert hit["provenance"] == "MOCK"
    assert "DEMO DATA" in hit["banner"]


async def test_a_viewer_cannot_screen_plates(api, users):
    """Screening queries a government record system. It is not a read of our
    own data and does not belong to the lowest role."""
    h = await _auth(api, users, "VIEWER")
    r = await api.post("/api/v1/intelligence/screen", headers=h,
                       json={"plate": STOLEN_PLATE, "confidence": 0.95})
    assert r.status_code == 403


async def test_a_named_lookup_requires_a_stated_reason(api, users):
    """The path that can release personal data records WHY it was used."""
    h = await _auth(api, users, "INVESTIGATOR")
    r = await api.get(f"/api/v1/intelligence/VAHAN/{STOLEN_PLATE}", headers=h)
    assert r.status_code == 400, r.text
    assert "X-Reason" in r.text


async def test_an_operator_cannot_pull_a_named_record(api, users):
    """Gated on evidence:export, not on a read permission: this is the path
    that releases a citizen's personal data."""
    h = await _auth(api, users, "OPERATOR")
    h["X-Reason"] = "curiosity"
    r = await api.get(f"/api/v1/intelligence/VAHAN/{STOLEN_PLATE}", headers=h)
    assert r.status_code == 403


async def test_an_investigation_lookup_over_the_api_needs_a_case_reference(api, users):
    h = await _auth(api, users, "INVESTIGATOR")
    h["X-Reason"] = "FIR/2026/0042 vehicle owner verification"
    r = await api.get(
        f"/api/v1/intelligence/VAHAN/{STOLEN_PLATE}"
        "?purpose=REGISTERED_INVESTIGATION", headers=h)
    assert r.status_code == 422
    assert "case_ref" in r.text


async def test_an_unauthorised_system_answers_501_not_503(api, users, monkeypatch):
    """501, not a 5xx that implies an outage. This deployment has never had
    access; an operator told "unavailable" would retry all shift."""
    import app.routers.intelligence as intel
    from sentinel_core.govt import build_registry
    monkeypatch.setattr(intel, "_REGISTRY_CACHE",
                        build_registry(real_systems={"VAHAN"}))
    h = await _auth(api, users, "INVESTIGATOR")
    h["X-Reason"] = "FIR/2026/0042"
    r = await api.get(f"/api/v1/intelligence/VAHAN/{STOLEN_PLATE}", headers=h)
    assert r.status_code == 501, r.text
    assert "VAHAN 4.0 API agreement" in r.text
