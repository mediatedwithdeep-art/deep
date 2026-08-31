"""Backend API tests: auth, RBAC, audit, search, and the safety rails.

Runs the real ASGI app against a live PostgreSQL via httpx. Skipped when no
database is reachable.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def api(db, pg_dsn):
    """The real app, wired to the test database."""
    import urllib.parse as up
    from sentinel_core.config import get_settings

    parsed = up.urlparse(pg_dsn)
    os.environ.update({
        "POSTGRES_HOST": parsed.hostname or "127.0.0.1",
        "POSTGRES_PORT": str(parsed.port or 5432),
        "POSTGRES_USER": parsed.username or "postgres",
        "POSTGRES_PASSWORD": parsed.password or "",
        "POSTGRES_DB": (parsed.path or "/sentinel_test").lstrip("/"),
        "SECRET_KEY": "test-secret-key-long-enough-for-hs256-signing-abcdef",
        "BUS_BACKEND": "memory",
        "LOG_LEVEL": "ERROR",
        "ENVIRONMENT": "development",
    })
    get_settings.cache_clear()

    import httpx
    from app.main import app
    from app import db as appdb

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            yield client
    await appdb.close_pool()
    get_settings.cache_clear()


@pytest.fixture
def users(db):
    """One user per role, sharing a known password."""
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
    from app.security import hash_password

    db.execute("INSERT INTO department (code,name) VALUES ('API','API Test') "
               "ON CONFLICT (code) DO NOTHING")
    pw = "ApiTestPassword2026!"
    created = {}
    for role in ("VIEWER", "OPERATOR", "INVESTIGATOR", "ADMIN"):
        username = f"api_{role.lower()}"
        # Upsert rather than delete-and-recreate: an earlier test may have
        # left a watchlist entry referencing this user, and deleting the row
        # would trip the foreign key. Resetting the password in place also
        # undoes any password change a previous test made.
        db.execute("""INSERT INTO app_user (username, full_name, password_hash,
                          role, department_id)
                      SELECT %s,%s,%s,%s::user_role,d.id FROM department d
                      WHERE d.code='API'
                      ON CONFLICT (username) DO UPDATE
                        SET password_hash = EXCLUDED.password_hash,
                            role          = EXCLUDED.role,
                            is_active     = TRUE,
                            failed_logins = 0,
                            locked_until  = NULL""",
                   (username, f"{role} User", hash_password(pw), role))
        created[role] = username
    return {"password": pw, **created}


async def _token(api, username: str, password: str) -> str:
    r = await api.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _auth(api, users, role="ADMIN") -> dict:
    token = await _token(api, users[role], users["password"])
    return {"Authorization": f"Bearer {token}"}


# ── health ───────────────────────────────────────────────────────────

async def test_health_needs_no_token(api):
    """A liveness probe that requires a token cannot run before the system
    is up, which is exactly when it matters."""
    r = await api.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_readiness_reports_database_state(api):
    r = await api.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["database"]["healthy"] is True


async def test_metrics_endpoint_serves_prometheus(api):
    r = await api.get("/metrics")
    assert r.status_code == 200
    assert b"sentinel_http_requests_total" in r.content


# ── authentication ───────────────────────────────────────────────────

async def test_login_returns_tokens_and_profile(api, users):
    r = await api.post("/api/v1/auth/login",
                       json={"username": users["ADMIN"], "password": users["password"]})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["role"] == "ADMIN"


@pytest.mark.parametrize("username,password", [
    ("api_admin", "wrong-password"),
    ("no-such-user", "ApiTestPassword2026!"),
])
async def test_bad_credentials_are_indistinguishable(api, users, username, password):
    """A different message or status for 'no such user' hands an attacker a
    free account-enumeration oracle."""
    r = await api.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid credentials"


async def test_protected_endpoints_reject_missing_and_forged_tokens(api):
    assert (await api.get("/api/v1/cameras")).status_code == 401
    r = await api.get("/api/v1/cameras",
                      headers={"Authorization": "Bearer not.a.real.token"})
    assert r.status_code == 401


async def test_refresh_token_is_single_use(api, users):
    """Rotation on use makes a stolen refresh token detectable and
    short-lived: the legitimate holder's next refresh fails loudly."""
    r = await api.post("/api/v1/auth/login",
                       json={"username": users["ADMIN"], "password": users["password"]})
    refresh = r.json()["refresh_token"]
    first = await api.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert first.status_code == 200
    replay = await api.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert replay.status_code == 401


async def test_repeated_failures_lock_the_account(api, users, db):
    db.execute("UPDATE app_user SET failed_logins=0, locked_until=NULL "
               "WHERE username=%s", (users["VIEWER"],))
    for _ in range(5):
        await api.post("/api/v1/auth/login",
                       json={"username": users["VIEWER"], "password": "wrong"})
    r = await api.post("/api/v1/auth/login",
                       json={"username": users["VIEWER"], "password": users["password"]})
    assert r.status_code == 423
    db.execute("UPDATE app_user SET failed_logins=0, locked_until=NULL "
               "WHERE username=%s", (users["VIEWER"],))


async def test_password_change_enforces_policy_and_kills_sessions(api, users, db):
    headers = await _auth(api, users, "OPERATOR")
    weak = await api.post("/api/v1/auth/password", headers=headers,
                          json={"current_password": users["password"],
                                "new_password": "short"})
    assert weak.status_code == 422

    ok = await api.post("/api/v1/auth/password", headers=headers,
                        json={"current_password": users["password"],
                              "new_password": "AnotherGoodPassword2026!"})
    assert ok.status_code == 204
    live = db.execute("SELECT count(*) FROM refresh_token r JOIN app_user u "
                      "ON u.id=r.user_id WHERE u.username=%s AND r.revoked_at IS NULL",
                      (users["OPERATOR"],)).fetchone()[0]
    assert live == 0, "old sessions survived a password change"


# ── authorisation ────────────────────────────────────────────────────

async def test_viewer_cannot_acknowledge_alerts(api, users):
    headers = await _auth(api, users, "VIEWER")
    r = await api.post("/api/v1/alerts/some-id/ack", headers=headers,
                       json={"state": "ACKNOWLEDGED"})
    assert r.status_code == 403
    assert "VIEWER" in r.json()["detail"]


async def test_operator_cannot_create_cameras_but_admin_can(api, users):
    body = {"camera_id": f"T-{uuid.uuid4().hex[:8]}", "name": "Test",
            "department_code": "API",
            "location": {"latitude": 23.0, "longitude": 72.5},
            "optics": {"heading_deg": 90}}
    op = await api.post("/api/v1/cameras", headers=await _auth(api, users, "OPERATOR"),
                        json=body)
    assert op.status_code == 403
    admin = await api.post("/api/v1/cameras", headers=await _auth(api, users, "ADMIN"),
                           json=body)
    assert admin.status_code == 201


async def test_denied_access_is_written_to_the_audit_log(api, users, db):
    """An attempt to reach data outside one's authority is exactly the event
    an audit trail exists to capture."""
    before = db.execute("SELECT count(*) FROM audit_log WHERE result='DENIED'").fetchone()[0]
    await api.post("/api/v1/alerts/x/ack", headers=await _auth(api, users, "VIEWER"),
                   json={"state": "ACKNOWLEDGED"})
    after = db.execute("SELECT count(*) FROM audit_log WHERE result='DENIED'").fetchone()[0]
    assert after > before


# ── DPDP purpose limitation ──────────────────────────────────────────

async def test_movement_history_requires_a_stated_reason(api, users):
    """Vehicle movement history reveals an identifiable person's movements.
    DPDP Act 2023 purpose limitation is enforced at the edge rather than
    trusted to callers."""
    headers = await _auth(api, users, "INVESTIGATOR")
    r = await api.get("/api/v1/vehicles/V-000001/timeline", headers=headers)
    assert r.status_code == 400
    assert "X-Reason" in r.json()["detail"]


# ── cameras ──────────────────────────────────────────────────────────

@pytest.fixture
def seeded_camera(db):
    ref = f"API-CAM-{uuid.uuid4().hex[:6]}"
    db.execute("INSERT INTO department (code,name) VALUES ('API','API Test') "
               "ON CONFLICT (code) DO NOTHING")
    db.execute("""INSERT INTO camera (camera_id,name,department_id,protocol,status,
                    latitude,longitude,heading_deg,fov_deg,range_m,width,height,anpr_capable)
                  SELECT %s,'API Camera',d.id,'RTSP','ONLINE',23.027,72.512,47,82,65,1920,1080,true
                  FROM department d WHERE d.code='API'""", (ref,))
    return ref


async def test_camera_list_and_detail(api, users, seeded_camera):
    headers = await _auth(api, users, "VIEWER")
    listing = await api.get("/api/v1/cameras", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    detail = await api.get(f"/api/v1/cameras/{seeded_camera}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["camera_id"] == seeded_camera
    assert detail.json()["fov_geojson"] is not None


async def test_camera_response_never_contains_credentials(api, users, seeded_camera, db):
    """A camera response reaching a browser must not carry anything that
    could be replayed against the DVR."""
    db.execute("UPDATE camera SET credential_ref='env:SECRET_REF' WHERE camera_id=%s",
               (seeded_camera,))
    r = await api.get(f"/api/v1/cameras/{seeded_camera}",
                      headers=await _auth(api, users, "VIEWER"))
    body = r.text.lower()
    for leak in ("password", "credential_ref", "passwd", "secret_ref"):
        assert leak not in body


async def test_camera_geojson_carries_points_and_fov_polygons(api, users, seeded_camera):
    r = await api.get("/api/v1/cameras/geojson", headers=await _auth(api, users, "VIEWER"))
    assert r.status_code == 200
    kinds = {f["properties"]["kind"] for f in r.json()["features"]}
    assert {"camera", "fov"} <= kinds


async def test_creating_a_camera_without_a_heading_warns(api, users):
    """Without a heading the camera has no field of view and, because the
    adjacency graph is directional, contributes a weaker gate. Warn rather
    than silently accept."""
    r = await api.post("/api/v1/cameras", headers=await _auth(api, users, "ADMIN"),
                       json={"camera_id": f"T-{uuid.uuid4().hex[:8]}", "name": "No heading",
                             "department_code": "API",
                             "location": {"latitude": 23.0, "longitude": 72.5}})
    assert r.status_code == 201
    assert any("heading" in w for w in r.json()["warnings"])


async def test_anpr_flag_on_a_low_resolution_camera_warns(api, users):
    r = await api.post("/api/v1/cameras", headers=await _auth(api, users, "ADMIN"),
                       json={"camera_id": f"T-{uuid.uuid4().hex[:8]}", "name": "Low res ANPR",
                             "department_code": "API",
                             "location": {"latitude": 23.0, "longitude": 72.5},
                             "optics": {"heading_deg": 90},
                             "width": 704, "height": 576, "anpr_capable": True})
    assert r.status_code == 201
    assert any("90px" in w or "readability" in w for w in r.json()["warnings"])


async def test_duplicate_camera_id_is_rejected(api, users, seeded_camera):
    r = await api.post("/api/v1/cameras", headers=await _auth(api, users, "ADMIN"),
                       json={"camera_id": seeded_camera, "name": "Duplicate",
                             "department_code": "API",
                             "location": {"latitude": 23.0, "longitude": 72.5}})
    assert r.status_code == 409


async def test_stream_endpoint_returns_a_whep_url_not_video(api, users, seeded_camera):
    """The API must never proxy video: bytes go browser-to-media-server so
    one slow viewer cannot affect the API."""
    r = await api.get(f"/api/v1/cameras/{seeded_camera}/stream",
                      headers=await _auth(api, users, "VIEWER"))
    assert r.status_code == 200
    assert r.json()["whep_url"].endswith("/whep")
    assert "video" not in r.headers.get("content-type", "")


# ── search ───────────────────────────────────────────────────────────

@pytest.fixture
def seeded_vehicle(db, seeded_camera):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    vtid = f"V-{uuid.uuid4().hex[:6].upper()}"
    db.execute("SELECT count(*) FROM ensure_partitions()")
    db.execute("""INSERT INTO vehicle (vehicle_track_id, first_seen, last_seen,
                      sighting_count, camera_count, vehicle_type, vehicle_color,
                      best_plate, best_plate_conf)
                  VALUES (%s,%s,%s,1,1,'car','white','GJ01AB1234',0.95)""",
               (vtid, now - timedelta(minutes=5), now))
    db.execute("""INSERT INTO vehicle_sighting (sighting_id, timestamp, first_seen,
                      last_seen, camera_id, camera_ref, vehicle_track_id, track_id,
                      vehicle_type, vehicle_color, plate_normalized, plate_confidence,
                      plate_valid_fmt, latitude, longitude)
                  SELECT %s,%s,%s,%s,c.id,%s,%s,'T-1','car','white','GJ01AB1234',0.95,
                         true,23.027,72.512
                  FROM camera c WHERE c.camera_id=%s""",
               (f"S-{uuid.uuid4().hex[:8]}", now, now, now, seeded_camera, vtid,
                seeded_camera))
    return vtid


async def test_plate_search_is_fuzzy_and_says_so(api, users, seeded_vehicle):
    """OCR confusions are systematic, so exact matching would miss most real
    reads. The response must say it was fuzzy, or an operator may believe
    they got a read the system never made."""
    headers = await _auth(api, users, "VIEWER")
    exact = await api.get("/api/v1/vehicles/search?plate=GJ01AB1234", headers=headers)
    assert exact.json()["count"] >= 1

    garbled = await api.get("/api/v1/vehicles/search?plate=GJO1AB1Z34", headers=headers)
    body = garbled.json()
    assert body["count"] >= 1, "O/0 and Z/2 confusions were not tolerated"
    assert body["search"]["match_type"] == "fuzzy"
    assert "Verify" in body["search"]["note"]


async def test_search_for_an_unrelated_plate_returns_nothing(api, users, seeded_vehicle):
    r = await api.get("/api/v1/vehicles/search?plate=MH99ZZ0000",
                      headers=await _auth(api, users, "VIEWER"))
    assert r.json()["count"] == 0


async def test_timeline_exposes_the_reasoning_behind_each_hop(api, users, seeded_vehicle):
    headers = await _auth(api, users, "INVESTIGATOR")
    headers["X-Reason"] = "FIR 0142/2026 enquiry"
    r = await api.get(f"/api/v1/vehicles/{seeded_vehicle}/timeline", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["hop_count"] >= 1
    hop = body["hops"][0]
    assert "association" in hop and "decision" in hop["association"]


async def test_timeline_read_is_audited_with_its_reason(api, users, seeded_vehicle, db):
    headers = await _auth(api, users, "INVESTIGATOR")
    headers["X-Reason"] = "FIR 0142/2026 vehicle movement enquiry"
    await api.get(f"/api/v1/vehicles/{seeded_vehicle}/timeline", headers=headers)
    row = db.execute("SELECT reason FROM audit_log WHERE action='VEHICLE_TIMELINE_READ' "
                     "ORDER BY timestamp DESC LIMIT 1").fetchone()
    assert row is not None and "FIR 0142/2026" in row[0]


async def test_track_geojson_is_renderable(api, users, seeded_vehicle):
    r = await api.get(f"/api/v1/vehicles/{seeded_vehicle}/track.geojson",
                      headers=await _auth(api, users, "VIEWER"))
    assert r.status_code == 200
    assert r.json()["type"] == "FeatureCollection"


# ── watchlist and rules ──────────────────────────────────────────────

async def test_watchlist_entry_requires_a_query_and_a_reason(api, users):
    headers = await _auth(api, users, "INVESTIGATOR")
    empty = await api.post("/api/v1/watchlist", headers=headers,
                           json={"label": "Nothing", "reason": "investigation"})
    assert empty.status_code == 422

    no_reason = await api.post("/api/v1/watchlist", headers=headers,
                               json={"label": "X", "plate_query": "GJ01AB1234"})
    assert no_reason.status_code == 422


async def test_watchlist_warns_on_an_invalid_plate_but_still_accepts_it(api, users):
    """A partially known plate is still useful and fuzzy matching will do
    the work, but the operator should be told to expect noise."""
    r = await api.post("/api/v1/watchlist",
                       headers=await _auth(api, users, "INVESTIGATOR"),
                       json={"label": "Partial", "plate_query": "ZZ99XX",
                             "reason": "witness statement, partial plate"})
    assert r.status_code == 201
    assert any("not a valid Indian plate" in w for w in r.json()["warnings"])


async def test_alert_rules_are_runtime_configuration(api, users):
    headers = await _auth(api, users, "ADMIN")
    listing = await api.get("/api/v1/alert-rules", headers=headers)
    assert listing.status_code == 200
    codes = {r["code"] for r in listing.json()["items"]}
    assert "WATCHLIST_PLATE" in codes

    patched = await api.patch("/api/v1/alert-rules/WATCHLIST_PLATE", headers=headers,
                              json={"dedup_seconds": 120})
    assert patched.status_code == 200
    assert "no restart" in patched.json()["note"]


# ── analytics ────────────────────────────────────────────────────────

async def test_dashboard_returns_the_headline_counters(api, users):
    r = await api.get("/api/v1/dashboard", headers=await _auth(api, users, "VIEWER"))
    assert r.status_code == 200
    for key in ("cameras_online", "cameras_offline", "active_alerts",
                "vehicles_tracked_1h", "anpr_events_1h"):
        assert key in r.json()["stats"]


async def test_anpr_analytics_reports_per_camera_class(api, users, seeded_camera):
    """A blended estate-wide read rate is meaningless: a wide-angle camera
    physically cannot resolve a plate."""
    r = await api.get("/api/v1/analytics/anpr", headers=await _auth(api, users, "VIEWER"))
    assert r.status_code == 200
    body = r.json()
    assert "by_camera_class" in body
    assert "meaningless" in body["note"]


async def test_camera_health_summary_is_a_first_class_view(api, users, seeded_camera):
    """Roughly a fifth of a real government estate is broken at any moment.
    A VMS that does not surface that is lying to its operators."""
    r = await api.get("/api/v1/cameras/health", headers=await _auth(api, users, "VIEWER"))
    assert r.status_code == 200
    summary = r.json()["summary"]
    for key in ("total", "online", "offline", "stale", "firmware_at_risk", "mean_trust"):
        assert key in summary


async def test_system_status_reports_component_health(api, users):
    r = await api.get("/api/v1/system/status", headers=await _auth(api, users, "VIEWER"))
    assert r.status_code == 200
    for key in ("database", "ingestion", "throughput", "websocket", "partitions"):
        assert key in r.json()


# ── responses ────────────────────────────────────────────────────────

async def test_every_response_carries_a_trace_id_and_security_headers(api):
    r = await api.get("/health")
    assert r.headers.get("X-Trace-Id")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"


async def test_openapi_schema_documents_every_endpoint(api):
    r = await api.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert len(paths) >= 30
    for expected in ("/api/v1/auth/login", "/api/v1/cameras",
                     "/api/v1/vehicles/search", "/api/v1/alerts",
                     "/api/v1/dashboard"):
        assert expected in paths
