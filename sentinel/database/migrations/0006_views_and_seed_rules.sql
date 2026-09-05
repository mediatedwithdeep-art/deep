-- Read-optimised views for the dashboard, plus default alert rules.
--
-- The command centre polls a handful of headline numbers every few seconds.
-- Computing them from raw tables on every poll is how a dashboard takes a
-- database down. These views keep the query shape fixed and index-friendly.

-- ── Camera estate health ─────────────────────────────────────────────
CREATE OR REPLACE VIEW v_camera_status AS
SELECT
    c.id,
    c.camera_id,
    c.name,
    c.status,
    c.protocol,
    c.role,
    c.zone,
    c.district,
    d.code AS department,
    c.latitude,
    c.longitude,
    c.heading_deg,
    c.fps,
    c.width || 'x' || c.height AS resolution,
    c.anpr_capable,
    c.trust_score,
    c.last_seen,
    c.consecutive_failures,
    EXTRACT(EPOCH FROM (now() - c.last_seen))::int AS seconds_since_seen,
    -- A camera is "stale" when it last reported more than 90 s ago. That is
    -- three missed 30 s beacons: long enough to survive a blip, short
    -- enough that an operator finds out during the incident, not after.
    (c.last_seen IS NULL OR c.last_seen < now() - INTERVAL '90 seconds') AS is_stale
FROM camera c
JOIN department d ON d.id = c.department_id
WHERE c.status <> 'DISABLED';

-- ── Command-centre headline counters ─────────────────────────────────
CREATE OR REPLACE VIEW v_dashboard_stats AS
SELECT
    (SELECT count(*) FROM camera WHERE status = 'ONLINE')                       AS cameras_online,
    (SELECT count(*) FROM camera WHERE status IN ('OFFLINE','DEGRADED'))        AS cameras_offline,
    (SELECT count(*) FROM camera WHERE status <> 'DISABLED')                    AS cameras_total,
    (SELECT count(*) FROM alert
      WHERE state = 'NEW' AND timestamp > now() - INTERVAL '24 hours')          AS active_alerts,
    (SELECT count(*) FROM alert
      WHERE severity IN ('HIGH','CRITICAL') AND state = 'NEW'
        AND timestamp > now() - INTERVAL '24 hours')                            AS critical_alerts,
    (SELECT count(*) FROM vehicle
      WHERE last_seen > now() - INTERVAL '1 hour')                              AS vehicles_tracked_1h,
    (SELECT count(*) FROM vehicle
      WHERE last_seen > now() - INTERVAL '24 hours')                            AS vehicles_tracked_24h,
    (SELECT count(*) FROM plate_read
      WHERE timestamp > now() - INTERVAL '1 hour')                              AS anpr_events_1h,
    (SELECT count(*) FROM plate_read
      WHERE timestamp > now() - INTERVAL '24 hours')                            AS anpr_events_24h,
    (SELECT count(*) FROM vehicle_sighting
      WHERE timestamp > now() - INTERVAL '1 hour')                              AS sightings_1h,
    (SELECT count(*) FROM watchlist WHERE is_active)                            AS watchlist_active,
    (SELECT count(*) FROM vehicle WHERE camera_count >= 2
       AND last_seen > now() - INTERVAL '24 hours')                             AS cross_camera_tracks_24h;

-- ── Vehicles seen at more than one camera: the cross-camera story ────
CREATE OR REPLACE VIEW v_cross_camera_vehicles AS
SELECT
    v.vehicle_track_id,
    v.vehicle_type,
    v.vehicle_color,
    v.best_plate,
    v.best_plate_conf,
    v.camera_count,
    v.sighting_count,
    v.first_seen,
    v.last_seen,
    EXTRACT(EPOCH FROM (v.last_seen - v.first_seen))::int AS duration_seconds,
    v.total_distance_m,
    v.is_watchlisted,
    (SELECT array_agg(DISTINCT vs.camera_ref ORDER BY vs.camera_ref)
       FROM vehicle_sighting vs
      WHERE vs.vehicle_track_id = v.vehicle_track_id)     AS cameras
FROM vehicle v
WHERE v.camera_count >= 2;

-- ── Per-camera activity, for the analytics page ──────────────────────
CREATE OR REPLACE VIEW v_camera_activity_1h AS
SELECT
    c.camera_id,
    c.name,
    c.zone,
    count(vs.*)                                          AS sightings,
    count(vs.plate_normalized)                           AS plate_reads,
    count(DISTINCT vs.vehicle_track_id)                  AS unique_vehicles,
    avg(vs.quality_score)::real                          AS avg_quality,
    max(vs.timestamp)                                    AS last_activity
FROM camera c
LEFT JOIN vehicle_sighting vs
       ON vs.camera_id = c.id AND vs.timestamp > now() - INTERVAL '1 hour'
WHERE c.status <> 'DISABLED'
GROUP BY c.camera_id, c.name, c.zone;

-- ── Latest health beacon per camera ──────────────────────────────────
CREATE OR REPLACE VIEW v_camera_health_latest AS
SELECT DISTINCT ON (camera_id)
    camera_id, camera_ref, timestamp, reachable, fps_actual,
    decode_errors, scene_change, mean_luma, blur_variance,
    latency_ms, inference_ms, queue_depth, message
FROM camera_health
WHERE timestamp > now() - INTERVAL '1 hour'
ORDER BY camera_id, timestamp DESC;

-- ─────────────────────────────────────────────────────────────────────
-- Default alert rules. Editable at runtime through the API: adding a rule
-- must never require a deployment.
-- ─────────────────────────────────────────────────────────────────────

INSERT INTO alert_rule (code, name, alert_type, severity, params, dedup_seconds, description) VALUES
('WATCHLIST_PLATE', 'Watchlist plate detected', 'WATCHLIST_HIT', 'CRITICAL',
 '{"min_confidence": 0.55, "match_bands": ["exact","confident","probable"]}', 60,
 'An ANPR read matches an active watchlist entry, allowing for OCR confusion (O/0, I/1, 8/B).'),

('ANPR_ANY', 'ANPR read recorded', 'ANPR_MATCH', 'INFO',
 '{"min_confidence": 0.75, "require_valid_format": true}', 300,
 'Any high-confidence, grammatically valid plate read. Informational; feeds the ANPR counter.'),

('MULTI_CAMERA_TRACK', 'Vehicle seen across multiple cameras', 'MULTI_CAMERA', 'MEDIUM',
 '{"min_cameras": 3, "window_minutes": 15}', 120,
 'The same vehicle confirmed at 3+ cameras within 15 minutes. Evidence of a tracked movement, not a coincidence.'),

('RESTRICTED_ZONE', 'Vehicle in restricted zone', 'RESTRICTED_ZONE', 'HIGH',
 '{"location_kinds": ["RESTRICTED"], "vehicle_types": []}', 60,
 'A vehicle sighted inside a geofenced restricted area. Empty vehicle_types means all types.'),

('CAMERA_DOWN', 'Camera offline', 'CAMERA_OFFLINE', 'MEDIUM',
 '{"stale_seconds": 90, "min_consecutive_failures": 3}', 600,
 'A camera has stopped reporting. Three missed beacons, so a single blip does not page anyone.'),

('CAMERA_FROZEN', 'Camera picture frozen', 'CAMERA_TAMPER', 'HIGH',
 '{"scene_change_below": 0.002, "min_samples": 5}', 600,
 'The stream is healthy but the picture is not changing. The most common silent camera failure; nothing else detects it.'),

('LOITERING', 'Repeated sightings at one camera', 'SUSPICIOUS_PATTERN', 'MEDIUM',
 '{"min_sightings": 4, "window_minutes": 20, "same_camera": true}', 300,
 'The same vehicle passes one camera repeatedly in a short window -- circling, casing, or waiting.'),

('IMPOSSIBLE_SPEED', 'Implausible travel time between cameras', 'SUSPICIOUS_PATTERN', 'LOW',
 '{"max_kmph": 160}', 300,
 'Implied speed between two sightings exceeds what is physically plausible. Usually a mis-association or a clock-drift problem, so it is a data-quality signal as much as a security one.')
ON CONFLICT (code) DO NOTHING;
