-- Add the AUDITOR role.
--
-- PART 20 of the brief names five roles. Mapped onto this schema:
--
--   SYSTEM        State Admin      -- every department, every control
--   ADMIN         Department Admin -- one department, full control within it
--   INVESTIGATOR  Investigator     -- case work, evidence export, cross-camera
--   OPERATOR      Police Operator  -- live monitoring, acknowledge, search
--   AUDITOR       Auditor          -- read the audit log; read nothing else
--   VIEWER                         -- pre-existing, read-only within a department
--
-- AUDITOR is deliberately NOT a superset of VIEWER. An auditor's job is to
-- inspect who did what, not to watch the estate; giving them camera:read
-- would widen the surveillance surface to satisfy a compliance role. The
-- permission map in backend/app/security.py enforces that separation and
-- tests/test_security_regression.py asserts it.
--
-- ADD VALUE is not transactional-safe to re-run, so it is guarded. The
-- migration ledger already runs each file once; this makes a manual re-run
-- harmless too.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'user_role' AND e.enumlabel = 'AUDITOR'
    ) THEN
        ALTER TYPE user_role ADD VALUE 'AUDITOR' BEFORE 'SYSTEM';
    END IF;
END $$;
