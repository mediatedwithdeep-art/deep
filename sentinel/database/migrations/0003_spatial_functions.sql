-- Spatio-temporal gating + fuzzy plate search.
-- These two functions are the operational core of cross-camera tracking.

-- ─────────────────────────────────────────────────────────────
-- candidate_cameras(): given a sighting at camera A at time t, return the
-- cameras that could plausibly see the same vehicle next, with the time
-- window in which to look.
--
-- This is what turns 50 candidate cameras into 2-5. At 80,000 cameras it
-- is the difference between a tractable query and a full scan.
--
-- Speed model: OSRM gives a routed duration at typical speed. Real
-- vehicles deviate, so widen it:
--     lower bound = travel_s / v_fast   (speeding, empty road)
--     upper bound = travel_s / v_slow + dwell   (traffic, signals, a stop)
-- Once we have observed transitions on an edge, prefer the empirical
-- mean +/- 2.5 sigma over the OSRM prior.
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION candidate_cameras(
    p_from_camera   UUID,
    p_seen_at       TIMESTAMPTZ,
    p_max_travel_s  REAL    DEFAULT 900,     -- don't gate beyond 15 min
    p_clock_conf    REAL    DEFAULT 1.0,
    p_v_fast        REAL    DEFAULT 1.6,     -- up to 1.6x routed speed
    p_v_slow        REAL    DEFAULT 0.35,    -- down to 0.35x routed speed
    p_dwell_s       REAL    DEFAULT 120      -- brief stop tolerance
)
RETURNS TABLE (
    camera_id     UUID,
    travel_s      REAL,
    window_start  TIMESTAMPTZ,
    window_end    TIMESTAMPTZ,
    expected_at   TIMESTAMPTZ,
    road_dist_m   REAL,
    source        TEXT
)
LANGUAGE sql STABLE AS $$
    WITH slack AS (
        -- Low clock confidence on a legacy DVR widens the window rather
        -- than producing a false negative.
        SELECT GREATEST(0.0, (1.0 - p_clock_conf)) * 300.0 AS extra_s
    )
    SELECT
        a.to_camera,
        a.travel_s,
        -- Prefer the empirical distribution once an edge has enough
        -- observations to be trusted; fall back to the OSRM prior widened
        -- by the speed bounds. The obs_count >= 5 test must match the
        -- `source` column below, or the reported provenance would lie.
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
        CASE WHEN a.obs_count >= 5 THEN 'observed' ELSE 'osrm_prior' END
    FROM camera_adjacency a
    CROSS JOIN slack s
    JOIN camera c ON c.id = a.to_camera AND c.status = 'ACTIVE'
    WHERE a.from_camera = p_from_camera
      AND a.travel_s <= p_max_travel_s
    ORDER BY a.travel_s;
$$;

-- st_feasibility(): how well does an actual observation time fit the
-- expected travel window? 1.0 at the expected time, decaying to 0 at the
-- window edges. Feeds w_s in the fusion score.
CREATE OR REPLACE FUNCTION st_feasibility(
    p_expected TIMESTAMPTZ,
    p_start    TIMESTAMPTZ,
    p_end      TIMESTAMPTZ,
    p_actual   TIMESTAMPTZ
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

-- ─────────────────────────────────────────────────────────────
-- Fuzzy plate search. OCR confusions are systematic (O/0, I/1, 8/B,
-- 5/S, 2/Z, 6/G), so map both sides into a canonical form where each
-- confusion class collapses to one character, then trigram-match.
-- Catches ~all single-confusion misreads that exact match would miss.
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION plate_canon(p TEXT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT translate(upper(regexp_replace(COALESCE(p,''), '[^A-Za-z0-9]', '', 'g')),
                     'OIBSZGDQU',
                     '018526OO0');
$$;

CREATE INDEX IF NOT EXISTS sighting_plate_canon_idx
    ON sighting (plate_canon(plate_norm)) WHERE plate_norm IS NOT NULL;

-- Indian plate grammar check. Used to set sighting.plate_valid_fmt and
-- to reject OCR output that cannot be a real plate.
CREATE OR REPLACE FUNCTION plate_valid_in(p TEXT)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
    SELECT upper(regexp_replace(COALESCE(p,''), '[^A-Za-z0-9]', '', 'g')) ~
           '^((AN|AP|AR|AS|BR|CG|CH|DD|DL|DN|GA|GJ|HP|HR|JH|JK|KA|KL|LA|LD|MH|ML|MN|MP|MZ|NL|OD|OR|PB|PY|RJ|SK|TN|TR|TS|UK|UP|WB)[0-9]{1,2}[A-Z]{0,3}[0-9]{1,4}|[0-9]{2}BH[0-9]{4}[A-Z]{1,2})$';
$$;

-- ─────────────────────────────────────────────────────────────
-- Field-of-view polygon, derived from position + heading + optics.
-- Stored so spatial queries can ask "which cameras SEE this point"
-- rather than "which cameras are NEAR this point" -- a distinction that
-- matters a great deal on a road with buildings.
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION compute_fov(
    p_geom GEOGRAPHY, p_heading REAL, p_fov REAL, p_range REAL
) RETURNS GEOGRAPHY LANGUAGE sql IMMUTABLE AS $$
    -- Build the wedge in UTM 43N (metres, valid across Gujarat) and return
    -- it to 4326. Buffering in degrees would distort the shape badly at
    -- Gujarat's latitude -- a degree of longitude is ~102 km here against
    -- 111 km for latitude, so a "circular" degree buffer is an ellipse.
    --
    -- The ring is centre -> arc -> centre, and the arc points MUST be
    -- emitted in angular order. ST_MakeLine's ORDER BY guarantees that;
    -- an unordered UNION would produce a self-intersecting polygon that
    -- fails silently as a wrong field of view rather than as an error.
    WITH c AS (
        SELECT ST_Transform(p_geom::geometry, 32643) AS pt
    ),
    arc AS (
        SELECT i,
               ST_Translate(
                   c.pt,
                   p_range * sin(radians(p_heading - p_fov / 2.0 + i * p_fov / 24.0)),
                   p_range * cos(radians(p_heading - p_fov / 2.0 + i * p_fov / 24.0))
               ) AS g
        FROM c, generate_series(0, 24) AS i
    ),
    ring AS (
        SELECT ST_MakeLine(arc.g ORDER BY arc.i) AS ln FROM arc
    )
    SELECT ST_Transform(
               ST_MakePolygon(
                   ST_AddPoint(ST_AddPoint(ring.ln, c.pt, 0), c.pt)
               ),
               4326
           )::geography
    FROM ring, c;
$$;

CREATE OR REPLACE FUNCTION camera_fov_trigger() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.heading_deg IS NOT NULL AND NEW.geom IS NOT NULL THEN
        NEW.fov_geom := compute_fov(NEW.geom, NEW.heading_deg,
                                    COALESCE(NEW.fov_deg, 90),
                                    COALESCE(NEW.range_m, 60));
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER camera_fov_bi BEFORE INSERT OR UPDATE OF geom, heading_deg, fov_deg, range_m
    ON camera FOR EACH ROW EXECUTE FUNCTION camera_fov_trigger();

-- ─────────────────────────────────────────────────────────────
-- Trajectory as GeoJSON for the GIS dashboard. One call, one payload.
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION target_track_geojson(p_target UUID)
RETURNS JSONB LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object(
      'type', 'FeatureCollection',
      'features', COALESCE(jsonb_agg(f), '[]'::jsonb))
    FROM (
      SELECT jsonb_build_object(
        'type','Feature',
        'geometry', ST_AsGeoJSON(gt.path)::jsonb,
        'properties', jsonb_build_object(
            'global_track_id', gt.id,
            'started_at', gt.started_at,
            'last_seen_at', gt.last_seen_at,
            'hop_count', gt.hop_count,
            'confidence', gt.confidence,
            'status', gt.status)
      ) AS f
      FROM global_track gt
      WHERE gt.target_id = p_target AND gt.path IS NOT NULL
      ORDER BY gt.started_at
    ) q;
$$;
