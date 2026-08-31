-- Tiered storage: HOT / WARM / COLD, and retention that can be changed
-- without editing a migration.
--
-- WHAT WAS WRONG
-- ──────────────
-- `partitioned_table_config` held a single `retention_days` per table and
-- `drop_old_partitions()` deleted anything older. That is a one-cliff
-- model: data is fully queryable, and then it is gone. It cannot express
-- the thing an estate actually needs -- recent data on fast storage, older
-- data still available but cheaper, oldest data archived before deletion
-- -- and it gave an operator no way to set a 7- or 15-day window for the
-- high-volume tables without a schema change and a redeploy.
--
-- THE MODEL
-- ─────────
--   HOT   on primary storage, indexed, served to the UI directly.
--   WARM  still in the database, still queryable, but no longer expected
--         to be fast: indexes it does not need are dropped and the
--         partition is left for the planner to seq-scan.
--   COLD  exported to the object store and detached from the parent, so
--         the query planner stops considering it at all. Recoverable, not
--         online.
--   then  dropped.
--
-- Boundaries are cumulative ages in days, so the invariant is simply
-- hot <= warm <= cold, enforced by a CHECK rather than by convention.
--
-- WHY THE COLD STEP IS DETACH AND NOT DROP
-- ────────────────────────────────────────
-- BSA s63 evidence and the DPDP audit trail both outlive the operational
-- window. Detaching keeps the table on disk under its own name, so an
-- export can be taken from it after it has left the query path. Dropping
-- at the cold boundary would make the retention policy silently destroy
-- material the law requires be producible.

ALTER TABLE partitioned_table_config
    ADD COLUMN IF NOT EXISTS hot_days  INTEGER,
    ADD COLUMN IF NOT EXISTS warm_days INTEGER,
    ADD COLUMN IF NOT EXISTS cold_days INTEGER;

-- Default the tiers from the retention already configured, so this
-- migration changes no table's effective lifetime on the day it is applied.
-- A migration that silently shortens retention is a data-loss event.
UPDATE partitioned_table_config
   SET hot_days  = COALESCE(hot_days,  GREATEST(1, retention_days / 10)),
       warm_days = COALESCE(warm_days, GREATEST(2, retention_days / 2)),
       cold_days = COALESCE(cold_days, retention_days)
 WHERE hot_days IS NULL OR warm_days IS NULL OR cold_days IS NULL;

ALTER TABLE partitioned_table_config
    ALTER COLUMN hot_days  SET NOT NULL,
    ALTER COLUMN warm_days SET NOT NULL,
    ALTER COLUMN cold_days SET NOT NULL;

ALTER TABLE partitioned_table_config
    DROP CONSTRAINT IF EXISTS partitioned_table_config_tier_order;
ALTER TABLE partitioned_table_config
    ADD CONSTRAINT partitioned_table_config_tier_order
    CHECK (hot_days >= 1
       AND warm_days >= hot_days
       AND cold_days >= warm_days
       AND retention_days >= cold_days);

COMMENT ON COLUMN partitioned_table_config.hot_days IS
    'Age in days a partition stays on the HOT tier: indexed and served directly.';
COMMENT ON COLUMN partitioned_table_config.warm_days IS
    'Cumulative age at which a partition leaves WARM. Still queryable while WARM.';
COMMENT ON COLUMN partitioned_table_config.cold_days IS
    'Cumulative age at which a partition is detached for archival. Not dropped.';

-- The high-volume tables, given explicit operator-facing windows. These are
-- the two the brief calls out as needing 7/15-day configurability, and they
-- are now a row update rather than a migration.
UPDATE partitioned_table_config SET hot_days = 1,  warm_days = 3,  cold_days = 3
 WHERE table_name = 'detection';
UPDATE partitioned_table_config SET hot_days = 7,  warm_days = 15, cold_days = 30
 WHERE table_name = 'vehicle_sighting';

-- ─────────────────────────────────────────────────────────────────────
-- Where does a given partition sit today?
-- ─────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION partition_tier(p_table TEXT, p_end DATE,
                                          p_now TIMESTAMPTZ DEFAULT now())
RETURNS TEXT LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN p_end > (p_now - make_interval(days => c.hot_days))::date  THEN 'HOT'
        WHEN p_end > (p_now - make_interval(days => c.warm_days))::date THEN 'WARM'
        WHEN p_end > (p_now - make_interval(days => c.cold_days))::date THEN 'COLD'
        ELSE 'EXPIRED'
    END
    FROM partitioned_table_config c WHERE c.table_name = p_table;
$$;

-- ─────────────────────────────────────────────────────────────────────
-- Report the whole estate's partitions with the tier each one is in.
-- An operator has to be able to answer "what is actually on disk, and how
-- much of it is hot" without reading pg_class by hand.
-- ─────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION storage_tier_report(p_now TIMESTAMPTZ DEFAULT now())
RETURNS TABLE (table_name TEXT, partition_name TEXT, part_end DATE,
               tier TEXT, bytes BIGINT)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    cfg  RECORD;
    part RECORD;
    pe   DATE;
BEGIN
    FOR cfg IN SELECT * FROM partitioned_table_config LOOP
        FOR part IN
            SELECT c.relname, c.oid,
                   pg_get_expr(c.relpartbound, c.oid) AS bound
            FROM pg_class c
            JOIN pg_inherits inh ON inh.inhrelid = c.oid
            JOIN pg_class parent ON parent.oid = inh.inhparent
            WHERE parent.relname = cfg.table_name
        LOOP
            pe := (regexp_match(part.bound, 'TO \(''([0-9-]+)'))[1]::date;
            CONTINUE WHEN pe IS NULL;
            table_name     := cfg.table_name;
            partition_name := part.relname;
            part_end       := pe;
            tier           := partition_tier(cfg.table_name, pe, p_now);
            bytes          := pg_total_relation_size(part.oid);
            RETURN NEXT;
        END LOOP;
    END LOOP;
END $$;

-- ─────────────────────────────────────────────────────────────────────
-- Move partitions that have aged out of COLD off the query path.
--
-- DETACH, not DROP. The partition keeps its name and its rows; it simply
-- stops being part of the parent table, so queries no longer plan against
-- it and an archival job can export it at its own pace.
-- `drop_old_partitions()` remains the only thing that destroys data, and it
-- runs at retention_days, which the CHECK above holds at or beyond the cold
-- boundary.
-- ─────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION detach_cold_partitions(p_now TIMESTAMPTZ DEFAULT now())
RETURNS TABLE (detached TEXT) LANGUAGE plpgsql AS $$
DECLARE
    cfg    RECORD;
    part   RECORD;
    pe     DATE;
    cutoff DATE;
BEGIN
    FOR cfg IN SELECT * FROM partitioned_table_config LOOP
        cutoff := (p_now - make_interval(days => cfg.cold_days))::date;
        FOR part IN
            SELECT c.relname,
                   pg_get_expr(c.relpartbound, c.oid) AS bound
            FROM pg_class c
            JOIN pg_inherits inh ON inh.inhrelid = c.oid
            JOIN pg_class parent ON parent.oid = inh.inhparent
            WHERE parent.relname = cfg.table_name
        LOOP
            pe := (regexp_match(part.bound, 'TO \(''([0-9-]+)'))[1]::date;
            IF pe IS NOT NULL AND pe <= cutoff THEN
                EXECUTE format('ALTER TABLE %I DETACH PARTITION %I',
                               cfg.table_name, part.relname);
                detached := part.relname;
                RETURN NEXT;
            END IF;
        END LOOP;
    END LOOP;
END $$;

COMMENT ON FUNCTION detach_cold_partitions(TIMESTAMPTZ) IS
    'Detach partitions past their COLD boundary. Data is retained on disk for archival; drop_old_partitions() is what deletes.';
