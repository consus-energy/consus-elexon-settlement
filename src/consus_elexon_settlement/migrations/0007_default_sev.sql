-- 0007 default expected volumes
--
-- A Default SEV is a standing profile, registered by 23:59 the day before it
-- takes effect (BSCP602 2.13.1) and standing until replaced. It is the safety
-- net: if neither a Default nor a per-period value is registered before Gate
-- Closure, SVAA sets Settlement Expected Volume to NULL and the deviation is
-- lost entirely (2.13.7).
--
-- Its own table rather than a trading intent with no trade. A Default covers
-- every period rather than one, exists whether or not we intend to trade, and
-- has no Gate Closure -- its deadline belongs to the EMS.

CREATE TABLE default_sev_intent (
    id             bigserial PRIMARY KEY,

    -- The natural key. No settlement period: a Default covers all of them.
    effective_from date        NOT NULL,
    bmu_id         varchar(11) NOT NULL,
    revision       int         NOT NULL DEFAULT 1 CHECK (revision >= 1),

    state          text        NOT NULL,
    detail         text,

    outbound_file_id bigint REFERENCES outbound_file(id),

    received_at    timestamptz NOT NULL DEFAULT now(),
    completed_at   timestamptz,

    UNIQUE (effective_from, bmu_id, revision)
);

CREATE INDEX default_sev_intent_state ON default_sev_intent (state)
    WHERE state IN ('RECEIVED', 'ACTING', 'PARTIAL');

-- The periods the profile covers. Stored rather than only sent, so a later
-- question -- what did the Default say for period 37 on that date -- can be
-- answered without reading the file back out of the archive.
--
-- A partial Default is valid and covers what it covers. Periods without a
-- value fall to NULL if no per-period SEV is registered, which is why
-- DefaultSevIntent.covers_full_day exists and why a partial one is logged.
CREATE TABLE default_sev_period (
    id                  bigserial PRIMARY KEY,
    default_sev_id      bigint   NOT NULL
                            REFERENCES default_sev_intent(id) ON DELETE CASCADE,
    settlement_period   smallint NOT NULL
                            CHECK (settlement_period BETWEEN 1 AND 50),
    volume_mwh          numeric(14, 4) NOT NULL,
    UNIQUE (default_sev_id, settlement_period)
);