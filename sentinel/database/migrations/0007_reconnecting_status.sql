-- RECONNECTING camera status.
--
-- A stream that ended and is being retried is not the same thing as a
-- camera believed gone. Collapsing the two makes a looping or briefly
-- dropped camera flap red in the control room, and operators who learn to
-- ignore a red tile also ignore the genuinely dead one next to it.
--
-- ADD VALUE cannot run inside a transaction block on PostgreSQL when the
-- new label is used in the same transaction, so this migration only adds
-- the label. Nothing here reads it back.

ALTER TYPE camera_status ADD VALUE IF NOT EXISTS 'RECONNECTING' AFTER 'DEGRADED';
