-- Extensions and shared enum types.
--
-- Note what is NOT here: TimescaleDB. The sighting/detection tables use
-- native PostgreSQL declarative range partitioning instead. It gives the
-- same partition pruning and cheap drop-old-data, needs no extension, and
-- runs unmodified on RDS, Cloud SQL, Azure Postgres and a plain container.
-- For a government deployment where the DB may be provisioned by someone
-- else entirely, "no non-standard extension" is worth more than continuous
-- aggregates.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
    CREATE TYPE camera_protocol AS ENUM ('RTSP','ONVIF','HLS','DVR','FILE','SIMULATED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE camera_status AS ENUM ('PENDING','PROBING','ONLINE','DEGRADED','OFFLINE','DISABLED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE camera_role AS ENUM ('SURVEILLANCE','ANPR','PTZ','THERMAL');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE vehicle_type AS ENUM
        ('car','motorcycle','auto_rickshaw','truck','bus','tractor','bicycle','unknown');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE alert_type AS ENUM
        ('WATCHLIST_HIT','ANPR_MATCH','MULTI_CAMERA','RESTRICTED_ZONE',
         'SUSPICIOUS_PATTERN','CAMERA_OFFLINE','CAMERA_TAMPER','TARGET_LOST');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE alert_severity AS ENUM ('INFO','LOW','MEDIUM','HIGH','CRITICAL');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE alert_state AS ENUM ('NEW','ACKNOWLEDGED','RESOLVED','FALSE_POSITIVE');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE match_decision AS ENUM ('AUTO','PROBABLE','OPERATOR','REJECTED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE user_role AS ENUM ('VIEWER','OPERATOR','INVESTIGATOR','ADMIN','SYSTEM');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
