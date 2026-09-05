-- Partition management, spatial gating, plate helpers, retention.

-- ─────────────────────────────────────────────────────────────────────
-- Partition management
--
-- Native range partitioning needs partitions to exist before a row lands
-- in them; an insert with no matching partition fails outright. So we
-- create them ahead of time and keep a rolling window. Call
-- ensure_partitions() from a scheduler (or the API's startup hook) daily.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE partitioned_table_config (
    table_name       TEXT PRIMARY KEY,
    interval_kind    TEXT NOT NULL CHECK (interval_kind IN ('day','month')),
    retention_days   INTEGER NOT NULL,
    lookahead_units  INTEGER NOT NULL DEFAULT 3
);

INSERT INTO partitioned_table_config (table_name, interval_kind, retention_days, lookahead_units) VALUES
    -- Per-frame boxes: huge, and nothing in the UI reads them. Short life.
    ('detection',        'day',   3,   3),
    -- The operational table. 90 days covers a typical investigation window.
    ('vehicle_sighting', 'day',   90,  3),
    ('plate_read',       'day',   365, 3),
    ('alert',            'month', 730, 2),
    ('event',            'day',   30,  3),
    ('camera_health',    'day',   30,  3),
    ('audit_log',        'month', 2555, 2)   -- 7 years: audit outlives everything
ON CONFLICT (table_name) DO NOTHING;

CREATE OR REPLACE FUNCTION ensure_partitions(p_now TIMESTAMPTZ DEFAULT now())
RETURNS TABLE (created TEXT) LANGUAGE plpgsql AS $$
DECLARE
    cfg      RECORD;
    i        INTEGER;
    p_start  DATE;
    p_end    DATE;
    p_name   TEXT;
BEGIN
    FOR cfg IN SELECT * FROM partitioned_table_config LOOP
        FOR i IN -1 .. cfg.lookahead_units LOOP
            IF cfg.interval_kind = 'day' THEN
                p_start := (p_now + make_interval(days => i))::date;
                p_end   := p_start + 1;
                p_name  := format('%s_p%s', cfg.table_name, to_char(p_start, 'YYYYMMDD'));
            ELSE
                p_start := date_trunc('month', p_now + make_interval(months => i))::date;
                p_end   := (p_start + INTERVAL '1 month')::date;
                p_name  := format('%s_p%s', cfg.table_name, to_char(p_start, 'YYYYMM'));
            END IF;

            IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = p_name) THEN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                    p_name, cfg.table_name, p_start, p_end);
                created := p_name;
                RETURN NEXT;
            END IF;
        END LOOP;
    END LOOP;
END $$;

-- Retention by DROP PARTITION: instant, and leaves no bloat behind. A
-- DELETE over the same range would take hours and trigger a vacuum storm.
CREATE OR REPLACE FUNCTION drop_old_partitions(p_now TIMESTAMPTZ DEFAULT now())
RETURNS TABLE (dropped TEXT) LANGUAGE plpgsql AS $$
DECLARE
    cfg      RECORD;
    part     RECORD;
    cutoff   DATE;
    part_end DATE;
BEGIN
    FOR cfg IN SELECT * FROM partitioned_table_config LOOP
        cutoff := (p_now - make_interval(days => cfg.retention_days))::date;
        FOR part IN
            SELECT c.relname,
                   pg_get_expr(c.relpartbound, c.oid) AS bound
            FROM pg_class c
            JOIN pg_inherits inh ON inh.inhrelid = c.oid
            JOIN pg_class parent ON parent.oid = inh.inhparent
            WHERE parent.relname = cfg.table_name
        LOOP
            -- Bound looks like: FOR VALUES FROM ('2026-08-01') TO ('2026-09-01')
            part_end := (regexp_match(part.bound, 'TO \(''([0-9-]+)'))[1]::date;
            IF part_end IS NOT NULL AND part_end <= cutoff THEN
                EXECUTE format('DROP TABLE IF EXISTS %I', part.relname);
                dropped := part.relname;
                RETURN NEXT;
            END IF;
        END LOOP;
    END LOOP;
END $$;

-- ─────────────────────────────────────────────────────────────────────
-- Plate helpers. MUST stay in step with shared/sentinel_core/plate_rules.py
-- (tests/test_sql_parity.py enforces this). If they diverge, SQL prefilters
-- silently drop rows the Python matcher would have matched -- a failure
-- that produces no error, only quietly worse recall.
-- ─────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION plate_canon(p TEXT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT translate(upper(regexp_replace(COALESCE(p,''), '[^A-Za-z0-9]', '', 'g')),
                     'OIBSZGDQU',
                     '018526OO0');
$$;

CREATE OR REPLACE FUNCTION plate_valid_in(p TEXT)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
    SELECT upper(regexp_replace(COALESCE(p,''), '[^A-Za-z0-9]', '', 'g')) ~
           '^((AN|AP|AR|AS|BR|CG|CH|DD|DL|DN|GA|GJ|HP|HR|JH|JK|KA|KL|LA|LD|MH|ML|MN|MP|MZ|NL|OD|OR|PB|PY|RJ|SK|TN|TR|TS|UK|UP|WB)[0-9]{1,2}[A-Z]{0,3}[0-9]{1,4}|[0-9]{2}BH[0-9]{4}[A-Z]{1,2})$';
$$;

CREATE OR REPLACE FUNCTION plate_state_code(p TEXT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN plate_valid_in(p)
                THEN substring(upper(regexp_replace(COALESCE(p,''), '[^A-Za-z0-9]', '', 'g')) from 1 for 2)
           END;
$$;

-- ─────────────────────────────────────────────────────────────────────
-- Spatio-temporal gating.
--
-- Given a sighting at camera A at time t, which cameras could plausibly
-- see the same vehicle next, and in what time window? This removes 95-98%
-- of candidate comparisons before any model runs, and it is what makes an
-- 85%-mAP ReID model operationally trustworthy instead of a false-positive
-- generator.
-- ─────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION candidate_cameras(
    p_from_camera   UUID,
    p_seen_at       TIMESTAMPTZ,
    p_max_travel_s  REAL    DEFAULT 900,
    p_clock_conf    REAL    DEFAULT 1.0,
    p_v_fast        REAL    DEFAULT 1.6,     -- up to 1.6x the routed speed
    p_v_slow        REAL    DEFAULT 0.35,    -- down to 0.35x (traffic, signals)
    p_dwell_s       REAL    DEFAULT 120      -- brief stop tolerance
)
RETURNS TABLE (
    camera_id     UUID,
    camera_ref    TEXT,
    camera_name   TEXT,
    travel_s      REAL,
    window_start  TIMESTAMPTZ,
    window_end    TIMESTAMPTZ,
    expected_at   TIMESTAMPTZ,
    road_dist_m   REAL,
    source        TEXT
)
LANGUAGE sql STABLE AS $$
    WITH slack AS (
        -- Low clock confidence (a drifting legacy DVR) widens the window
        -- rather than producing a false negative. Missing a real transition
        -- is far more damaging than surfacing one extra candidate.
        SELECT GREATEST(0.0, 1.0 - p_clock_conf) * 300.0 AS extra_s
    )
    SELECT
        a.to_camera,
        c.camera_id,
        c.name,
        a.travel_s,
        p_seen_at + make_interval(secs =>
            GREATEST(0, CASE WHEN a.obs_count >= 5 AND a.obs_mean_s IS NOT NULL
                             THEN a.obs_mean_s - 2.5 * COALESCE(a.obs_stddev_s, a.travel_s * 0.3)
                             ELSE a.travel_s / p_v_fast END - s.extra_s)),
        p_seen_at + make_interval(secs =>
            CASE WHEN a.obs_count >= 5 AND a.obs_mean_s IS NOT NULL
                 THEN a.obs_mean_s + 2.5 * COALESCE(a.obs_stddev_s, a.travel_s * 0.3)
                 ELSE a.travel_s / p_v_slow END + p_dwell_s + s.extra_s),
        p_seen_at + make_interval(secs =>
            CASE WHEN a.obs_count >= 5 AND a.obs_mean_s IS NOT NULL
                 THEN a.obs_mean_s ELSE a.travel_s END),
        a.road_dist_m,
        CASE WHEN a.obs_count >= 5 THEN 'observed' ELSE 'routed_prior' END
    FROM camera_adjacency a
    CROSS JOIN slack s
    JOIN camera c ON c.id = a.to_camera AND c.status <> 'DISABLED'
    WHERE a.from_camera = p_from_camera
      AND a.travel_s <= p_max_travel_s
    ORDER BY a.travel_s;
$$;

CREATE OR REPLACE FUNCTION st_feasibility(
    p_expected TIMESTAMPTZ, p_start TIMESTAMPTZ,
    p_end TIMESTAMPTZ, p_actual TIMESTAMPTZ
) RETURNS REAL LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_actual < p_start OR p_actual > p_end THEN 0.0::REAL
        WHEN p_actual <= p_expected THEN
            GREATEST(0.0, 1.0 - EXTRACT(EPOCH FROM (p_expected - p_actual))
                   / NULLIF(EXTRACT(EPOCH FROM (p_expected - p_start)), 0))::REAL
        ELSE
            GREATEST(0.0, 1.0 - EXTRACT(EPOCH FROM (p_actual - p_expected))
                   / NULLIF(EXTRACT(EPOCH FROM (p_end - p_expected)), 0))::REAL
    END;
$$;

-- Learn real transition times from confirmed links, so the gate gets
-- sharper the longer the system runs instead of staying frozen at its
-- routing prior.
CREATE OR REPLACE FUNCTION refresh_adjacency_observations(p_days INTEGER DEFAULT 7)
RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE n INTEGER;
BEGIN
    WITH obs AS (
        SELECT from_camera_id, to_camera_id,
               count(*)                    AS c,
               avg(travel_actual_s)::real  AS mean_s,
               COALESCE(stddev_samp(travel_actual_s), 0)::real AS sd_s
        FROM track_link
        WHERE decision = 'AUTO'
          AND from_camera_id IS NOT NULL
          AND travel_actual_s IS NOT NULL
          AND timestamp > now() - make_interval(days => p_days)
        GROUP BY from_camera_id, to_camera_id
        HAVING count(*) >= 3
    )
    UPDATE camera_adjacency a
       SET obs_count = obs.c, obs_mean_s = obs.mean_s,
           obs_stddev_s = obs.sd_s, updated_at = now()
      FROM obs
     WHERE a.from_camera = obs.from_camera_id
       AND a.to_camera = obs.to_camera_id;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- ─────────────────────────────────────────────────────────────────────
-- Trajectory as GeoJSON, ready for MapLibre. One call, one payload.
-- Observed segments and inferred corridors are separate features so the
-- map can style them differently -- drawing an inferred segment as if it
-- were observed would misrepresent evidence.
-- ─────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION vehicle_track_geojson(p_vehicle_track_id TEXT)
RETURNS JSONB LANGUAGE sql STABLE AS $$
    WITH pts AS (
        SELECT vs.timestamp, vs.latitude, vs.longitude, vs.camera_ref,
               vs.sighting_id, vs.plate_normalized, vs.plate_confidence,
               vs.vehicle_type, vs.vehicle_color, vs.speed_kmph, vs.heading_deg,
               vs.snapshot_key,
               c.name AS camera_name,
               row_number() OVER (ORDER BY vs.timestamp) AS seq
        FROM vehicle_sighting vs
        LEFT JOIN camera c ON c.id = vs.camera_id
        WHERE vs.vehicle_track_id = p_vehicle_track_id
          AND vs.latitude IS NOT NULL
        ORDER BY vs.timestamp
    ),
    line AS (
        SELECT CASE WHEN count(*) >= 2 THEN
            ST_AsGeoJSON(ST_MakeLine(
                ST_SetSRID(ST_MakePoint(longitude, latitude), 4326) ORDER BY timestamp))::jsonb
        END AS geom FROM pts
    ),
    features AS (
        SELECT jsonb_build_object(
            'type', 'Feature',
            'geometry', jsonb_build_object('type','Point',
                        'coordinates', jsonb_build_array(longitude, latitude)),
            'properties', jsonb_build_object(
                'kind','sighting', 'seq', seq, 'timestamp', timestamp,
                'camera_ref', camera_ref, 'camera_name', camera_name,
                'sighting_id', sighting_id, 'plate', plate_normalized,
                'plate_confidence', plate_confidence,
                'vehicle_type', vehicle_type, 'vehicle_color', vehicle_color,
                'speed_kmph', speed_kmph, 'heading_deg', heading_deg,
                'snapshot_key', snapshot_key)) AS f
        FROM pts
    )
    SELECT jsonb_build_object(
        'type', 'FeatureCollection',
        'vehicle_track_id', p_vehicle_track_id,
        'features',
            COALESCE((SELECT jsonb_agg(f) FROM features), '[]'::jsonb)
            || CASE WHEN (SELECT geom FROM line) IS NOT NULL THEN
                 jsonb_build_array(jsonb_build_object(
                   'type','Feature',
                   'geometry', (SELECT geom FROM line),
                   'properties', jsonb_build_object('kind','path')))
               ELSE '[]'::jsonb END);
$$;
