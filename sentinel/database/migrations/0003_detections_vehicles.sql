-- High-volume tables: detections, vehicles, sightings, plates.
--
-- PARTITIONING STRATEGY
-- These tables are partitioned by RANGE on their timestamp. At 50 cameras
-- this is barely needed; at 80,000 it is the difference between a working
-- system and an unusable one:
--   * queries carrying a time filter touch one or two partitions, not all
--   * "delete data older than N days" is DROP PARTITION (instant, no bloat)
--     instead of DELETE (hours, and a vacuum storm afterwards)
--   * autovacuum works per partition, so it finishes
-- `ensure_partitions()` in 0005 creates them ahead of time.

-- ── vehicles: the global identity a track is assigned ────────────────
-- One row per vehicle the system believes it has seen, across all cameras.
CREATE TABLE vehicle (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Human-readable and stable, e.g. 'V-000123'. Operators quote this on
    -- the radio; a UUID is unusable for that.
    vehicle_track_id   TEXT UNIQUE NOT NULL,
    first_seen         TIMESTAMPTZ NOT NULL,
    last_seen          TIMESTAMPTZ NOT NULL,
    sighting_count     INTEGER NOT NULL DEFAULT 0,
    camera_count       INTEGER NOT NULL DEFAULT 0,
    vehicle_type       vehicle_type NOT NULL DEFAULT 'unknown',
    vehicle_color      TEXT,
    make_model         TEXT,
    -- Best plate read across all sightings, with the confidence that earned it
    best_plate         TEXT,
    best_plate_conf    REAL,
    plate_read_count   INTEGER NOT NULL DEFAULT 0,
    -- Quality-weighted mean of the sighting embeddings; more robust than
    -- any single frame.
    embedding          VECTOR(512),
    embedding_model    TEXT,
    -- The trajectory. LINESTRING M with the M ordinate carrying epoch
    -- seconds, so one geometry is both renderable and queryable.
    path               GEOMETRY(LINESTRINGM, 4326),
    total_distance_m   REAL,
    is_watchlisted     BOOLEAN NOT NULL DEFAULT FALSE,
    metadata           JSONB NOT NULL DEFAULT '{}',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX vehicle_last_seen_idx  ON vehicle (last_seen DESC);
CREATE INDEX vehicle_plate_idx      ON vehicle (best_plate) WHERE best_plate IS NOT NULL;
CREATE INDEX vehicle_plate_trgm_idx ON vehicle USING GIN (best_plate gin_trgm_ops);
CREATE INDEX vehicle_path_idx       ON vehicle USING GIST (path);
CREATE INDEX vehicle_watch_idx      ON vehicle (is_watchlisted) WHERE is_watchlisted;
CREATE INDEX vehicle_type_idx       ON vehicle (vehicle_type, last_seen DESC);
-- HNSW for approximate nearest-neighbour ReID search. At MVP volumes an
-- exact scan would do; the index is here so the query plan does not change
-- shape when the table grows.
CREATE INDEX vehicle_embedding_idx  ON vehicle
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ── vehicle_sightings: one vehicle at one camera, entry to exit ──────
-- This is the operational table: everything the map, the timeline and the
-- search page read comes from here.
CREATE TABLE vehicle_sighting (
    id                UUID NOT NULL DEFAULT gen_random_uuid(),
    sighting_id       TEXT NOT NULL,
    timestamp         TIMESTAMPTZ NOT NULL,
    first_seen        TIMESTAMPTZ NOT NULL,
    last_seen         TIMESTAMPTZ NOT NULL,
    camera_id         UUID NOT NULL,
    camera_ref        TEXT NOT NULL,          -- denormalised: avoids a join on every map read
    vehicle_id        UUID,
    vehicle_track_id  TEXT,
    track_id          TEXT NOT NULL,          -- per-camera tracker id

    vehicle_type      vehicle_type NOT NULL DEFAULT 'unknown',
    type_confidence   REAL,
    vehicle_color     TEXT,
    color_confidence  REAL,
    make_model        TEXT,

    plate_raw         TEXT,
    plate_normalized  TEXT,
    plate_confidence  REAL,
    plate_valid_fmt   BOOLEAN NOT NULL DEFAULT FALSE,

    embedding         VECTOR(512),
    embedding_model   TEXT,

    latitude          DOUBLE PRECISION,
    longitude         DOUBLE PRECISION,
    geom              GEOGRAPHY(POINT, 4326),
    heading_deg       REAL,
    speed_kmph        REAL,

    bbox              INTEGER[4],
    detection_count   INTEGER NOT NULL DEFAULT 1,
    quality_score     REAL NOT NULL DEFAULT 1.0,
    clock_confidence  REAL NOT NULL DEFAULT 1.0,
    snapshot_key      TEXT,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

CREATE INDEX vs_camera_ts_idx   ON vehicle_sighting (camera_id, timestamp DESC);
CREATE INDEX vs_vehicle_ts_idx  ON vehicle_sighting (vehicle_track_id, timestamp);
CREATE INDEX vs_plate_idx       ON vehicle_sighting (plate_normalized)
    WHERE plate_normalized IS NOT NULL;
CREATE INDEX vs_plate_trgm_idx  ON vehicle_sighting USING GIN (plate_normalized gin_trgm_ops);
CREATE INDEX vs_geom_idx        ON vehicle_sighting USING GIST (geom);
CREATE INDEX vs_ts_idx          ON vehicle_sighting (timestamp DESC);
CREATE INDEX vs_type_color_idx  ON vehicle_sighting (vehicle_type, vehicle_color, timestamp DESC);
CREATE UNIQUE INDEX vs_sighting_id_idx ON vehicle_sighting (sighting_id, timestamp);

-- ── detections: per-frame boxes ──────────────────────────────────────
-- Retained briefly for replay, evidence and debugging. NOT the table the
-- application reads: at 80k cameras this is ~800k rows/s and nothing in
-- the UI needs frame-level granularity. Short retention on purpose.
CREATE TABLE detection (
    id               UUID NOT NULL DEFAULT gen_random_uuid(),
    timestamp        TIMESTAMPTZ NOT NULL,
    camera_id        UUID NOT NULL,
    camera_ref       TEXT NOT NULL,
    track_id         TEXT NOT NULL,
    sighting_id      TEXT,
    frame_seq        BIGINT,
    vehicle_type     vehicle_type NOT NULL,
    confidence       REAL NOT NULL,
    bbox             INTEGER[4] NOT NULL,
    vehicle_color    TEXT,
    plate_raw        TEXT,
    plate_confidence REAL,
    quality_score    REAL,
    latitude         DOUBLE PRECISION,
    longitude        DOUBLE PRECISION,
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

CREATE INDEX det_camera_ts_idx ON detection (camera_id, timestamp DESC);
CREATE INDEX det_track_idx     ON detection (track_id, timestamp);
CREATE INDEX det_ts_idx        ON detection (timestamp DESC);

-- ── plates: every ANPR read, kept separately from sightings ──────────
-- Separate table because plate search has a completely different access
-- pattern (exact/fuzzy string lookup over a long history) from map queries
-- (spatial + recent), and because a plate read is evidence in its own right.
CREATE TABLE plate_read (
    id                UUID NOT NULL DEFAULT gen_random_uuid(),
    timestamp         TIMESTAMPTZ NOT NULL,
    camera_id         UUID NOT NULL,
    camera_ref        TEXT NOT NULL,
    sighting_id       TEXT,
    vehicle_track_id  TEXT,
    raw_plate         TEXT NOT NULL,
    normalized_plate  TEXT NOT NULL,
    -- Confusion-collapsed form (O/0, I/1, 8/B ...). Indexed so a fuzzy
    -- search is an index lookup rather than a scan with a distance function.
    canonical_plate   TEXT NOT NULL,
    confidence        REAL NOT NULL,
    valid_format      BOOLEAN NOT NULL DEFAULT FALSE,
    corrected         BOOLEAN NOT NULL DEFAULT FALSE,
    plate_width_px    INTEGER,
    state_code        TEXT,
    crop_key          TEXT,
    latitude          DOUBLE PRECISION,
    longitude         DOUBLE PRECISION,
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

CREATE INDEX plate_norm_idx      ON plate_read (normalized_plate, timestamp DESC);
CREATE INDEX plate_canon_idx     ON plate_read (canonical_plate, timestamp DESC);
CREATE INDEX plate_trgm_idx      ON plate_read USING GIN (normalized_plate gin_trgm_ops);
CREATE INDEX plate_camera_ts_idx ON plate_read (camera_id, timestamp DESC);
CREATE INDEX plate_vehicle_idx   ON plate_read (vehicle_track_id, timestamp);
CREATE INDEX plate_ts_idx        ON plate_read (timestamp DESC);

-- ── track_link: every cross-camera association, with its reasoning ───
-- Auditable. An operator can ask "why do you think that is the same car"
-- and get the score breakdown that produced the decision. Also the labelled
-- training set for tuning the fusion weights.
CREATE TABLE track_link (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id            UUID REFERENCES vehicle(id) ON DELETE CASCADE,
    vehicle_track_id      TEXT NOT NULL,
    from_sighting_id      TEXT,
    to_sighting_id        TEXT NOT NULL,
    from_camera_id        UUID,
    to_camera_id          UUID NOT NULL,
    timestamp             TIMESTAMPTZ NOT NULL,
    decision              match_decision NOT NULL,
    score_total           REAL NOT NULL,
    score_plate           REAL NOT NULL DEFAULT 0,
    score_reid            REAL NOT NULL DEFAULT 0,
    score_color           REAL NOT NULL DEFAULT 0,
    score_type            REAL NOT NULL DEFAULT 0,
    score_spatiotemporal  REAL NOT NULL DEFAULT 0,
    travel_expected_s     REAL,
    travel_actual_s       REAL,
    reasons               TEXT[] NOT NULL DEFAULT '{}',
    operator_verdict      TEXT CHECK (operator_verdict IN ('CONFIRMED','REJECTED')),
    operator_id           UUID REFERENCES app_user(id),
    verdict_at            TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX tl_vehicle_idx  ON track_link (vehicle_track_id, timestamp);
CREATE INDEX tl_decision_idx ON track_link (decision, timestamp DESC);
CREATE INDEX tl_review_idx   ON track_link (timestamp DESC)
    WHERE decision = 'PROBABLE' AND operator_verdict IS NULL;
