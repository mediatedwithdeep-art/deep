-- Department on the sighting row, so scoping is a predicate not a join.
--
-- WHY THIS EXISTS
-- ───────────────
-- Department isolation was added as an EXISTS through camera -> department.
-- Correct, but measured at 200k sightings it cost 38 ms against 0.4 ms
-- unscoped: the subquery stops the planner using the timestamp index to
-- satisfy ORDER BY ... LIMIT early, so it gathers every matching row for
-- every camera in the department first. At 26 departments and 80,000
-- cameras that is the live map's dominant cost.
--
-- The column is maintained by a TRIGGER, not by the application. A
-- denormalised authorisation key that application code is trusted to fill
-- is a data leak waiting for one missed INSERT path -- the trigger makes
-- the database the single writer, so the value cannot drift from the
-- camera's real owner however the row arrives.

ALTER TABLE vehicle_sighting
    ADD COLUMN IF NOT EXISTS department_code TEXT;

CREATE OR REPLACE FUNCTION sighting_department_code()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    SELECT d.code INTO NEW.department_code
      FROM camera c JOIN department d ON d.id = c.department_id
     WHERE c.id = NEW.camera_id;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION sighting_department_code() IS
    'Derives vehicle_sighting.department_code from the owning camera. The '
    'database is the only writer of this column because it gates access.';

DROP TRIGGER IF EXISTS trg_sighting_department ON vehicle_sighting;
CREATE TRIGGER trg_sighting_department
    BEFORE INSERT OR UPDATE OF camera_id ON vehicle_sighting
    FOR EACH ROW EXECUTE FUNCTION sighting_department_code();

-- Backfill anything written before this migration.
UPDATE vehicle_sighting s
   SET department_code = d.code
  FROM camera c JOIN department d ON d.id = c.department_id
 WHERE c.id = s.camera_id
   AND s.department_code IS DISTINCT FROM d.code;

-- The access pattern this exists for: one department's recent activity,
-- newest first, stopping at LIMIT.
CREATE INDEX IF NOT EXISTS vs_dept_ts_idx
    ON vehicle_sighting (department_code, timestamp DESC);

-- Re-pointing a camera at another department must re-stamp its history,
-- or yesterday's sightings stay readable by the previous owner.
CREATE OR REPLACE FUNCTION camera_department_changed()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.department_id IS DISTINCT FROM OLD.department_id THEN
        UPDATE vehicle_sighting s
           SET department_code = (SELECT d.code FROM department d
                                   WHERE d.id = NEW.department_id)
         WHERE s.camera_id = NEW.id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_camera_department_changed ON camera;
CREATE TRIGGER trg_camera_department_changed
    AFTER UPDATE OF department_id ON camera
    FOR EACH ROW EXECUTE FUNCTION camera_department_changed();
