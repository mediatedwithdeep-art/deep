"""Tests for cross-camera matching and the alert engine.

Integration tests against a live PostgreSQL; skipped when none is reachable.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from sentinel_core.domain import (
    Alert, AlertType, CameraHealth, MatchDecision, PlateRead, Severity,
    Sighting, VehicleType,
)
from sentinel_core.bus import create_bus
from processor.store import Store
from processor.matcher import CrossCameraMatcher
from processor.alerts import AlertEngine
from processor.pipeline import EventProcessor

pytestmark = pytest.mark.integration

NOW = datetime.now(timezone.utc)


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def estate(db, pg_dsn):
    """Two cameras 950 m apart with a 120 s road link between them."""
    db.execute("INSERT INTO department (code,name) VALUES ('EP','EventProc') "
               "ON CONFLICT (code) DO NOTHING")
    for ref, lat, lon, anpr in [("EP-CAM-A", 23.0270, 72.5119, True),
                                ("EP-CAM-B", 23.0331, 72.5189, True),
                                ("EP-CAM-Z", 23.4000, 72.9000, False)]:
        db.execute("""INSERT INTO camera (camera_id,name,department_id,protocol,status,
                        latitude,longitude,heading_deg,anpr_capable)
                      SELECT %s,%s,d.id,'RTSP','ONLINE',%s,%s,90,%s
                      FROM department d WHERE d.code='EP'
                      ON CONFLICT (camera_id) DO NOTHING""", (ref, ref, lat, lon, anpr))
    a = db.execute("SELECT id FROM camera WHERE camera_id='EP-CAM-A'").fetchone()[0]
    b = db.execute("SELECT id FROM camera WHERE camera_id='EP-CAM-B'").fetchone()[0]
    db.execute("INSERT INTO camera_adjacency (from_camera,to_camera,road_dist_m,travel_s) "
               "VALUES (%s,%s,950,120) ON CONFLICT DO NOTHING", (a, b))
    db.execute("INSERT INTO camera_adjacency (from_camera,to_camera,road_dist_m,travel_s) "
               "VALUES (%s,%s,950,120) ON CONFLICT DO NOTHING", (b, a))
    db.execute("SELECT count(*) FROM ensure_partitions()")

    store = Store(pg_dsn)
    store.connect()
    yield store
    store.close()


def _emb(seed: int, dim: int = 512) -> list[float]:
    import hashlib, math, random
    rng = random.Random(int(hashlib.sha256(str(seed).encode()).hexdigest()[:12], 16))
    v = [rng.gauss(0, 1) for _ in range(dim)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _sighting(camera_ref: str, *, ts: datetime, plate: str | None = None,
              embedding=None, colour="white", vtype=VehicleType.CAR,
              lat=23.0270, lon=72.5119, conf=0.95) -> Sighting:
    return Sighting(
        sighting_id=f"S-{uuid.uuid4().hex[:10]}",
        vehicle_track_id="", camera_id=camera_ref,
        track_id=f"{camera_ref}:T-1",
        first_seen=ts, last_seen=ts + timedelta(seconds=3), timestamp=ts,
        vehicle_type=vtype, vehicle_color=colour, color_confidence=0.9,
        plate=(PlateRead(raw_plate=plate, normalized_plate=plate, confidence=conf,
                         valid_format=True, plate_width_px=140) if plate else None),
        embedding=embedding, embedding_model="test-512" if embedding else None,
        latitude=lat, longitude=lon, detection_count=8, best_quality=0.9)


# ── matcher ──────────────────────────────────────────────────────────

def test_first_sighting_creates_a_new_vehicle(estate):
    m = CrossCameraMatcher(estate)
    out = m.process_batch([_sighting("EP-CAM-A", ts=NOW)])
    assert len(out) == 1 and out[0].is_new
    assert out[0].vehicle_track_id.startswith("V-")


def test_plate_match_links_across_cameras_without_a_gate(estate, db):
    """A canonical plate match is identity evidence and must NOT require the
    travel-time window. A vehicle can legitimately vanish and reappear
    across the city; refusing that link would lose exactly the long-range
    associations an investigation depends on."""
    proc = EventProcessor(estate, create_bus("memory"))
    first = _sighting("EP-CAM-A", ts=NOW - timedelta(minutes=40), plate="GJ01AB1234")
    out, _ = proc.process_sightings([first])
    vtid = out[0].vehicle_track_id

    # 40 minutes later at a camera far outside any travel window.
    second = _sighting("EP-CAM-Z", ts=NOW, plate="GJ01AB1234",
                       lat=23.4000, lon=72.9000)
    out2, _ = proc.process_sightings([second])
    assert out2[0].vehicle_track_id == vtid
    assert not out2[0].is_new
    assert out2[0].link.decision is MatchDecision.AUTO


def test_ocr_confused_plate_still_matches_the_same_vehicle(estate):
    """O/0 and Z/2 are systematic OCR confusions. Exact string comparison
    would treat this as a different vehicle and break the track."""
    proc = EventProcessor(estate, create_bus("memory"))
    out, _ = proc.process_sightings([
        _sighting("EP-CAM-A", ts=NOW - timedelta(minutes=2), plate="GJ01AB1234")])
    vtid = out[0].vehicle_track_id
    out2, _ = proc.process_sightings([
        _sighting("EP-CAM-B", ts=NOW, plate="GJO1AB1Z34", lat=23.0331, lon=72.5189)])
    assert out2[0].vehicle_track_id == vtid


def test_appearance_match_inside_the_gate_is_probable_never_auto(estate):
    """Appearance alone must surface a candidate for confirmation, never
    assert identity. A white hatchback resembles every other white
    hatchback, and an auto-confirm the operator did not sanction is how a
    system loses their trust."""
    proc = EventProcessor(estate, create_bus("memory"))
    e = _emb(1)
    out, _ = proc.process_sightings([
        _sighting("EP-CAM-A", ts=NOW - timedelta(seconds=130), embedding=e)])
    vtid = out[0].vehicle_track_id
    out2, _ = proc.process_sightings([
        _sighting("EP-CAM-B", ts=NOW, embedding=e, lat=23.0331, lon=72.5189)])
    assert out2[0].vehicle_track_id == vtid
    assert out2[0].link.decision is MatchDecision.PROBABLE
    assert out2[0].link.score_reid > 0.5


def test_arrival_outside_the_travel_window_is_not_matched(estate):
    """The gate is the component that makes appearance matching usable. A
    vehicle cannot cover a 120 s road link in 5 s."""
    proc = EventProcessor(estate, create_bus("memory"))
    e = _emb(2)
    out, _ = proc.process_sightings([
        _sighting("EP-CAM-A", ts=NOW - timedelta(seconds=5), embedding=e)])
    vtid = out[0].vehicle_track_id
    out2, _ = proc.process_sightings([
        _sighting("EP-CAM-B", ts=NOW, embedding=e, lat=23.0331, lon=72.5189)])
    assert out2[0].vehicle_track_id != vtid
    assert out2[0].is_new


def test_a_genuinely_different_vehicle_is_not_matched(estate):
    proc = EventProcessor(estate, create_bus("memory"))
    out, _ = proc.process_sightings([
        _sighting("EP-CAM-A", ts=NOW - timedelta(seconds=130),
                  embedding=_emb(10), colour="white")])
    vtid = out[0].vehicle_track_id
    out2, _ = proc.process_sightings([
        _sighting("EP-CAM-B", ts=NOW, embedding=_emb(99), colour="red",
                  lat=23.0331, lon=72.5189)])
    assert out2[0].vehicle_track_id != vtid


def test_every_association_records_why(estate, db):
    """An operator must be able to ask why the system believes two
    sightings are the same vehicle, and get the score breakdown."""
    proc = EventProcessor(estate, create_bus("memory"))
    e = _emb(3)
    proc.process_sightings([_sighting("EP-CAM-A", ts=NOW - timedelta(seconds=130), embedding=e)])
    proc.process_sightings([_sighting("EP-CAM-B", ts=NOW, embedding=e,
                                      lat=23.0331, lon=72.5189)])
    row = db.execute("""SELECT decision, score_total, score_reid, score_spatiotemporal,
                               travel_expected_s, travel_actual_s, reasons
                        FROM track_link ORDER BY created_at DESC LIMIT 1""").fetchone()
    assert row is not None
    decision, total, reid, st, expected, actual, reasons = row
    assert 0 < total <= 1 and reid > 0 and st > 0
    assert expected == pytest.approx(120, abs=1)
    assert actual is not None and reasons


def test_camera_count_counts_distinct_cameras_only(estate, db):
    """camera_count >= 2 is the signal the whole cross-camera story rests
    on, so re-entering the same camera must not inflate it."""
    proc = EventProcessor(estate, create_bus("memory"))
    out, _ = proc.process_sightings([
        _sighting("EP-CAM-A", ts=NOW - timedelta(minutes=5), plate="GJ09XY4321")])
    vtid = out[0].vehicle_track_id
    for offset in (4, 3):
        proc.process_sightings([
            _sighting("EP-CAM-A", ts=NOW - timedelta(minutes=offset), plate="GJ09XY4321")])
    n = db.execute("SELECT camera_count FROM vehicle WHERE vehicle_track_id=%s",
                   (vtid,)).fetchone()[0]
    assert n == 1


def test_trajectory_is_rebuilt_as_a_geometry(estate, db):
    proc = EventProcessor(estate, create_bus("memory"))
    proc.process_sightings([_sighting("EP-CAM-A", ts=NOW - timedelta(minutes=3),
                                      plate="GJ11PQ8877")])
    proc.process_sightings([_sighting("EP-CAM-B", ts=NOW, plate="GJ11PQ8877",
                                      lat=23.0331, lon=72.5189)])
    row = db.execute("""SELECT ST_NPoints(path), total_distance_m FROM vehicle
                        WHERE vehicle_track_id IN
                          (SELECT vehicle_track_id FROM vehicle WHERE best_plate='GJ11PQ8877')
                     """).fetchone()
    assert row and row[0] >= 2
    assert row[1] > 500


def test_best_plate_keeps_the_strongest_read(estate, db):
    """A clean daylight read must not be overwritten by a poor night one at
    the next camera."""
    proc = EventProcessor(estate, create_bus("memory"))
    proc.process_sightings([_sighting("EP-CAM-A", ts=NOW - timedelta(minutes=2),
                                      plate="GJ22MN3311", conf=0.97)])
    proc.process_sightings([_sighting("EP-CAM-B", ts=NOW, plate="GJ22MN3311",
                                      conf=0.55, lat=23.0331, lon=72.5189)])
    conf = db.execute("SELECT best_plate_conf FROM vehicle WHERE best_plate='GJ22MN3311'"
                      ).fetchone()[0]
    assert conf == pytest.approx(0.97, abs=0.01)


# ── alert engine ─────────────────────────────────────────────────────

@pytest.fixture
def watchlisted(db):
    # Reset rather than ON CONFLICT: watchlist has no uniqueness constraint
    # on plate, deliberately, because two different FIRs can legitimately
    # list the same vehicle and both cases need to be alerted. That means a
    # careless fixture accumulates duplicates across tests.
    db.execute("DELETE FROM watchlist WHERE plate_query='GJ01AB1234'")
    db.execute("""INSERT INTO watchlist (label, plate_query, plate_canonical,
                       severity, reason, case_ref)
                  VALUES ('Suspect vehicle','GJ01AB1234',plate_canon('GJ01AB1234'),
                          'CRITICAL','test','FIR/0142/2026')""")
    return "GJ01AB1234"


def test_two_cases_on_one_vehicle_both_raise_an_alert(estate, db):
    """Two FIRs can list the same vehicle. Each case must be told, so this
    is one alert per matching watchlist entry, not one per sighting."""
    db.execute("DELETE FROM watchlist WHERE plate_query='GJ77DUP1111'")
    for case in ("FIR/0001/2026", "FIR/0002/2026"):
        db.execute("""INSERT INTO watchlist (label, plate_query, plate_canonical,
                           severity, reason, case_ref)
                      VALUES (%s,'GJ77DUP1111',plate_canon('GJ77DUP1111'),
                              'HIGH','test',%s)""", (f"Case {case}", case))
    proc = EventProcessor(estate, create_bus("memory"))
    proc.alerts.refresh(force=True)
    _o, alerts = proc.process_sightings([
        _sighting("EP-CAM-A", ts=NOW, plate="GJ77DUP1111")])
    hits = [a for a in alerts if a.alert_type is AlertType.WATCHLIST_HIT]
    assert len(hits) == 2
    assert {h.evidence["case_ref"] for h in hits} == {"FIR/0001/2026", "FIR/0002/2026"}


def test_watchlist_hit_raises_a_critical_alert(estate, watchlisted):
    proc = EventProcessor(estate, create_bus("memory"))
    _o, alerts = proc.process_sightings([
        _sighting("EP-CAM-A", ts=NOW, plate="GJ01AB1234")])
    hits = [a for a in alerts if a.alert_type is AlertType.WATCHLIST_HIT]
    assert hits
    a = hits[0]
    assert a.severity is Severity.CRITICAL
    assert a.evidence["certain"] is True
    assert a.evidence["case_ref"] == "FIR/0142/2026"


def test_fuzzy_watchlist_hit_is_labelled_probable_not_certain(estate, watchlisted):
    """Overstating certainty is how the wrong car gets stopped. A fuzzy
    match must say so in the title and carry its distance."""
    proc = EventProcessor(estate, create_bus("memory"))
    _o, alerts = proc.process_sightings([
        _sighting("EP-CAM-A", ts=NOW, plate="GJ01AB1284")])   # 3 -> 8 confusion
    hits = [a for a in alerts if a.alert_type is AlertType.WATCHLIST_HIT]
    assert hits
    a = hits[0]
    assert "Probable" in a.title
    assert a.evidence["certain"] is False
    assert a.evidence["plate_distance"] > 0
    assert "Verify before acting" in a.message


def test_alerts_are_deduplicated(estate, watchlisted):
    """A stationary vehicle at a watchlisted junction would otherwise
    produce an alert per sighting and the operator mutes the system."""
    proc = EventProcessor(estate, create_bus("memory"))
    total = 0
    for i in range(6):
        _o, alerts = proc.process_sightings([
            _sighting("EP-CAM-A", ts=NOW + timedelta(seconds=i), plate="GJ01AB1234")])
        total += len([a for a in alerts if a.alert_type is AlertType.WATCHLIST_HIT])
    assert total == 1
    assert proc.alerts.stats.suppressed > 0


def test_multi_camera_alert_needs_genuinely_distinct_cameras(estate, db):
    db.execute("UPDATE alert_rule SET params='{\"min_cameras\":2,\"window_minutes\":15}' "
               "WHERE code='MULTI_CAMERA_TRACK'")
    proc = EventProcessor(estate, create_bus("memory"))
    proc.alerts.refresh(force=True)
    proc.process_sightings([_sighting("EP-CAM-A", ts=NOW - timedelta(minutes=2),
                                      plate="GJ44ZZ1000")])
    _o, alerts = proc.process_sightings([_sighting("EP-CAM-B", ts=NOW, plate="GJ44ZZ1000",
                                                   lat=23.0331, lon=72.5189)])
    multi = [a for a in alerts if a.alert_type is AlertType.MULTI_CAMERA]
    assert multi
    assert multi[0].evidence["camera_count"] >= 2


def test_disabled_rule_produces_no_alerts(estate, watchlisted, db):
    """Rules are runtime configuration. Turning one off must take effect
    without a deployment."""
    db.execute("UPDATE alert_rule SET is_enabled=false WHERE code='WATCHLIST_PLATE'")
    proc = EventProcessor(estate, create_bus("memory"))
    proc.alerts.refresh(force=True)
    _o, alerts = proc.process_sightings([
        _sighting("EP-CAM-A", ts=NOW, plate="GJ01AB1234")])
    assert not [a for a in alerts if a.alert_type is AlertType.WATCHLIST_HIT]
    db.execute("UPDATE alert_rule SET is_enabled=true WHERE code='WATCHLIST_PLATE'")


def test_restricted_zone_alert_fires_inside_a_geofence(estate, db):
    db.execute("""INSERT INTO location (code,name,kind,restricted,geom)
                  VALUES ('EP_RESTRICTED','Test Restricted','RESTRICTED',TRUE,
                    ST_SetSRID(ST_MakePolygon(ST_GeomFromText(
                      'LINESTRING(72.5100 23.0260, 72.5140 23.0260, 72.5140 23.0290,
                                  72.5100 23.0290, 72.5100 23.0260)')),4326)::geography)
                  ON CONFLICT (code) DO NOTHING""")
    proc = EventProcessor(estate, create_bus("memory"))
    _o, alerts = proc.process_sightings([
        _sighting("EP-CAM-A", ts=NOW, lat=23.0270, lon=72.5119, embedding=_emb(77))])
    zone = [a for a in alerts if a.alert_type is AlertType.RESTRICTED_ZONE]
    assert zone and zone[0].evidence["zone_code"] == "EP_RESTRICTED"


# ── health ───────────────────────────────────────────────────────────

def test_health_beacon_updates_camera_status_and_trust(estate, db):
    proc = EventProcessor(estate, create_bus("memory"))
    proc.process_health([CameraHealth(camera_id="EP-CAM-A", reachable=False,
                                      scene_change=0.5, message="stream stalled")])
    row = db.execute("SELECT status::text, consecutive_failures, trust_score "
                     "FROM camera WHERE camera_id='EP-CAM-A'").fetchone()
    assert row[0] == "OFFLINE" and row[1] >= 1
    proc.process_health([CameraHealth(camera_id="EP-CAM-A", reachable=True,
                                      scene_change=0.4, fps_actual=6.0)])
    row2 = db.execute("SELECT status::text, consecutive_failures FROM camera "
                      "WHERE camera_id='EP-CAM-A'").fetchone()
    assert row2[0] == "ONLINE" and row2[1] == 0


def test_frozen_picture_is_detected_even_though_the_stream_is_healthy(estate):
    """The classic silent camera failure: a live socket delivering an
    unchanging image. Every other health signal reports fine."""
    proc = EventProcessor(estate, create_bus("memory"))
    alerts = proc.process_health([
        CameraHealth(camera_id="EP-CAM-B", reachable=True, fps_actual=6.0,
                     scene_change=0.0001, frames_decoded=5000)])
    frozen = [a for a in alerts if a.alert_type is AlertType.CAMERA_TAMPER]
    assert frozen
    assert frozen[0].evidence["scene_change"] < frozen[0].evidence["threshold"]


def test_trust_score_decays_fast_and_recovers_slowly(estate, db):
    """A camera that flaps must not be treated as reliable between flaps."""
    proc = EventProcessor(estate, create_bus("memory"))
    db.execute("UPDATE camera SET trust_score=0.9 WHERE camera_id='EP-CAM-Z'")
    proc.process_health([CameraHealth(camera_id="EP-CAM-Z", reachable=False)])
    after_fail = db.execute("SELECT trust_score FROM camera WHERE camera_id='EP-CAM-Z'"
                            ).fetchone()[0]
    proc.process_health([CameraHealth(camera_id="EP-CAM-Z", reachable=True)])
    after_ok = db.execute("SELECT trust_score FROM camera WHERE camera_id='EP-CAM-Z'"
                          ).fetchone()[0]
    assert after_fail < 0.9 - 0.1
    assert after_ok - after_fail < 0.05


# ── persistence ──────────────────────────────────────────────────────

def test_sightings_and_plate_reads_are_persisted(estate, db):
    proc = EventProcessor(estate, create_bus("memory"))
    proc.process_sightings([_sighting("EP-CAM-A", ts=NOW, plate="GJ55TT9090")])
    assert db.execute("SELECT count(*) FROM vehicle_sighting WHERE plate_normalized='GJ55TT9090'"
                      ).fetchone()[0] == 1
    row = db.execute("SELECT canonical_plate, state_code, valid_format FROM plate_read "
                     "WHERE normalized_plate='GJ55TT9090'").fetchone()
    assert row and row[1] == "GJ" and row[2] is True


def test_sighting_from_an_unknown_camera_is_rejected_not_orphaned(estate):
    """A registry/ingestion mismatch must not write a row the UI cannot
    resolve. Count it as an error and say so."""
    proc = EventProcessor(estate, create_bus("memory"))
    before = proc.stats.errors
    proc.process_sightings([_sighting("NOT-A-REAL-CAMERA", ts=NOW)])
    assert proc.stats.errors > before
