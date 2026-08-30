-- Sentinel VMS — core schema
-- PostgreSQL 16 + PostGIS 3.4 + TimescaleDB + pgvector
-- SRID 4326 for storage/interchange; SRID 32643 (UTM 43N, covers Gujarat) for metric work.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ─────────────────────────────────────────────────────────────
-- Organisation / tenancy (26 departments)
-- ─────────────────────────────────────────────────────────────

CREATE TABLE department (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code          TEXT UNIQUE NOT NULL,          -- 'GP_AHM', 'AMC', 'GSRTC'
    name          TEXT NOT NULL,
    parent_id     UUID REFERENCES department(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE site (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    department_id UUID NOT NULL REFERENCES department(id),
    code          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    district      TEXT,
    geom          GEOGRAPHY(POINT, 4326),
    -- edge gateway that owns this site's cameras; NULL = pulled centrally
    gateway_id    UUID,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX site_geom_idx ON site USING GIST (geom);

CREATE TABLE edge_gateway (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code           TEXT UNIQUE NOT NULL,
    site_id        UUID REFERENCES site(id),
    -- mTLS client cert fingerprint: the gateway's machine identity
    cert_sha256    TEXT UNIQUE,
    wg_public_key  TEXT,
    last_seen_at   TIMESTAMPTZ,
    agent_version  TEXT,
    status         TEXT NOT NULL DEFAULT 'PENDING'
                   CHECK (status IN ('PENDING','ACTIVE','DEGRADED','OFFLINE','REVOKED'))
);

-- ─────────────────────────────────────────────────────────────
-- Camera registry — the heterogeneity-absorbing table
-- ─────────────────────────────────────────────────────────────

CREATE TYPE camera_source_type AS ENUM (
    'RTSP','ONVIF','HLS','DVR_ANALOG','VENDOR_SDK','FILE_LOOP'
);
CREATE TYPE camera_role AS ENUM (
    'SURVEILLANCE','ANPR','PTZ','THERMAL','CROWD'
);
CREATE TYPE camera_status AS ENUM (
    'PENDING','PROBING','ACTIVE','DEGRADED','OFFLINE','DISABLED'
);

CREATE TABLE camera (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    external_ref       TEXT,                     -- owning department's own ID
    site_id            UUID NOT NULL REFERENCES site(id),
    department_id      UUID NOT NULL REFERENCES department(id),
    name               TEXT NOT NULL,
    source_type        camera_source_type NOT NULL,
    role               camera_role NOT NULL DEFAULT 'SURVEILLANCE',
    status             camera_status NOT NULL DEFAULT 'PENDING',

    -- Location & optics. `geom` is where the camera IS.
    -- `fov_geom` is the ground polygon it SEES — this is what spatial
    -- queries actually need, and almost every VMS gets it wrong by
    -- storing only the point.
    geom               GEOGRAPHY(POINT, 4326) NOT NULL,
    altitude_m         REAL,
    heading_deg        REAL CHECK (heading_deg BETWEEN 0 AND 360),
    fov_deg            REAL DEFAULT 90,
    range_m            REAL DEFAULT 60,
    fov_geom           GEOGRAPHY(POLYGON, 4326),

    -- Ground-plane homography (3x3, row-major) mapping image px -> local
    -- metric plane. Enables real speed/direction instead of pixel guesses.
    homography         REAL[9],
    calibrated         BOOLEAN NOT NULL DEFAULT FALSE,

    -- Vendor / signal facts that drive adapter + CV behaviour
    vendor             TEXT,
    model              TEXT,
    firmware           TEXT,
    firmware_risk      TEXT CHECK (firmware_risk IN ('OK','EOL','KNOWN_CVE','UNKNOWN')),
    signal_class       TEXT CHECK (signal_class IN ('CVBS','AHD','TVI','CVI','IP')),

    -- Connection. NOTE: credentials are NOT stored here. `credential_ref`
    -- is a Vault path; the API never returns a password.
    host               INET,
    port               INTEGER,
    credential_ref     TEXT,
    main_stream_url    TEXT,
    sub_stream_url     TEXT,          -- what the AI pipeline consumes
    onvif_profile      TEXT,

    -- Observed capabilities, filled by the probe job
    codec              TEXT,
    width              INTEGER,
    height             INTEGER,
    fps                REAL,
    gop_size           INTEGER,

    -- Operational quality, updated from health beacons
    trust_score        REAL NOT NULL DEFAULT 0.5 CHECK (trust_score BETWEEN 0 AND 1),
    last_frame_at      TIMESTAMPTZ,
    clock_offset_ms    INTEGER,

    tags               TEXT[] DEFAULT '{}',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (department_id, external_ref)
);
CREATE INDEX camera_geom_idx     ON camera USING GIST (geom);
CREATE INDEX camera_fov_idx      ON camera USING GIST (fov_geom);
CREATE INDEX camera_status_idx   ON camera (status) WHERE status = 'ACTIVE';
CREATE INDEX camera_site_idx     ON camera (site_id);

-- Camera adjacency: road-network travel time between camera pairs.
-- Precomputed from OSRM. THIS TABLE IS THE SPATIO-TEMPORAL GATE.
-- Without it, cross-camera ReID compares against every camera and the
-- false-positive rate makes the system unusable.
CREATE TABLE camera_adjacency (
    from_camera   UUID NOT NULL REFERENCES camera(id) ON DELETE CASCADE,
    to_camera     UUID NOT NULL REFERENCES camera(id) ON DELETE CASCADE,
    road_dist_m   REAL NOT NULL,
    travel_s      REAL NOT NULL,          -- OSRM routed duration, typical speed
    -- Observed transition stats, learned from confirmed tracks. These
    -- override the OSRM prior once enough evidence accumulates.
    obs_count     INTEGER NOT NULL DEFAULT 0,
    obs_mean_s    REAL,
    obs_stddev_s  REAL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_camera, to_camera)
);
CREATE INDEX cam_adj_from_idx ON camera_adjacency (from_camera, travel_s);

-- ─────────────────────────────────────────────────────────────
-- Detections / sightings  (TimescaleDB hypertable)
-- ─────────────────────────────────────────────────────────────

CREATE TABLE sighting (
    id               UUID NOT NULL DEFAULT uuid_generate_v4(),
    ts               TIMESTAMPTZ NOT NULL,
    camera_id        UUID NOT NULL,
    tracklet_id      UUID NOT NULL,          -- per-camera track
    global_track_id  UUID,                   -- assigned by the matcher

    class            TEXT NOT NULL,          -- car|motorcycle|auto_rickshaw|truck|bus
    class_conf       REAL NOT NULL,
    bbox             INTEGER[4] NOT NULL,    -- x,y,w,h in source pixels

    -- Derived via homography when calibrated
    geom             GEOGRAPHY(POINT, 4326),
    speed_kmph       REAL,
    heading_deg      REAL,

    -- ANPR
    plate_text       TEXT,
    plate_norm       TEXT,                   -- uppercased, whitespace-stripped
    plate_conf       REAL,
    plate_valid_fmt  BOOLEAN,                -- passed the Indian-plate grammar
    plate_crop_key   TEXT,                   -- MinIO object key

    -- Attributes
    colour           TEXT,
    colour_conf      REAL,

    -- Quality / trust
    quality_score    REAL,
    clock_confidence REAL NOT NULL DEFAULT 1.0,

    crop_key         TEXT,
    PRIMARY KEY (id, ts)
);
SELECT create_hypertable('sighting', 'ts', chunk_time_interval => INTERVAL '1 day');

CREATE INDEX sighting_camera_ts_idx ON sighting (camera_id, ts DESC);
CREATE INDEX sighting_track_idx     ON sighting (global_track_id, ts);
CREATE INDEX sighting_plate_idx     ON sighting (plate_norm) WHERE plate_norm IS NOT NULL;
-- Trigram index enables fuzzy plate search directly in SQL, which is how
-- the operator "find this plate" box stays fast on partial/garbled input.
CREATE INDEX sighting_plate_trgm_idx ON sighting USING GIN (plate_norm gin_trgm_ops);
CREATE INDEX sighting_geom_idx      ON sighting USING GIST (geom);

SELECT add_retention_policy('sighting', INTERVAL '90 days');

-- ReID embeddings, kept separate: different lifecycle (30d vs 90d) and
-- a 512-float row would bloat the sighting hypertable's chunk scans.
CREATE TABLE reid_embedding (
    tracklet_id  UUID PRIMARY KEY,
    camera_id    UUID NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,
    embedding    VECTOR(512) NOT NULL,
    class        TEXT,
    colour       TEXT
);
CREATE INDEX reid_hnsw_idx ON reid_embedding
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX reid_ts_idx ON reid_embedding (ts DESC);

-- ─────────────────────────────────────────────────────────────
-- Targets and global tracks
-- ─────────────────────────────────────────────────────────────

CREATE TABLE target (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    label           TEXT NOT NULL,
    plate_query     TEXT,                    -- normalised target plate, if known
    seed_tracklet   UUID,                    -- "track THIS vehicle" from a click
    seed_embedding  VECTOR(512),
    class_hint      TEXT,
    colour_hint     TEXT,
    priority        TEXT NOT NULL DEFAULT 'MEDIUM'
                    CHECK (priority IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    status          TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE','PAUSED','CLOSED')),
    case_ref        TEXT,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ
);
CREATE INDEX target_active_idx ON target (status) WHERE status = 'ACTIVE';

CREATE TABLE global_track (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_id     UUID REFERENCES target(id) ON DELETE CASCADE,
    started_at    TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL,
    hop_count     INTEGER NOT NULL DEFAULT 1,
    -- LINESTRING M: the M ordinate carries epoch seconds, so the whole
    -- trajectory is one geometry that is both renderable and queryable.
    path          GEOMETRY(LINESTRINGM, 4326),
    confidence    REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'OPEN'
                  CHECK (status IN ('OPEN','CLOSED','MERGED'))
);
CREATE INDEX gt_target_idx ON global_track (target_id, last_seen_at DESC);
CREATE INDEX gt_path_idx   ON global_track USING GIST (path);

-- Every cross-camera association decision, with its score breakdown.
-- Auditable: an operator can ask "why did you say that was the same car"
-- and get a real answer. Also the training set for weight tuning.
CREATE TABLE track_link (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    global_track_id  UUID NOT NULL REFERENCES global_track(id) ON DELETE CASCADE,
    from_tracklet    UUID,
    to_tracklet      UUID NOT NULL,
    from_camera      UUID,
    to_camera        UUID NOT NULL,
    ts               TIMESTAMPTZ NOT NULL,
    score_total      REAL NOT NULL,
    score_plate      REAL,
    score_reid       REAL,
    score_colour     REAL,
    score_type       REAL,
    score_st         REAL,
    decision         TEXT NOT NULL CHECK (decision IN ('AUTO','PROBABLE','OPERATOR','REJECTED')),
    operator_verdict TEXT CHECK (operator_verdict IN ('CONFIRMED','REJECTED'))
);
CREATE INDEX tl_track_idx ON track_link (global_track_id, ts);

-- ─────────────────────────────────────────────────────────────
-- Alerts
-- ─────────────────────────────────────────────────────────────

CREATE TABLE alert (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    type          TEXT NOT NULL CHECK (type IN
                  ('TARGET_SIGHTED','TARGET_PROBABLE','TARGET_LOST',
                   'HOTLIST_HIT','ROUTE_DEVIATION','CAMERA_DOWN','TAMPER')),
    severity      TEXT NOT NULL CHECK (severity IN ('INFO','LOW','MEDIUM','HIGH','CRITICAL')),
    target_id     UUID REFERENCES target(id) ON DELETE CASCADE,
    camera_id     UUID,
    sighting_id   UUID,
    confidence    REAL,
    payload       JSONB NOT NULL DEFAULT '{}',
    acked_by      TEXT,
    acked_at      TIMESTAMPTZ,
    state         TEXT NOT NULL DEFAULT 'NEW'
                  CHECK (state IN ('NEW','ACKED','RESOLVED','FALSE_POSITIVE'))
);
CREATE INDEX alert_ts_idx     ON alert (ts DESC);
CREATE INDEX alert_open_idx   ON alert (state, severity) WHERE state = 'NEW';
CREATE INDEX alert_target_idx ON alert (target_id, ts DESC);

-- ─────────────────────────────────────────────────────────────
-- Camera health
-- ─────────────────────────────────────────────────────────────

CREATE TABLE camera_health (
    ts               TIMESTAMPTZ NOT NULL,
    camera_id        UUID NOT NULL,
    reachable        BOOLEAN NOT NULL,
    fps_actual       REAL,
    decode_err_rate  REAL,
    -- A frozen picture on a live stream is the classic silent failure:
    -- the socket is healthy and the image never changes.
    scene_change     REAL,
    mean_luma        REAL,
    blur_var         REAL,
    clock_offset_ms  INTEGER,
    rtt_ms           REAL
);
SELECT create_hypertable('camera_health', 'ts', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX ch_cam_idx ON camera_health (camera_id, ts DESC);
SELECT add_retention_policy('camera_health', INTERVAL '30 days');

-- ─────────────────────────────────────────────────────────────
-- Evidence & audit  (DPDP Act 2023 / BSA 2023 s.63)
-- ─────────────────────────────────────────────────────────────

CREATE TABLE evidence_export (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    camera_id      UUID NOT NULL,
    window_start   TIMESTAMPTZ NOT NULL,
    window_end     TIMESTAMPTZ NOT NULL,
    object_key     TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    -- Hash chain: H(n) = SHA256(H(n-1) || sha256 || metadata || ts).
    -- Append-only; makes tampering with the export log detectable.
    chain_prev     TEXT,
    chain_hash     TEXT NOT NULL,
    case_ref       TEXT,
    requested_by   TEXT NOT NULL,
    purpose        TEXT NOT NULL,          -- DPDP purpose limitation
    bsa63_cert     JSONB                   -- generated s.63 certificate
);

CREATE TABLE audit_log (
    id          BIGSERIAL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL,
    department  TEXT,
    action      TEXT NOT NULL,
    resource    TEXT NOT NULL,
    reason      TEXT,
    ip          INET,
    result      TEXT NOT NULL,
    detail      JSONB,
    PRIMARY KEY (id, ts)
);
SELECT create_hypertable('audit_log', 'ts', chunk_time_interval => INTERVAL '7 days');
