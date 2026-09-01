"""Department isolation beyond the camera registry.

WHY THIS FILE EXISTS SEPARATELY FROM test_security_regression.py
────────────────────────────────────────────────────────────────
That suite attacks /cameras, and every one of its 25 tests passed while
the vehicle, sighting, alert and WebSocket surfaces were state-wide for
every operator in every department. Scoping the registry is not scoping
the estate: everything derived from a camera -- who was seen, where they
went, what was alerted on -- inherits that camera's department, and each
had to be scoped in its own query.

These tests are written as attacks. Each one performs the access a
department A user should not be able to perform and asserts the system
refuses it, so a regression reads as a breach rather than as a diff.
"""
from __future__ import annotations
import pathlib
import sys
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

pytestmark = pytest.mark.asyncio

from test_security_regression import auth, estate  # noqa: F401


@pytest.fixture
def victim_data(db, estate):
    """A vehicle seen ONLY by department B's camera.

    Department A has no lawful basis to know this vehicle exists.
    """
    db.execute("""INSERT INTO vehicle (vehicle_track_id, first_seen, last_seen,
                      sighting_count, camera_count, vehicle_type, vehicle_color,
                      best_plate, best_plate_conf, plate_read_count)
                  VALUES ('VICTIM-TRACK-B', now()-interval '10 min', now(),
                          1, 1, 'car', 'white', 'GJ05XX9999', 0.97, 1)
                  ON CONFLICT (vehicle_track_id) DO NOTHING""")
    db.execute("""INSERT INTO vehicle_sighting (sighting_id, timestamp, first_seen,
                      last_seen, camera_id, camera_ref, vehicle_track_id, track_id,
                      vehicle_type, vehicle_color, plate_raw, plate_normalized,
                      plate_confidence, plate_valid_fmt, latitude, longitude)
                  SELECT 'VICTIM-SIGHT-B', now(), now()-interval '1 min', now(),
                         c.id, c.camera_id, 'VICTIM-TRACK-B', 'T-1',
                         'car','white','GJ05XX9999','GJ05XX9999',0.97,TRUE,23.03,72.58
                  FROM camera c WHERE c.camera_id='SEC-B-CAM-001'
                  ON CONFLICT DO NOTHING""")
    return {"plate": "GJ05XX9999", "track": "VICTIM-TRACK-B"}


async def test_vehicle_search_leaks_another_department(api, victim_data):
    """A: search for B's vehicle by plate."""
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/vehicles/search",
                      params={"plate": victim_data["plate"]}, headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items == [], (
        f"LEAK: dept A operator retrieved {len(items)} vehicle(s) seen only by "
        f"dept B. Payload: {items}")


async def test_vehicle_detail_leaks_another_department(api, victim_data):
    h = await auth(api, "sec_a_operator")
    r = await api.get(f"/api/v1/vehicles/{victim_data['track']}", headers=h)
    assert r.status_code == 404, (
        f"LEAK: dept A read B's vehicle detail -> {r.status_code} {r.text[:300]}")


async def test_movement_timeline_leaks_another_department(api, victim_data):
    """The most sensitive endpoint in the system: where a vehicle went."""
    h = await auth(api, "sec_a_operator")
    h["X-Reason"] = "red team probe"
    r = await api.get(f"/api/v1/vehicles/{victim_data['track']}/timeline", headers=h)
    body = r.text
    assert r.status_code == 404 or "SEC-B-CAM-001" not in body, (
        f"LEAK: dept A retrieved B's movement history -> {r.status_code} {body[:400]}")


async def test_sightings_feed_leaks_another_department(api, victim_data):
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/sightings", params={"hours": 24}, headers=h)
    assert r.status_code == 200, r.text
    refs = {i.get("camera_ref") for i in r.json().get("items", [])}
    assert "SEC-B-CAM-001" not in refs, (
        f"LEAK: dept A sightings feed contains dept B camera. refs={refs}")


async def test_live_geojson_leaks_another_department(api, victim_data):
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/sightings/live.geojson", params={"minutes": 60}, headers=h)
    assert r.status_code == 200, r.text
    assert "SEC-B-CAM-001" not in r.text, (
        "LEAK: dept A live map payload contains dept B camera")


@pytest.fixture
def victim_alert(db, estate, victim_data):
    db.execute("""INSERT INTO alert (alert_id, alert_type, severity, title, message,
                      camera_id, camera_ref, vehicle_track_id, timestamp, state)
                  SELECT 'VICTIM-ALERT-B','WATCHLIST_HIT'::alert_type,
                         'CRITICAL'::alert_severity,
                         'B-only alert','seen by dept B only',
                         c.id, c.camera_id, 'VICTIM-TRACK-B', now(),
                         'NEW'::alert_state
                  FROM camera c WHERE c.camera_id='SEC-B-CAM-001'
                  ON CONFLICT DO NOTHING""")
    return {"alert_id": "VICTIM-ALERT-B"}


async def test_alerts_leak_another_department(api, victim_alert):
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/alerts", params={"hours": 24}, headers=h)
    assert r.status_code == 200, r.text
    ids = {i.get("alert_id") for i in r.json().get("items", [])}
    assert "VICTIM-ALERT-B" not in ids, f"LEAK: dept A sees dept B alert. ids={ids}"


async def test_alert_ack_hijacks_another_department(api, victim_alert):
    """Not just reading -- can A *acknowledge* B's critical alert?"""
    h = await auth(api, "sec_a_operator")
    r = await api.post("/api/v1/alerts/VICTIM-ALERT-B/ack",
                       json={"note": "hijacked"}, headers=h)
    assert r.status_code == 404, (
        f"LEAK: dept A acknowledged dept B's CRITICAL alert -> {r.status_code} {r.text[:200]}")


async def test_audit_log_leaks_other_departments(api, estate):
    """Can a dept A investigator read dept B's audit trail?"""
    hb = await auth(api, "sec_b_operator")
    await api.get("/api/v1/cameras", headers=hb)          # generate B activity
    ha = await auth(api, "sec_a_investig")
    ha["X-Reason"] = "red team probe of audit scope"
    r = await api.get("/api/v1/system/audit", params={"limit": 200}, headers=ha)
    assert r.status_code == 200, r.text
    depts = {i.get("department") for i in r.json().get("items", [])}
    assert depts <= {"SECA", None}, (
        f"LEAK: dept A investigator read audit entries for {depts}")


# ── the live channel ─────────────────────────────────────────────────

async def test_websocket_broadcast_leaks_another_department():
    """A dept A socket must not receive a dept B camera's events."""
    from app import ws as wsmod

    mgr = wsmod.ConnectionManager()
    mgr._camera_dept = {"SEC-A-CAM-001": "SECA", "SEC-B-CAM-001": "SECB"}
    mgr._dept_refreshed_at = 1e18          # never refresh during the test

    class FakeSocket:
        def __init__(self): self.sent = []
        async def accept(self): pass
        async def send_json(self, m): self.sent.append(m)
        async def close(self, **kw): pass

    a_sock, b_sock, sys_sock = FakeSocket(), FakeSocket(), FakeSocket()
    a = await mgr.connect(a_sock, "a_op", department="SECA")
    b = await mgr.connect(b_sock, "b_op", department="SECB")
    root = await mgr.connect(sys_sock, "state", department="SECSTATE", sees_all=True)
    for c in (a, b, root):
        c.channels = {"*"}

    await mgr._route(wsmod.Topics.SIGHTINGS, {"camera_id": "SEC-B-CAM-001",
                                   "vehicle_track_id": "VICTIM-TRACK-B",
                                   "plate_normalized": "GJ05XX9999"})

    def drained(c):
        out = []
        while not c.queue.empty():
            out.append(c.queue.get_nowait())
        return out

    a_got, b_got, root_got = drained(a), drained(b), drained(root)
    assert a_got == [], f"LEAK: dept A socket received dept B events: {a_got}"
    assert b_got, "dept B socket should have received its own camera's event"
    assert root_got, "state admin should receive every department's events"


async def test_websocket_unattributable_event_is_not_broadcast_widely():
    """An event we cannot attribute reaches only the state admin."""
    from app import ws as wsmod

    mgr = wsmod.ConnectionManager()
    mgr._camera_dept = {}
    mgr._dept_refreshed_at = 1e18

    class FakeSocket:
        async def accept(self): pass
        async def send_json(self, m): pass
        async def close(self, **kw): pass

    a = await mgr.connect(FakeSocket(), "a_op", department="SECA")
    root = await mgr.connect(FakeSocket(), "state", department="SECSTATE", sees_all=True)
    a.channels = root.channels = {"*"}

    await mgr._route(wsmod.Topics.ALERTS, {"camera_id": "UNKNOWN-CAM", "title": "x"})

    assert a.queue.empty(), "LEAK: unattributable event reached a scoped socket"
    assert not root.queue.empty(), "state admin should still receive it"


# ── aggregates and investigation metadata ────────────────────────────

async def test_watchlist_leaks_another_departments_cases(api, estate, db):
    """A watchlist entry names a case. It must not cross a department."""
    db.execute("""INSERT INTO watchlist (label, plate_query, plate_canonical,
                      severity, reason, case_ref, created_by)
                  SELECT 'B-only watch','GJ05XX9999','GJ05XX9999',
                         'CRITICAL','dept B investigation','FIR-B-2026-001', u.id
                  FROM app_user u WHERE u.username='sec_b_operator'""")
    h = await auth(api, "sec_a_investig")
    r = await api.get("/api/v1/watchlist", headers=h)
    assert r.status_code == 200, r.text
    body = r.text
    assert "FIR-B-2026-001" not in body and "B-only watch" not in body, (
        f"LEAK: dept A read dept B's watchlist case. {body[:400]}")


async def test_camera_analytics_leaks_the_other_departments_inventory(api, victim_data):
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/analytics/cameras", headers=h)
    assert r.status_code == 200, r.text
    assert "SEC-B-CAM-001" not in r.text, (
        "LEAK: dept A analytics names a dept B camera")


async def test_alert_summary_counts_only_the_callers_department(api, victim_alert):
    """A's summary must not count B's alert."""
    ha = await auth(api, "sec_a_operator")
    ra = await api.get("/api/v1/alerts/summary", headers=ha)
    hs = await auth(api, "sec_state_admin")
    rs = await api.get("/api/v1/alerts/summary", headers=hs)
    assert ra.status_code == rs.status_code == 200
    a_total = ra.json()["counts"]["total_24h"]
    state_total = rs.json()["counts"]["total_24h"]
    assert state_total > a_total, (
        f"LEAK: dept A's alert total ({a_total}) matches the state-wide total "
        f"({state_total}); the summary is not scoped")


# ── inference leaks: what a scoped row still admits ──────────────────


@pytest.fixture
def cross_boundary(db, estate):
    """One vehicle seen by BOTH departments: 1 hop in A, 3 hops in B."""
    db.execute("""INSERT INTO vehicle (vehicle_track_id, first_seen, last_seen,
                      sighting_count, camera_count, vehicle_type, vehicle_color,
                      best_plate, best_plate_conf, plate_read_count, total_distance_m)
                  VALUES ('XB-TRACK', now()-interval '30 min', now(), 4, 2,
                          'car','white','GJ07ZZ7777',0.95,4, 8800)
                  ON CONFLICT (vehicle_track_id) DO UPDATE
                    SET sighting_count=4, camera_count=2, total_distance_m=8800""")
    for i, cam in enumerate(["SEC-A-CAM-001", "SEC-B-CAM-001",
                             "SEC-B-CAM-001", "SEC-B-CAM-001"]):
        db.execute("""INSERT INTO vehicle_sighting (sighting_id, timestamp, first_seen,
                          last_seen, camera_id, camera_ref, vehicle_track_id, track_id,
                          vehicle_type, vehicle_color, plate_normalized,
                          plate_confidence, plate_valid_fmt, latitude, longitude)
                      SELECT %s, now()-make_interval(mins=>%s), now()-make_interval(mins=>%s),
                             now()-make_interval(mins=>%s), c.id, c.camera_id,
                             'XB-TRACK', 'T-XB','car','white','GJ07ZZ7777',0.95,TRUE,
                             23.03, 72.58
                      FROM camera c WHERE c.camera_id=%s
                      ON CONFLICT DO NOTHING""",
                   (f"XB-SIGHT-{i}", 30 - i * 5, 30 - i * 5, 30 - i * 5, cam))
    return {"track": "XB-TRACK"}


async def test_vehicle_detail_aggregates_do_not_count_other_departments(
        api, cross_boundary):
    """A saw 1 hop on 1 camera. Does the detail admit to 4 hops on 2?"""
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/vehicles/XB-TRACK", headers=h)
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["sighting_count"], b["camera_count"]) == (1, 1), (
        f"LEAK: aggregates are estate-wide. A saw 1 sighting on 1 camera but "
        f"the API reports sighting_count={b['sighting_count']} "
        f"camera_count={b['camera_count']} distance={b.get('total_distance_m')}")


async def test_next_cameras_does_not_name_another_departments_camera(
        api, cross_boundary):
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/vehicles/XB-TRACK/next-cameras", headers=h)
    assert r.status_code in (200, 404), r.text
    assert "SEC-B-CAM-001" not in r.text, (
        f"LEAK: next-cameras names a dept B camera: {r.text[:400]}")


async def test_search_result_camera_list_is_scoped(api, cross_boundary):
    """The search row carries an array of every camera that saw the vehicle."""
    h = await auth(api, "sec_a_operator")
    r = await api.get("/api/v1/vehicles/search",
                      params={"plate": "GJ07ZZ7777"}, headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "dept A legitimately saw this vehicle and should find it"
    assert "SEC-B-CAM-001" not in str(items), (
        f"LEAK: search row lists dept B cameras: {items}")
