-- Departments, zones, users, cameras, streams.
--
-- SRID 4326 for storage; metric work casts to geography, which does the
-- spheroid maths correctly without picking a projection per district.

-- ── Departments (26 of them at state scale) ──────────────────────────
CREATE TABLE department (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'POLICE',
    parent_id   UUID REFERENCES department(id),
    contact     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Locations: named places and zones, including restricted areas ────
-- `geom` is a polygon so "restricted zone" is a real geofence, not a radius.
CREATE TABLE location (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code         TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'ZONE'
                 CHECK (kind IN ('ZONE','DISTRICT','STATION','RESTRICTED','CORRIDOR','TOLL','BORDER')),
    district     TEXT,
    centroid     GEOGRAPHY(POINT, 4326),
    geom         GEOGRAPHY(POLYGON, 4326),
    restricted   BOOLEAN NOT NULL DEFAULT FALSE,
    metadata     JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX location_geom_idx     ON location USING GIST (geom);
CREATE INDEX location_centroid_idx ON location USING GIST (centroid);
CREATE INDEX location_restricted_idx ON location (restricted) WHERE restricted;

-- ── Users ────────────────────────────────────────────────────────────
CREATE TABLE app_user (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username       TEXT UNIQUE NOT NULL,
    email          TEXT UNIQUE,
    full_name      TEXT NOT NULL,
    -- bcrypt/argon2 digest only. There is no column that can hold a
    -- plaintext or reversibly-encrypted password.
    password_hash  TEXT NOT NULL,
    role           user_role NOT NULL DEFAULT 'VIEWER',
    department_id  UUID REFERENCES department(id),
    badge_number   TEXT,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    mfa_secret     TEXT,
    failed_logins  INTEGER NOT NULL DEFAULT 0,
    locked_until   TIMESTAMPTZ,
    last_login_at  TIMESTAMPTZ,
    password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX app_user_dept_idx ON app_user (department_id) WHERE is_active;


-- ── Field-of-view wedge ──────────────────────────────────────────────
-- Stored so spatial queries can ask "which cameras SEE this point" rather
-- than "which cameras are NEAR this point" -- a distinction that matters a
-- great deal on a street with buildings.
--
-- Built in geography space by walking the arc with ST_Project, so it is
-- correct at any latitude without choosing a projection. The arc points
-- MUST be emitted in angular order: an unordered ring produces a
-- self-intersecting polygon that fails silently as a wrong field of view
-- rather than as an error.
CREATE OR REPLACE FUNCTION compute_fov_wedge(
    p_lat DOUBLE PRECISION, p_lon DOUBLE PRECISION,
    p_heading REAL, p_fov REAL, p_range REAL
) RETURNS GEOGRAPHY LANGUAGE sql IMMUTABLE AS $fov$
    WITH centre AS (
        SELECT ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326)::geography AS g
    ),
    arc AS (
        SELECT i,
               ST_Project(centre.g,
                          p_range,
                          radians(p_heading - p_fov / 2.0 + i * p_fov / 16.0))::geometry AS pt
        FROM centre, generate_series(0, 16) AS i
    ),
    ring AS (
        SELECT ST_MakeLine(arc.pt ORDER BY arc.i) AS ln FROM arc
    )
    SELECT ST_MakePolygon(
               ST_AddPoint(ST_AddPoint(ring.ln, centre.g::geometry, 0),
                           centre.g::geometry)
           )::geography
    FROM ring, centre;
$fov$;

-- ── Cameras ──────────────────────────────────────────────────────────
-- The normalisation point for the entire heterogeneous estate. A 2011
-- analog camera on a DVR channel and a 2025 4K ONVIF camera are the same
-- row shape here, and nothing downstream can tell them apart.
CREATE TABLE camera (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id         TEXT UNIQUE NOT NULL,          -- human/stable, e.g. 'AHM-SAT-014'
    external_ref      TEXT,                          -- owning department's own id
    name              TEXT NOT NULL,
    department_id     UUID NOT NULL REFERENCES department(id),
    location_id       UUID REFERENCES location(id),
    zone              TEXT,
    district          TEXT,
    address           TEXT,

    protocol          camera_protocol NOT NULL,
    role              camera_role NOT NULL DEFAULT 'SURVEILLANCE',
    status            camera_status NOT NULL DEFAULT 'PENDING',

    latitude          DOUBLE PRECISION NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude         DOUBLE PRECISION NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    altitude_m        REAL,
    -- Derived from lat/lon by trigger. Keeping lat/lon as the source of
    -- truth means the API, the CSV import and the AI workers never need a
    -- GIS library; PostGIS is used for querying, not as the storage format.
    geom              GEOGRAPHY(POINT, 4326),

    heading_deg       REAL CHECK (heading_deg BETWEEN 0 AND 360),
    fov_deg           REAL NOT NULL DEFAULT 90,
    range_m           REAL NOT NULL DEFAULT 60,
    fov_geom          GEOGRAPHY(POLYGON, 4326),

    -- Connection. NO CREDENTIALS. `credential_ref` names a secret in the
    -- store; the URL columns hold the credential-free form.
    stream_url        TEXT,
    substream_url     TEXT,
    credential_ref    TEXT,
    onvif_host        INET,
    onvif_port        INTEGER,
    dvr_channel       INTEGER,

    vendor            TEXT,
    model             TEXT,
    firmware          TEXT,
    firmware_risk     TEXT CHECK (firmware_risk IN ('OK','EOL','KNOWN_CVE','UNKNOWN')),
    signal_class      TEXT CHECK (signal_class IN ('CVBS','AHD','TVI','CVI','IP')),

    codec             TEXT,
    width             INTEGER,
    height            INTEGER,
    fps               REAL,
    gop_size          INTEGER,
    anpr_capable      BOOLEAN NOT NULL DEFAULT FALSE,

    trust_score       REAL NOT NULL DEFAULT 0.5 CHECK (trust_score BETWEEN 0 AND 1),
    last_seen         TIMESTAMPTZ,
    last_error        TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,

    tags              TEXT[] NOT NULL DEFAULT '{}',
    metadata          JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX camera_geom_idx    ON camera USING GIST (geom);
CREATE INDEX camera_fov_idx     ON camera USING GIST (fov_geom);
CREATE INDEX camera_status_idx  ON camera (status);
CREATE INDEX camera_online_idx  ON camera (status) WHERE status = 'ONLINE';
CREATE INDEX camera_dept_idx    ON camera (department_id);
CREATE INDEX camera_zone_idx    ON camera (zone);
CREATE INDEX camera_tags_idx    ON camera USING GIN (tags);
CREATE INDEX camera_name_trgm   ON camera USING GIN (name gin_trgm_ops);

-- ── Streams: a camera can expose several (main / sub / ANPR crop) ─────
CREATE TABLE stream (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id      UUID NOT NULL REFERENCES camera(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('MAIN','SUB','ANPR','SNAPSHOT')),
    url            TEXT NOT NULL,
    protocol       camera_protocol NOT NULL,
    codec          TEXT,
    width          INTEGER,
    height         INTEGER,
    fps            REAL,
    bitrate_kbps   INTEGER,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    last_probe_at  TIMESTAMPTZ,
    probe_result   JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (camera_id, kind)
);
CREATE INDEX stream_camera_idx ON stream (camera_id) WHERE is_active;

-- ── Camera adjacency: road-network travel time. The gate. ────────────
CREATE TABLE camera_adjacency (
    from_camera   UUID NOT NULL REFERENCES camera(id) ON DELETE CASCADE,
    to_camera     UUID NOT NULL REFERENCES camera(id) ON DELETE CASCADE,
    road_dist_m   REAL NOT NULL,
    travel_s      REAL NOT NULL,
    -- Learned from confirmed transitions; overrides the routing prior once
    -- there is enough evidence.
    obs_count     INTEGER NOT NULL DEFAULT 0,
    obs_mean_s    REAL,
    obs_stddev_s  REAL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_camera, to_camera)
);
CREATE INDEX cam_adj_from_idx ON camera_adjacency (from_camera, travel_s);

-- ── Triggers: keep geometry in step with lat/lon ─────────────────────
CREATE OR REPLACE FUNCTION camera_geometry_sync() RETURNS TRIGGER AS $$
BEGIN
    NEW.geom := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326)::geography;
    IF NEW.heading_deg IS NOT NULL THEN
        NEW.fov_geom := compute_fov_wedge(NEW.latitude, NEW.longitude,
                                          NEW.heading_deg, NEW.fov_deg, NEW.range_m);
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER camera_geometry_sync_trg
    BEFORE INSERT OR UPDATE OF latitude, longitude, heading_deg, fov_deg, range_m
    ON camera FOR EACH ROW EXECUTE FUNCTION camera_geometry_sync();
