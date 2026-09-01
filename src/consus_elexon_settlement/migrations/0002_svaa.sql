-- 0002 SVAA flows and channel transport rules
--
-- Adds the two SVAA outbound flows a VTP owes -- Submitted Expected Volume
-- and MSID Pair Delivered Volume -- and records the transport differences
-- between SVAA and ECVAA on the channel rather than in code.

-- ECVAA rejects a file whose sequence number is not the next one and stops
-- dead. SVAA rejects a number lower than one already processed but tolerates
-- a gap. That difference belongs on the channel: the gap detector must not
-- raise on an SVAA channel, and must raise on an ECVAA one.
ALTER TABLE channel
    ADD COLUMN allows_sequence_gaps boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN channel.allows_sequence_gaps IS
    'SVAA tolerates gaps, ECVAA does not. Set per central system, not globally.';

-- P0328 BM Unit Submitted Expected Volume Notification.
--
-- Two kinds of submission share this flow, distinguished only by whether the
-- Effective To Date is present:
--
--   Default SEV   -- effective_to NULL, registered by 23:59 the day before it
--                    takes effect, stands until replaced (BSCP602 2.13.1)
--   Per-period    -- effective_to set, submitted before Gate Closure for the
--                    relevant period (BSCP602 2.13.2)
--
-- If neither is registered before Gate Closure, SVAA sets Settlement Expected
-- Volume to NULL for that period and the deviation is lost (2.13.7). Hence
-- keeping a Default in force at all times.
CREATE TABLE sev (
    id                bigserial PRIMARY KEY,
    outbound_file_id  bigint      NOT NULL REFERENCES outbound_file(id),
    effective_from    date        NOT NULL,
    -- NULL means Default SEV. Not a missing value.
    effective_to      date,
    bmu_id            varchar(11) NOT NULL,
    state             text        NOT NULL,
    rejection_reason  varchar(80),
    created_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE INDEX sev_bmu_effective ON sev (bmu_id, effective_from);

-- BSP has cardinality 1-50, so a submission carries at least one period and
-- at most a long clock-change day's worth.
--
-- decimal(14,4), and the sign convention is CVA: positive is Export, negative
-- is Import. That is the opposite of how site import reads, so the conversion
-- happens once, here, not scattered through the optimiser.
CREATE TABLE sev_period (
    id                bigserial PRIMARY KEY,
    sev_id            bigint   NOT NULL REFERENCES sev(id) ON DELETE CASCADE,
    settlement_period smallint NOT NULL CHECK (settlement_period BETWEEN 1 AND 50),
    volume_mwh        numeric(14, 4) NOT NULL,
    UNIQUE (sev_id, settlement_period)
);

-- P0282 MSID Pair Delivered Volume Notification, due at D+1 for every
-- settlement period in which we traded (BSCP602 2.2A.1).
--
-- Unlike the SEV, this comes from metered data rather than from our forecast,
-- so its source is the site metering chain and it arrives a day later.
CREATE TABLE delivered_volume (
    id                bigserial PRIMARY KEY,
    outbound_file_id  bigint      NOT NULL REFERENCES outbound_file(id),
    settlement_date   date        NOT NULL,
    gsp_group_id      varchar(2)  NOT NULL,
    bmu_id            varchar(11) NOT NULL,
    import_msid       bigint      NOT NULL,
    export_msid       bigint,
    state             text        NOT NULL,
    rejection_reason  varchar(80),
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX delivered_volume_date ON delivered_volume (settlement_date, bmu_id);

CREATE TABLE delivered_volume_period (
    id                   bigserial PRIMARY KEY,
    delivered_volume_id  bigint   NOT NULL
                             REFERENCES delivered_volume(id) ON DELETE CASCADE,
    settlement_period    smallint NOT NULL CHECK (settlement_period BETWEEN 1 AND 50),
    volume_mwh           numeric(14, 4) NOT NULL,
    UNIQUE (delivered_volume_id, settlement_period)
);