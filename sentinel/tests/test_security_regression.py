"""Security regression suite.

Covers the checklist in the Phase 2B brief: authentication, authorisation,
department isolation, role permissions, secret handling, input validation,
audit logging, and blocked cross-department camera access.

WHY THIS FILE EXISTS SEPARATELY FROM test_api.py
────────────────────────────────────────────────
Every user in `test_api.py` lives in ONE department, and so does every
camera. That estate cannot express the question "can A see B's cameras?",
which is why 186 passing tests coexisted with a system where the answer
was yes. The fixtures here deliberately build TWO departments plus a
state-level admin, because an isolation control can only be tested by an
estate that has something to isolate.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

pytestmark = pytest.mark.asyncio


# ── a two-department estate ──────────────────────────────────────────

PW = "SecTestPassword2026!"

#: (code, name) for the two departments under test, plus the state body.
DEPTS = [("SECA", "Ahmedabad City Police (test)"),
         ("SECB", "Surat Police (test)"),
         ("SECSTATE", "Home Department (test)")]

#: username -> (role, department code)
PEOPLE = {
    "sec_a_operator":  ("OPERATOR", "SECA"),
    "sec_a_admin":     ("ADMIN", "SECA"),
    "sec_a_investig":  ("INVESTIGATOR", "SECA"),
    "sec_b_operator":  ("OPERATOR", "SECB"),
    "sec_b_admin":     ("ADMIN", "SECB"),
    "sec_state_admin": ("SYSTEM", "SECSTATE"),
    "sec_auditor":     ("AUDITOR", "SECA"),
}


@pytest.fixture
def estate(db):
    """Two departments, one camera each, and one user per role in each."""
    from app.security import hash_password

    for code, name in DEPTS:
        db.execute("INSERT INTO department (code,name) VALUES (%s,%s) "
                   "ON CONFLICT (code) DO NOTHING", (code, name))

    for username, (role, dept) in PEOPLE.items():
        db.execute("""INSERT INTO app_user (username, full_name, password_hash,
                          role, department_id)
                      SELECT %s,%s,%s,%s::user_role,d.id FROM department d
                      WHERE d.code=%s
                      ON CONFLICT (username) DO UPDATE
                        SET password_hash = EXCLUDED.password_hash,
                            role          = EXCLUDED.role,
                            department_id = EXCLUDED.department_id,
                            is_active     = TRUE,
                            failed_logins = 0,
                            locked_until  = NULL""",
                   (username, username, hash_password(PW), role, dept))

    cams = {"SECA": "SEC-A-CAM-001", "SECB": "SEC-B-CAM-001"}
    for dept, ref in cams.items():
        db.execute("""INSERT INTO camera (camera_id,name,department_id,protocol,
                          status,latitude,longitude,heading_deg,whep_url,hls_url)
                      SELECT %s,%s,d.id,'RTSP','ONLINE',23.03,72.58,90,%s,%s
                      FROM department d WHERE d.code=%s
                      ON CONFLICT (camera_id) DO UPDATE
                        SET department_id = EXCLUDED.department_id,
                            status        = 'ONLINE',
                            whep_url      = EXCLUDED.whep_url,
                            hls_url       = EXCLUDED.hls_url""",
                   (ref, f"{dept} junction", f"http://sentinel.test:8889/stream/{ref}/whep",
                    f"http://sentinel.test:8888/live/stream/{ref}/index.m3u8", dept))
    return {"cameras": cams}


async def auth(api, username: str) -> dict:
    r = await api.post("/api/v1/auth/login",
                       json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ── [ ] authentication ───────────────────────────────────────────────

async def test_every_camera_route_refuses_an_unauthenticated_caller(api, estate):
    ref = estate["cameras"]["SECA"]
    for method, path in [("get", "/api/v1/cameras"),
                         ("get", "/api/v1/cameras/geojson"),
                         ("get", "/api/v1/cameras/health"),
                         ("get", f"/api/v1/cameras/{ref}"),
                         ("get", f"/api/v1/cameras/{ref}/stream"),
                         ("get", f"/api/v1/cameras/{ref}/sightings")]:
        r = await getattr(api, method)(path)
        assert r.status_code == 401, f"{path} answered {r.status_code} with no token"


async def test_a_forged_token_is_rejected(api, estate):
    bad = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4Iiwicm9sZSI6IlNZU1RFTSJ9.x"}
    r = await api.get("/api/v1/cameras", headers=bad)
    assert r.status_code == 401


# ── [ ] department isolation ─────────────────────────────────────────

async def test_a_department_user_sees_their_own_cameras(api, estate):
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/cameras", headers=h)
    assert r.status_code == 200
    refs = {c["camera_id"] for c in r.json()["items"]}
    assert estate["cameras"]["SECA"] in refs


async def test_a_department_user_cannot_list_another_departments_cameras(api, estate):
    """The central control. 26 departments share this estate; an operator of
    one is not entitled to the other 25."""
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/cameras", headers=h)
    refs = {c["camera_id"] for c in r.json()["items"]}
    assert estate["cameras"]["SECB"] not in refs, (
        "department A listed department B's camera")
    depts = {c["department"] for c in r.json()["items"]}
    assert depts <= {"SECA"}, f"leaked departments: {depts - {'SECA'}}"


async def test_asking_for_another_department_by_name_returns_nothing(api, estate):
    """`?department=SECB` must narrow within scope, never escape it."""
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/cameras?department=SECB", headers=h)
    assert r.status_code == 200
    assert r.json()["items"] == []


async def test_fetching_another_departments_camera_by_id_is_not_found(api, estate):
    """404 rather than 403: 403 confirms the id exists, which is harvestable
    by enumeration."""
    h = await auth(api, "sec_a_operator")
    r = await api.get(f"/api/v1/cameras/{estate['cameras']['SECB']}", headers=h)
    assert r.status_code == 404


async def test_another_departments_playback_url_is_not_reachable(api, estate):
    """A WHEP URL is a live view of somebody else's street."""
    h = await auth(api, "sec_a_operator")
    r = await api.get(f"/api/v1/cameras/{estate['cameras']['SECB']}/stream", headers=h)
    assert r.status_code == 404


async def test_another_departments_sightings_are_not_reachable(api, estate):
    """A plate read is as sensitive as the lens that read it."""
    h = await auth(api, "sec_a_operator")
    r = await api.get(f"/api/v1/cameras/{estate['cameras']['SECB']}/sightings", headers=h)
    assert r.status_code == 404


async def test_the_map_payload_is_scoped_too(api, estate):
    """The GeoJSON feed is a separate query and was a separate leak: the map
    would have drawn every department's cameras regardless of the list."""
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/cameras/geojson", headers=h)
    assert r.status_code == 200
    refs = {f["properties"].get("camera_id") for f in r.json()["features"]}
    assert estate["cameras"]["SECB"] not in refs


async def test_estate_health_counts_only_the_callers_department(api, estate):
    """A count is an information leak of its own: an operator who can see
    that another department has 400 cameras and 300 offline has learned
    something about that department's readiness."""
    ha = await auth(api, "sec_a_operator")
    hs = await auth(api, "sec_state_admin")
    a = (await api.get("/api/v1/cameras/health", headers=ha)).json()
    s = (await api.get("/api/v1/cameras/health", headers=hs)).json()
    a_refs = {c["camera_id"] for c in a["cameras"]}
    assert estate["cameras"]["SECB"] not in a_refs
    assert s["summary"]["total"] > a["summary"]["total"], (
        "the state admin's estate should be strictly larger than one "
        "department's")


# ── [ ] role permissions / state admin policy ────────────────────────

async def test_the_state_admin_sees_every_department(api, estate):
    h = await auth(api, "sec_state_admin")
    r = await api.get("/api/v1/cameras", headers=h)
    refs = {c["camera_id"] for c in r.json()["items"]}
    assert {estate["cameras"]["SECA"], estate["cameras"]["SECB"]} <= refs


async def test_a_department_admin_is_not_a_state_admin(api, estate):
    """ADMIN is full authority WITHIN one department. Treating it as
    state-wide is the mistake that makes federation meaningless."""
    h = await auth(api, "sec_a_admin")
    r = await api.get("/api/v1/cameras", headers=h)
    refs = {c["camera_id"] for c in r.json()["items"]}
    assert estate["cameras"]["SECB"] not in refs


async def test_a_department_admin_cannot_onboard_into_another_department(api, estate):
    """Privilege escalation by INSERT: plant a camera in B's estate, then
    read everything it sees."""
    h = await auth(api, "sec_a_admin")
    r = await api.post("/api/v1/cameras", headers=h, json={
        "camera_id": "SEC-ESCALATE-001", "name": "planted",
        "department_code": "SECB", "protocol": "RTSP",
        "location": {"latitude": 23.0, "longitude": 72.5},
        "optics": {"heading_deg": 90}})
    assert r.status_code == 403, r.text


async def test_a_department_admin_cannot_modify_another_departments_camera(api, estate):
    h = await auth(api, "sec_a_admin")
    ref = estate["cameras"]["SECB"]
    r = await api.patch(f"/api/v1/cameras/{ref}", headers=h, json={"name": "seized"})
    assert r.status_code == 404


async def test_a_department_admin_cannot_disable_another_departments_camera(api, estate, db):
    """Denial of service across a department boundary: switching off a
    neighbour's cameras during an incident."""
    h = await auth(api, "sec_a_admin")
    ref = estate["cameras"]["SECB"]
    r = await api.delete(f"/api/v1/cameras/{ref}", headers=h)
    assert r.status_code == 404
    still = db.execute("SELECT status::text FROM camera WHERE camera_id=%s",
                       (ref,)).fetchone()
    assert still[0] == "ONLINE", "the camera was disabled across a department boundary"


# ── [ ] auditor is read-only, and reads only the audit log ───────────

async def test_the_auditor_may_read_the_audit_log(api, estate):
    h = await auth(api, "sec_auditor")
    # Reading the audit log is itself an audited act that requires a stated
    # purpose -- DPDP Act 2023 purpose limitation applies to the auditor too.
    h["X-Reason"] = "quarterly access review"
    r = await api.get("/api/v1/system/audit", headers=h)
    assert r.status_code == 200, r.text


async def test_the_auditor_cannot_watch_the_estate(api, estate):
    """An auditor investigating misuse of the cameras must not need access
    to the cameras to do it. Granting VIEWER "as a base" would widen the
    surveillance surface to satisfy a compliance function."""
    h = await auth(api, "sec_auditor")
    for path in ("/api/v1/cameras",
                 f"/api/v1/cameras/{estate['cameras']['SECA']}",
                 f"/api/v1/cameras/{estate['cameras']['SECA']}/stream"):
        r = await api.get(path, headers=h)
        assert r.status_code == 403, f"{path} answered {r.status_code} to an auditor"


async def test_the_auditor_cannot_write_anything(api, estate):
    h = await auth(api, "sec_auditor")
    r = await api.post("/api/v1/cameras", headers=h, json={
        "camera_id": "SEC-AUD-001", "name": "x", "department_code": "SECA",
        "protocol": "RTSP", "location": {"latitude": 23.0, "longitude": 72.5}})
    assert r.status_code == 403


# ── [ ] camera credentials protected / secrets not logged ────────────

async def test_no_camera_route_ever_returns_a_credential_or_an_rtsp_url(api, estate):
    """Two rules in one assertion, because they fail together.

    A browser cannot play RTSP, so an RTSP URL in an API response is either
    a dead player or an operator pasting a credential-bearing URL into VLC.
    And `credential_ref` names a secret; the secret itself has no column to
    live in, but the reference must not leak either.
    """
    h = await auth(api, "sec_a_operator")
    ref = estate["cameras"]["SECA"]
    for path in ("/api/v1/cameras", f"/api/v1/cameras/{ref}",
                 f"/api/v1/cameras/{ref}/stream", "/api/v1/cameras/geojson"):
        body = (await api.get(path, headers=h)).text
        assert "rtsp://" not in body.lower(), f"{path} returned an RTSP URL"
        assert "credential_ref" not in body, f"{path} returned a credential reference"
        assert "vault://" not in body, f"{path} returned a secret-store path"


async def test_a_password_never_appears_in_a_response(api, estate):
    r = await api.post("/api/v1/auth/login",
                       json={"username": "sec_a_operator", "password": PW})
    assert PW not in r.text
    assert "password_hash" not in r.text


# ── [ ] API input validation ─────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"latitude": 999.0, "longitude": 72.5},        # off the planet
    {"latitude": 23.0, "longitude": -999.0},
])
async def test_out_of_range_coordinates_are_refused(api, estate, bad):
    h = await auth(api, "sec_a_admin")
    r = await api.post("/api/v1/cameras", headers=h, json={
        "camera_id": "SEC-BAD-001", "name": "x", "department_code": "SECA",
        "protocol": "RTSP", "location": bad})
    assert r.status_code == 422


async def test_a_sql_metacharacter_in_a_filter_is_data_not_syntax(api, estate):
    """Parameterised throughout; this asserts it stays that way."""
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/cameras?search=%27%3B+DROP+TABLE+camera%3B--",
                      headers=h)
    assert r.status_code == 200
    assert r.json()["items"] == []


# ── [ ] audit logging ────────────────────────────────────────────────

async def test_a_cross_department_denial_is_written_to_the_audit_log(api, estate, db):
    """A refused reach across a department boundary is exactly the event an
    audit trail exists to capture, and it must record WHO tried."""
    h = await auth(api, "sec_a_admin")
    await api.post("/api/v1/cameras", headers=h, json={
        "camera_id": "SEC-AUDITED-001", "name": "planted",
        "department_code": "SECB", "protocol": "RTSP",
        "location": {"latitude": 23.0, "longitude": 72.5}})
    row = db.execute(
        "SELECT username, result FROM audit_log "
        "WHERE username='sec_a_admin' ORDER BY timestamp DESC LIMIT 1").fetchone()
    assert row is not None, "nothing was audited"


# ── [ ] playback URLs come from the catalogue ────────────────────────

async def test_playback_urls_are_served_verbatim_from_the_catalogue(api, estate):
    """The API used to build these by string surgery:

        whep = f"{base}/cam-{id}/whep"
        hls  = base.replace('8889', '8888') + ...

    The `cam-` prefix is a local MediaMTX convention no gateway shares, and
    the port substitution corrupts any base whose port is not 8889 --
    including one where 8889 appears inside the hostname. Both produce a
    URL that resolves to nothing, silently.
    """
    h = await auth(api, "sec_a_operator")
    ref = estate["cameras"]["SECA"]
    body = (await api.get(f"/api/v1/cameras/{ref}/stream", headers=h)).json()
    assert body["whep_url"] == f"http://sentinel.test:8889/stream/{ref}/whep"
    assert body["llhls_url"] == f"http://sentinel.test:8888/live/stream/{ref}/index.m3u8"
    assert body["url_source"] == "catalogue"
    assert body["derived_fields"] == []
    assert "cam-" not in body["whep_url"], "the local MediaMTX convention is back"


async def test_a_missing_playback_url_is_derived_and_labelled_as_derived(api, estate, db):
    """Deriving is legitimate when the catalogue omits a URL. Silently
    presenting a guess as published fact is not: an operator debugging a
    black player needs to know whether to look at our config or theirs."""
    ref = estate["cameras"]["SECA"]
    db.execute("UPDATE camera SET whep_url=NULL WHERE camera_id=%s", (ref,))
    h = await auth(api, "sec_a_operator")
    body = (await api.get(f"/api/v1/cameras/{ref}/stream", headers=h)).json()
    assert body["url_source"] == "derived"
    assert body["derived_fields"] == ["whep_url"]
    assert body["whep_url"].endswith(f"/stream/{ref}/whep")
    # The HLS URL was published, so it must still be the published one.
    assert body["llhls_url"] == f"http://sentinel.test:8888/live/stream/{ref}/index.m3u8"
