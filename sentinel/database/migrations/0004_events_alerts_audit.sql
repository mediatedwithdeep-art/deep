-- Events, alerts, watchlists, camera health, audit and evidence.

-- ── watchlist: what the alert engine is looking for ──────────────────
CREATE TABLE watchlist (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label          TEXT NOT NULL,
    -- Either a plate (known identity) or a seed sighting (operator clicked
    -- a vehicle on the wall). Both together is strongest: plate gives
    -- precision, embedding gives recall on the ~85% of cameras that cannot
    -- read a plate.
    plate_query    TEXT,
    plate_canonical TEXT,
    seed_sighting_id TEXT,
    seed_embedding VECTOR(512),
    vehicle_type   vehicle_type,
    vehicle_color  TEXT,
    severity       alert_severity NOT NULL DEFAULT 'HIGH',
    reason         TEXT NOT NULL,           -- DPDP Act purpose limitation
    case_ref       TEXT,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_by     UUID REFERENCES app_user(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ,
    hit_count      INTEGER NOT NULL DEFAULT 0,
    last_hit_at    TIMESTAMPTZ,
    CONSTRAINT watchlist_needs_a_query
        CHECK (plate_query IS NOT NULL OR seed_sighting_id IS NOT NULL
               OR seed_embedding IS NOT NULL)
);
CREATE INDEX watchlist_active_idx ON watchlist (is_active) WHERE is_active;
CREATE INDEX watchlist_plate_idx  ON watchlist (plate_canonical) WHERE is_active;

-- ── alert_rule: configurable rules, editable without a deploy ────────
-- The engine reads these at runtime. Adding "alert me when a truck enters
-- zone X between 22:00 and 05:00" must not require a code change.
CREATE TABLE alert_rule (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code         TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL,
    alert_type   alert_type NOT NULL,
    severity     alert_severity NOT NULL DEFAULT 'MEDIUM',
    is_enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    -- Rule-specific thresholds, e.g.
    --   {"min_cameras": 3, "window_minutes": 10}
    --   {"location_codes": ["RESTRICTED_SECRETARIAT"]}
    params       JSONB NOT NULL DEFAULT '{}',
    dedup_seconds INTEGER NOT NULL DEFAULT 60,
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── alerts ───────────────────────────────────────────────────────────
CREATE TABLE alert (
    id               UUID NOT NULL DEFAULT gen_random_uuid(),
    alert_id         TEXT NOT NULL,
    timestamp        TIMESTAMPTZ NOT NULL DEFAULT now(),
    alert_type       alert_type NOT NULL,
    severity         alert_severity NOT NULL,
    state            alert_state NOT NULL DEFAULT 'NEW',
    title            TEXT NOT NULL,
    message          TEXT NOT NULL,
    rule_id          UUID,
    watchlist_id     UUID,
    camera_id        UUID,
    camera_ref       TEXT,
    camera_name      TEXT,
    vehicle_track_id TEXT,
    sighting_id      TEXT,
    plate            TEXT,
    latitude         DOUBLE PRECISION,
    longitude        DOUBLE PRECISION,
    geom             GEOGRAPHY(POINT, 4326),
    confidence       REAL NOT NULL DEFAULT 1.0,
    -- The score breakdown and context that justified this alert. Shown to
    -- the operator verbatim: an alert nobody can interrogate is an alert
    -- they will learn to mute.
    evidence         JSONB NOT NULL DEFAULT '{}',
    dedup_key        TEXT,
    acknowledged_by  UUID,
    acknowledged_at  TIMESTAMPTZ,
    resolved_at      TIMESTAMPTZ,
    note             TEXT,
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

CREATE INDEX alert_ts_idx       ON alert (timestamp DESC);
CREATE INDEX alert_open_idx     ON alert (state, severity, timestamp DESC) WHERE state = 'NEW';
CREATE INDEX alert_vehicle_idx  ON alert (vehicle_track_id, timestamp DESC);
CREATE INDEX alert_camera_idx   ON alert (camera_id, timestamp DESC);
CREATE INDEX alert_type_idx     ON alert (alert_type, timestamp DESC);
CREATE INDEX alert_dedup_idx    ON alert (dedup_key, timestamp DESC) WHERE dedup_key IS NOT NULL;
CREATE UNIQUE INDEX alert_alert_id_idx ON alert (alert_id, timestamp);

-- ── events: the generic system event log ─────────────────────────────
-- Everything notable that is not an operator-facing alert: service starts,
-- stream reconnects, model swaps, config changes, partition maintenance.
-- Separate from audit_log, which is specifically about who accessed what.
CREATE TABLE event (
    id           UUID NOT NULL DEFAULT gen_random_uuid(),
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type   TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'INFO'
                 CHECK (severity IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
    service      TEXT NOT NULL,
    camera_id    UUID,
    camera_ref   TEXT,
    message      TEXT NOT NULL,
    payload      JSONB NOT NULL DEFAULT '{}',
    trace_id     TEXT,
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

CREATE INDEX event_ts_idx      ON event (timestamp DESC);
CREATE INDEX event_type_idx    ON event (event_type, timestamp DESC);
CREATE INDEX event_service_idx ON event (service, severity, timestamp DESC);
CREATE INDEX event_camera_idx  ON event (camera_ref, timestamp DESC);

-- ── camera_health: the beacon behind the trust score ─────────────────
CREATE TABLE camera_health (
    id               UUID NOT NULL DEFAULT gen_random_uuid(),
    timestamp        TIMESTAMPTZ NOT NULL DEFAULT now(),
    camera_id        UUID NOT NULL,
    camera_ref       TEXT NOT NULL,
    reachable        BOOLEAN NOT NULL,
    fps_actual       REAL,
    frames_decoded   BIGINT,
    decode_errors    INTEGER,
    -- Near-zero scene_change on a reachable stream means a frozen picture:
    -- the socket is healthy and the image never changes. This is the most
    -- common silent camera failure and nothing else detects it.
    scene_change     REAL,
    mean_luma        REAL,
    blur_variance    REAL,
    latency_ms       REAL,
    clock_offset_ms  INTEGER,
    inference_ms     REAL,
    queue_depth      INTEGER,
    message          TEXT,
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

CREATE INDEX ch_camera_ts_idx ON camera_health (camera_id, timestamp DESC);
CREATE INDEX ch_ts_idx        ON camera_health (timestamp DESC);

-- ── audit_log: who did what, to which data, and why ──────────────────
-- DPDP Act 2023 requires purpose limitation and accountability for
-- personal data. Video of identifiable people is personal data, so every
-- read of it is auditable and carries a stated reason. Build this on day
-- one; it cannot be retrofitted credibly.
CREATE TABLE audit_log (
    id           BIGSERIAL,
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id      UUID,
    username     TEXT,
    department   TEXT,
    action       TEXT NOT NULL,
    resource     TEXT NOT NULL,
    resource_id  TEXT,
    reason       TEXT,
    ip_address   INET,
    user_agent   TEXT,
    result       TEXT NOT NULL DEFAULT 'SUCCESS'
                 CHECK (result IN ('SUCCESS','DENIED','ERROR')),
    detail       JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

CREATE INDEX audit_ts_idx     ON audit_log (timestamp DESC);
CREATE INDEX audit_user_idx   ON audit_log (user_id, timestamp DESC);
CREATE INDEX audit_action_idx ON audit_log (action, timestamp DESC);
CREATE INDEX audit_denied_idx ON audit_log (timestamp DESC) WHERE result = 'DENIED';

-- ── evidence_export: tamper-evident clip export ──────────────────────
-- Hash-chained so any alteration of the export ledger is detectable, and
-- carrying the certificate that makes an export admissible under BSA 2023
-- s.63 (which replaced Indian Evidence Act s.65B).
CREATE TABLE evidence_export (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp      TIMESTAMPTZ NOT NULL DEFAULT now(),
    camera_id      UUID NOT NULL,
    camera_ref     TEXT NOT NULL,
    window_start   TIMESTAMPTZ NOT NULL,
    window_end     TIMESTAMPTZ NOT NULL,
    object_key     TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    chain_prev     TEXT,
    chain_hash     TEXT NOT NULL,
    case_ref       TEXT,
    requested_by   UUID REFERENCES app_user(id),
    purpose        TEXT NOT NULL,
    bsa63_certificate JSONB,
    size_bytes     BIGINT
);
CREATE INDEX evidence_case_idx   ON evidence_export (case_ref, timestamp DESC);
CREATE INDEX evidence_camera_idx ON evidence_export (camera_id, timestamp DESC);

-- ── refresh/session tokens ───────────────────────────────────────────
CREATE TABLE refresh_token (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    -- SHA-256 of the token, never the token. A database leak must not hand
    -- the attacker working sessions.
    token_hash  TEXT UNIQUE NOT NULL,
    issued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    user_agent  TEXT,
    ip_address  INET
);
CREATE INDEX rt_user_idx    ON refresh_token (user_id) WHERE revoked_at IS NULL;
CREATE INDEX rt_expires_idx ON refresh_token (expires_at);
