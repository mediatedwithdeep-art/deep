"""Integration tests for the schema, against a real PostgreSQL.

Skipped automatically when no database is reachable, so `pytest` is always
green on a fresh checkout. Run the real thing with:

    TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/sentinel_test pytest tests/ -v
"""
from __future__ import annotations

import datetime as dt
import pytest

pytestmark = pytest.mark.integration


# ── extensions and structure ─────────────────────────────────────────

def test_required_extensions_present(db):
    got = {r[0] for r in db.execute(
        "SELECT extname FROM pg_extension").fetchall()}
    assert {"postgis", "vector", "pg_trgm"} <= got


def test_all_expected_tables_exist(db):
    got = {r[0] for r in db.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
    required = {
        "camera", "stream", "detection", "vehicle", "vehicle_sighting",
        "plate_read", "alert", "location", "event", "app_user", "audit_log",
        "department", "watchlist", "alert_rule", "track_link",
        "camera_health", "evidence_export", "camera_adjacency",
    }
    assert required <= got, f"missing: {sorted(required - got)}"


def test_high_volume_tables_are_partitioned(db):
    partitioned = {r[0] for r in db.execute(
        "SELECT c.relname FROM pg_class c WHERE c.relkind='p'").fetchall()}
    # These are the tables that grow without bound. Partitioning is what
    # makes retention a DROP instead of a multi-hour DELETE.
    assert {"detection", "vehicle_sighting", "plate_read", "alert",
            "event", "camera_health", "audit_log"} <= partitioned


def test_no_column_can_hold_a_camera_password(db):
    """Credentials must never reach the database.

    The camera table references a secret by name (`credential_ref`); if a
    `password`/`secret` column ever appears here, a database dump becomes a
    working set of camera credentials for the whole estate.
    """
    cols = {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='camera'").fetchall()}
    for forbidden in ("password", "passwd", "secret", "credentials", "auth_token"):
        assert forbidden not in cols
    assert "credential_ref" in cols


# ── partition lifecycle ──────────────────────────────────────────────

def test_ensure_partitions_is_idempotent(db):
    db.execute("SELECT count(*) FROM ensure_partitions()")
    second = db.execute("SELECT count(*) FROM ensure_partitions()").fetchone()[0]
    assert second == 0, "re-running ensure_partitions() created duplicates"


def test_retention_drops_only_expired_partitions(db):
    db.execute("SELECT count(*) FROM ensure_partitions()")
    db.execute("CREATE TABLE IF NOT EXISTS detection_p20200101 "
               "PARTITION OF detection FOR VALUES FROM ('2020-01-01') TO ('2020-01-02')")
    dropped = {r[0] for r in db.execute("SELECT dropped FROM drop_old_partitions()").fetchall()}
    assert "detection_p20200101" in dropped
    today = dt.date.today().strftime("%Y%m%d")
    assert f"detection_p{today}" not in dropped, "retention dropped a live partition"


def test_partition_pruning_engages(db):
    """A time-filtered query must not scan every partition.

    This is the entire reason for partitioning; if the planner stops pruning
    the design has silently lost its value.
    """
    db.execute("SELECT count(*) FROM ensure_partitions()")
    plan = "\n".join(r[0] for r in db.execute(
        "EXPLAIN SELECT count(*) FROM vehicle_sighting "
        "WHERE timestamp > now() - INTERVAL '1 hour'").fetchall())
    assert "Subplans Removed" in plan or plan.count("Seq Scan") <= 3, plan


# ── geometry ─────────────────────────────────────────────────────────

@pytest.fixture
def seeded_camera(db):
    db.execute("INSERT INTO department (code,name) VALUES ('T_DEPT','Test') "
               "ON CONFLICT (code) DO NOTHING")
    db.execute("""
        INSERT INTO camera (camera_id,name,department_id,protocol,status,
                            latitude,longitude,heading_deg,fov_deg,range_m)
        SELECT 'T-CAM-1','Test Cam',d.id,'RTSP','ONLINE',23.02705,72.51192,47,82,65
        FROM department d WHERE d.code='T_DEPT'
        ON CONFLICT (camera_id) DO NOTHING""")
    return db.execute("SELECT id FROM camera WHERE camera_id='T-CAM-1'").fetchone()[0]


def test_geometry_is_derived_from_lat_lon(db, seeded_camera):
    lon, lat = db.execute(
        "SELECT ST_X(geom::geometry), ST_Y(geom::geometry) FROM camera "
        "WHERE camera_id='T-CAM-1'").fetchone()
    assert abs(lon - 72.51192) < 1e-6 and abs(lat - 23.02705) < 1e-6


def test_fov_wedge_is_valid_and_correctly_oriented(db, seeded_camera):
    valid, ahead, behind, area = db.execute("""
        SELECT ST_IsValid(fov_geom::geometry),
               ST_Covers(fov_geom, ST_Project(geom, 40, radians(47))),
               ST_Covers(fov_geom, ST_Project(geom, 40, radians(227))),
               ST_Area(fov_geom)
        FROM camera WHERE camera_id='T-CAM-1'""").fetchone()
    assert valid, "self-intersecting FOV polygon"
    assert ahead, "camera cannot see along its own heading"
    assert not behind, "camera can see behind itself"
    # A 82-degree wedge of radius 65 m is pi*r^2*(82/360) ~= 3023 m^2.
    expected = 3.14159 * 65 * 65 * (82 / 360)
    assert abs(area - expected) / expected < 0.05, f"{area} vs {expected}"


def test_moving_a_camera_updates_its_geometry(db, seeded_camera):
    db.execute("UPDATE camera SET latitude=23.05, longitude=72.55 WHERE camera_id='T-CAM-1'")
    lon, lat = db.execute(
        "SELECT ST_X(geom::geometry), ST_Y(geom::geometry) FROM camera "
        "WHERE camera_id='T-CAM-1'").fetchone()
    assert abs(lat - 23.05) < 1e-6 and abs(lon - 72.55) < 1e-6


# ── plate helpers (SQL side; parity with Python is tested separately) ──

@pytest.mark.parametrize("plate,valid", [
    ("GJ01AB1234", True), ("MH12DE1433", True), ("22BH1234AA", True),
    ("GJ 01 AB 1234", True), ("XX01AB1234", False), ("GJ01AB", False), ("", False),
])
def test_plate_grammar(db, plate, valid):
    assert db.execute("SELECT plate_valid_in(%s)", (plate,)).fetchone()[0] is valid


def test_plate_canon_collapses_ocr_confusions(db):
    a, b = db.execute(
        "SELECT plate_canon('GJ01AB1234'), plate_canon('GJ0IAB1Z34')").fetchone()
    assert a == b, "O/0 and Z/2 confusions must canonicalise together"


def test_plate_state_code_extraction(db):
    assert db.execute("SELECT plate_state_code('MH12DE1433')").fetchone()[0] == "MH"
    assert db.execute("SELECT plate_state_code('XX99ZZ0000')").fetchone()[0] is None


# ── spatio-temporal gate ─────────────────────────────────────────────

@pytest.fixture
def two_linked_cameras(db):
    db.execute("INSERT INTO department (code,name) VALUES ('G_DEPT','Gate') "
               "ON CONFLICT (code) DO NOTHING")
    for ref, lat, lon in [("G-CAM-A", 23.0270, 72.5119), ("G-CAM-B", 23.0331, 72.5189)]:
        db.execute("""INSERT INTO camera (camera_id,name,department_id,protocol,status,
                        latitude,longitude,heading_deg)
                      SELECT %s,%s,d.id,'RTSP','ONLINE',%s,%s,90
                      FROM department d WHERE d.code='G_DEPT'
                      ON CONFLICT (camera_id) DO NOTHING""", (ref, ref, lat, lon))
    a = db.execute("SELECT id FROM camera WHERE camera_id='G-CAM-A'").fetchone()[0]
    b = db.execute("SELECT id FROM camera WHERE camera_id='G-CAM-B'").fetchone()[0]
    db.execute("INSERT INTO camera_adjacency (from_camera,to_camera,road_dist_m,travel_s) "
               "VALUES (%s,%s,950,120) ON CONFLICT DO NOTHING", (a, b))
    return a, b


def test_gate_returns_reachable_camera_with_a_window(db, two_linked_cameras):
    a, _ = two_linked_cameras
    rows = db.execute(
        "SELECT camera_ref, travel_s, window_start, expected_at, window_end, source "
        "FROM candidate_cameras(%s, now())", (a,)).fetchall()
    assert any(r[0] == "G-CAM-B" for r in rows)
    row = next(r for r in rows if r[0] == "G-CAM-B")
    _, travel, w_start, expected, w_end, source = row
    assert travel == pytest.approx(120, abs=1)
    assert w_start < expected < w_end, "window must bracket the expected arrival"
    assert source == "routed_prior"
    # Fast bound is travel/1.6 = 75 s, so the early margin is 120-75 = 45 s.
    # Slow bound is travel/0.35 + 120 dwell = 463 s, a 343 s late margin.
    # The asymmetry is deliberate: a vehicle can be delayed far more easily
    # than it can arrive early, so the window is skewed late.
    assert (expected - w_start).total_seconds() == pytest.approx(45, abs=2)
    assert (w_end - expected).total_seconds() == pytest.approx(343, abs=5)


def test_low_clock_confidence_widens_the_gate(db, two_linked_cameras):
    """A drifting legacy DVR must widen the window, never narrow it.

    Missing a real transition is far more damaging than surfacing one extra
    candidate for the operator to dismiss.
    """
    a, _ = two_linked_cameras
    wide = db.execute("SELECT window_end - window_start FROM candidate_cameras(%s, now(), 900, 0.2)",
                      (a,)).fetchone()[0]
    tight = db.execute("SELECT window_end - window_start FROM candidate_cameras(%s, now(), 900, 1.0)",
                       (a,)).fetchone()[0]
    assert wide > tight


def test_st_feasibility_peaks_at_expected_and_is_zero_outside(db, two_linked_cameras):
    a, _ = two_linked_cameras
    peak, early, late, mid = db.execute("""
        WITH g AS (SELECT * FROM candidate_cameras(%s, now()) LIMIT 1)
        SELECT st_feasibility(g.expected_at,g.window_start,g.window_end,g.expected_at),
               st_feasibility(g.expected_at,g.window_start,g.window_end,g.window_start - INTERVAL '1 s'),
               st_feasibility(g.expected_at,g.window_start,g.window_end,g.window_end + INTERVAL '1 s'),
               st_feasibility(g.expected_at,g.window_start,g.window_end,g.expected_at + INTERVAL '60 s')
        FROM g""", (a,)).fetchone()
    assert peak == pytest.approx(1.0, abs=1e-4)
    assert early == 0.0 and late == 0.0
    assert 0.0 < mid < 1.0


# ── views ────────────────────────────────────────────────────────────

def test_dashboard_stats_view_returns_one_row_of_counters(db):
    row = db.execute("SELECT * FROM v_dashboard_stats").fetchone()
    assert row is not None
    assert all(isinstance(v, int) for v in row), "counters must be integers"


def test_default_alert_rules_are_seeded(db):
    codes = {r[0] for r in db.execute("SELECT code FROM alert_rule").fetchall()}
    assert {"WATCHLIST_PLATE", "MULTI_CAMERA_TRACK", "RESTRICTED_ZONE",
            "CAMERA_DOWN", "CAMERA_FROZEN"} <= codes


def test_watchlist_requires_something_to_match_on(db):
    """A watchlist entry with neither a plate nor an embedding matches
    everything, which would flood the operator. The constraint blocks it."""
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO watchlist (label, reason) VALUES ('empty','test')")


# ── tiered storage ───────────────────────────────────────────────────

def test_every_partitioned_table_has_an_ordered_tier_policy(db):
    """HOT <= WARM <= COLD <= retention, for every table without exception.

    An out-of-order policy is not a cosmetic defect: if cold_days exceeds
    retention_days the drop runs before the archival detach, and the data is
    destroyed before anything can export it. The database enforces the
    ordering so no operator can configure that state by hand.
    """
    rows = db.execute(
        "SELECT table_name, hot_days, warm_days, cold_days, retention_days "
        "FROM partitioned_table_config").fetchall()
    assert rows, "no partitioned tables are configured"
    for name, hot, warm, cold, retention in rows:
        assert 1 <= hot <= warm <= cold <= retention, (
            f"{name} has an out-of-order tier policy: "
            f"hot={hot} warm={warm} cold={cold} retention={retention}")


def test_the_tier_policy_cannot_be_configured_out_of_order(db):
    """The CHECK is the control. Without it the ordering above is a
    convention that holds until the first operator edits a row."""
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE partitioned_table_config "
                   "SET cold_days = retention_days + 1 WHERE table_name='event'")


@pytest.mark.parametrize("age_days,expected", [
    (1, "HOT"), (10, "WARM"), (20, "COLD"), (99, "EXPIRED")])
def test_a_partition_moves_through_the_tiers_as_it_ages(db, age_days, expected):
    """vehicle_sighting is configured 7 / 15 / 30, which is the 7- and
    15-day window the brief asks to be configurable. It is a row in a
    config table now, not a constant inside a migration."""
    tier = db.execute(
        "SELECT partition_tier('vehicle_sighting', (now() - make_interval(days => %s))::date)",
        (age_days,)).fetchone()[0]
    assert tier == expected


def test_retention_windows_are_configurable_without_a_migration(db):
    """The point of the config table. An estate that must keep 15 days of
    sightings instead of 7 changes one row; nothing is rebuilt or
    redeployed, and the change takes effect on the next tier evaluation."""
    db.execute("UPDATE partitioned_table_config "
               "SET hot_days = 15, warm_days = 21 WHERE table_name='vehicle_sighting'")
    try:
        tier = db.execute(
            "SELECT partition_tier('vehicle_sighting', (now() - interval '10 day')::date)"
        ).fetchone()[0]
        assert tier == "HOT", "a widened hot window did not take effect"
    finally:
        db.execute("UPDATE partitioned_table_config "
                   "SET hot_days = 7, warm_days = 15 WHERE table_name='vehicle_sighting'")


def test_the_tier_report_accounts_for_every_live_partition(db):
    """An operator has to be able to answer 'what is on disk, and how much
    of it is hot' without reading pg_class by hand."""
    db.execute("SELECT count(*) FROM ensure_partitions()")
    rows = db.execute("SELECT table_name, partition_name, tier, bytes "
                      "FROM storage_tier_report()").fetchall()
    assert rows, "the report found no partitions at all"
    assert all(tier in ("HOT", "WARM", "COLD", "EXPIRED") for _, _, tier, _ in rows)
    assert all(nbytes >= 0 for _, _, _, nbytes in rows)
    # Every reported partition must belong to a configured table.
    configured = {r[0] for r in db.execute(
        "SELECT table_name FROM partitioned_table_config").fetchall()}
    assert {r[0] for r in rows} <= configured


def test_cold_detach_removes_a_partition_from_the_query_path_without_dropping_it(db):
    """DETACH, not DROP.

    BSA s63 evidence and the DPDP audit trail both outlive the operational
    window, so the archival step must leave the rows producible. A cold
    boundary that deleted data would make retention policy quietly destroy
    material the law requires be recoverable.
    """
    db.execute("""CREATE TABLE IF NOT EXISTS tier_demo (
                      id BIGSERIAL, ts TIMESTAMPTZ NOT NULL
                  ) PARTITION BY RANGE (ts)""")
    db.execute("""CREATE TABLE IF NOT EXISTS tier_demo_old
                  PARTITION OF tier_demo
                  FOR VALUES FROM ('2020-01-01') TO ('2020-01-02')""")
    db.execute("INSERT INTO partitioned_table_config "
               "(table_name, interval_kind, retention_days, lookahead_units, "
               " hot_days, warm_days, cold_days) "
               "VALUES ('tier_demo','day',3650,1,1,2,3) "
               "ON CONFLICT (table_name) DO NOTHING")
    db.execute("INSERT INTO tier_demo (ts) VALUES ('2020-01-01T06:00:00Z')")
    try:
        assert db.execute("SELECT count(*) FROM tier_demo").fetchone()[0] == 1

        detached = [r[0] for r in
                    db.execute("SELECT detached FROM detach_cold_partitions()").fetchall()]
        assert "tier_demo_old" in detached

        # Gone from the parent's query path ...
        assert db.execute("SELECT count(*) FROM tier_demo").fetchone()[0] == 0
        # ... but the rows still exist and can be exported.
        assert db.execute("SELECT count(*) FROM tier_demo_old").fetchone()[0] == 1
    finally:
        db.execute("DROP TABLE IF EXISTS tier_demo_old")
        db.execute("DROP TABLE IF EXISTS tier_demo")
        db.execute("DELETE FROM partitioned_table_config WHERE table_name='tier_demo'")
