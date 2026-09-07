-- 0005 intents: what the EMS asked for, and what we did about it
--
-- The layer above submissions. An intent is one decision by the EMS -- "we
-- are trading this BM Unit in this settlement period, at this contracted
-- volume, against this expected volume". The gateway turns it into the three
-- flows that decision requires and records which of them succeeded.
--
-- Separation of concerns is the point. The EMS owns the numbers: it holds the
-- forecast, the trade and the outturn, and publishes them as facts. The
-- gateway owns the settlement obligations: the flows, the deadlines, the
-- sequence discipline, the file formats. It never computes a volume.
--
-- The consequence is worth naming: the gateway cannot tell whether a volume
-- is right, only whether it is well formed and in time. A wrong expected
-- volume sends cleanly and settles wrongly. That check belongs in the EMS,
-- and reconciliation is where the two views get compared.

-- One row per decision.
--
-- Not per flow: the three pre-Gate-Closure submissions are one decision, and
-- splitting them would let a partially consistent set look like several
-- independent successes. An ECVN with no WMAN is a trade SVAA never learns
-- about, and nothing would say so.
CREATE TABLE intent (
    id                bigserial PRIMARY KEY,

    -- The natural key. NOT a UUID.
    --
    -- Pub/Sub delivers at least once, so the same message can arrive twice --
    -- but the EMS can also send the same decision twice by mistake, and a
    -- UUID would make those two cases look different. A natural key makes
    -- them the same case: the second arrival finds the row and does nothing.
    settlement_date   date        NOT NULL,
    settlement_period smallint    NOT NULL
                          CHECK (settlement_period BETWEEN 1 AND 50),
    bmu_id            varchar(11) NOT NULL,

    -- Trading the same period again is revision 2, not a duplicate. Without
    -- this a revised position is indistinguishable from a redelivery, and we
    -- would either reject a real change or double-submit a repeat.
    revision          int         NOT NULL DEFAULT 1 CHECK (revision >= 1),

    -- What the EMS says. CVA convention throughout: positive is Export,
    -- negative is Import.
    --
    -- expected: what the BM Unit would have done absent our action, which
    --           becomes the SEV.
    -- contracted: what we traded, which becomes the ECVN.
    expected_mwh      numeric(14, 4) NOT NULL,
    contracted_mwh    numeric(10, 3) NOT NULL,

    -- Which standing authorisation the ECVN goes under. Established manually
    -- under BSCP71; the EMS knows which counterparty it traded with.
    ecvnaa_id         varchar(10) NOT NULL,
    ecvn_ecvnaa_id    varchar(10) NOT NULL,

    state             text        NOT NULL,

    -- Why an intent is not ACTED. Free text, because the reasons are varied
    -- and mostly for a human: a passed deadline, a missing channel, a
    -- transport failure.
    detail            text,

    received_at       timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz,

    -- The deadline this intent had to meet, computed on receipt rather than
    -- when acted on. Recorded so that a late intent can be shown to have been
    -- late on arrival rather than delayed by us -- the difference matters
    -- when explaining a missed submission.
    gate_closure      timestamptz NOT NULL,

    UNIQUE (settlement_date, settlement_period, bmu_id, revision)
);

CREATE INDEX intent_state ON intent (state)
    WHERE state IN ('RECEIVED', 'ACTING', 'PARTIAL');

CREATE INDEX intent_gate_closure ON intent (gate_closure)
    WHERE state IN ('RECEIVED', 'ACTING', 'PARTIAL');

COMMENT ON TABLE intent IS
    'One decision by the EMS. The gateway derives the flows it requires.';

-- What we did about each intent, one row per flow.
--
-- Separate rows because the flows fail independently and have different
-- deadlines. A retry needs to know exactly which are outstanding rather than
-- resending everything: resending a WMAN that already succeeded would consume
-- a sequence number for a duplicate.
CREATE TABLE intent_flow (
    id           bigserial PRIMARY KEY,
    intent_id    bigint  NOT NULL REFERENCES intent(id) ON DELETE CASCADE,

    -- 'wman', 'ecvn', 'sev', 'delivered'. Not a foreign key to a lookup
    -- table: the set is fixed by the BSC, not by us, and a table would imply
    -- otherwise.
    flow         text    NOT NULL
                     CHECK (flow IN ('wman', 'ecvn', 'sev', 'delivered')),

    -- The file this flow produced, once it has been built. NULL while
    -- pending: the file row does not exist until the sequence number is
    -- reserved.
    outbound_file_id bigint REFERENCES outbound_file(id),

    state        text    NOT NULL,
    detail       text,

    attempts     int     NOT NULL DEFAULT 0,
    last_attempt timestamptz,

    UNIQUE (intent_id, flow)
);

CREATE INDEX intent_flow_outstanding ON intent_flow (intent_id)
    WHERE state IN ('PENDING', 'FAILED');

-- Delivered volumes arrive separately, at D+1, as their own intent kind.
--
-- Not a column on intent, because the MSID Pair is not known at trading time
-- and a delivered volume can cover a period we traded in a different intent.
-- The link is by settlement date, period and BM Unit rather than by id.
CREATE TABLE delivered_intent (
    id                bigserial PRIMARY KEY,
    settlement_date   date        NOT NULL,
    settlement_period smallint    NOT NULL
                          CHECK (settlement_period BETWEEN 1 AND 50),
    bmu_id            varchar(11) NOT NULL,
    revision          int         NOT NULL DEFAULT 1 CHECK (revision >= 1),

    gsp_group_id      varchar(2)  NOT NULL,
    import_msid       bigint      NOT NULL,
    export_msid       bigint,

    -- What we actually delivered: our deviation, not the metered volume.
    -- SVAA holds the metered data already and uses this to allocate how much
    -- of it was ours (BSCP602 Appendix 3.6). Capped by the metered volume, so
    -- over-claiming produces a P0285 exception rather than settling.
    delivered_mwh     numeric(14, 4) NOT NULL,

    state             text        NOT NULL,
    detail            text,

    outbound_file_id  bigint REFERENCES outbound_file(id),

    received_at       timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz,

    -- D+1, one Working Day after the Settlement Day (BSCP602 2.2A.1). Stored
    -- rather than derived: working days depend on a calendar, and recording
    -- the deadline we believed applied is more useful afterwards than
    -- recomputing what it should have been.
    due_by            timestamptz NOT NULL,

    UNIQUE (settlement_date, settlement_period, bmu_id, import_msid, revision)
);

CREATE INDEX delivered_intent_state ON delivered_intent (state)
    WHERE state IN ('RECEIVED', 'FAILED');