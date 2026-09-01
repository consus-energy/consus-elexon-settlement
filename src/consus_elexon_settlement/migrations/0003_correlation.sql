-- 0003 correlation keys and acceptance detail
--
-- E0091 rejection carries no filename, only the authorisation ids, reference
-- code and effective date. That combination must identify one notification or
-- a rejection could be applied to the wrong one -- marking a live position as
-- failed while the failed one still looks healthy.

CREATE UNIQUE INDEX notification_business_key
    ON notification (ecvnaa_id, ecvn_ecvnaa_id, reference_code, effective_from);

-- E0281 returns ECVAA's transaction id, which is the handle any query to them
-- is raised against, and the first period from which the notification takes
-- effect. A mid-day submission takes effect from a period, not from midnight,
-- so the accepted profile can be shorter than the one submitted.
ALTER TABLE notification
    ADD COLUMN transaction_id bigint,
    ADD COLUMN first_effective_period smallint
        CHECK (first_effective_period BETWEEN 1 AND 50);