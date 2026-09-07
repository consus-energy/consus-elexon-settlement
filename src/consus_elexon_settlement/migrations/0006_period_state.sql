-- 0006 period-level state on the SVAA tables
--
-- notification_period carries a state; sev_period and delivered_volume_period
-- do not. That asymmetry was an oversight in 0002, and db.py has been
-- cascading to all three as though they matched.
--
-- The state matters for the same reason it does on notification_period:
-- rejection can be per settlement period, so the period row is what records
-- which half of a partial rejection is still live (ADR-0006). Without it a
-- P0329 naming one period would have nowhere to put that fact.

ALTER TABLE sev_period
    ADD COLUMN state            text NOT NULL DEFAULT 'PENDING',
    ADD COLUMN rejection_reason varchar(80);

ALTER TABLE delivered_volume_period
    ADD COLUMN state            text NOT NULL DEFAULT 'PENDING',
    ADD COLUMN rejection_reason varchar(80);

-- The default exists only to make the ALTER work on existing rows. New rows
-- are inserted with an explicit state, and leaving the default in place would
-- let a caller omit it silently.
ALTER TABLE sev_period            ALTER COLUMN state DROP DEFAULT;
ALTER TABLE delivered_volume_period ALTER COLUMN state DROP DEFAULT;