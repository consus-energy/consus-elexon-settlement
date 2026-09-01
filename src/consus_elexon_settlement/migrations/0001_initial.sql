-- 0001 initial schema
--
-- Applied once, in order, by db.migrate(). Never edit an applied migration:
-- add a new numbered file. The checksum of each applied file is recorded, so
-- an edit to history fails loudly rather than diverging silently between
-- environments.

-- Who we are and who we talk to. Sequence numbers are contiguous per
-- from-role / from-party / to-role / to-party combination (IDD 2.2.8), so the
-- counter lives on the full tuple. Because we are our own ECVNA we send under
-- two identities -- 'EN' with our ECVNA Id for E0041, 'VT' with our Party Id
-- for E0511 -- and each needs its own gap-free counter.
CREATE TABLE channel (
    id                  serial PRIMARY KEY,
    from_role_code      char(2)     NOT NULL,
    from_participant_id varchar(8)  NOT NULL,
    to_role_code        char(2)     NOT NULL,
    to_participant_id   varchar(8)  NOT NULL,
    -- IDD 2.2.1 field 10: 'OPER' or empty for operational, other values for
    -- test phases. text(4), so a test flag longer than four characters is not
    -- representable. Empty string means omitted.
    test_flag           varchar(4)  NOT NULL,
    -- IDD 2.2.8: sequence numbers start from 1.
    next_sequence       bigint      NOT NULL DEFAULT 1
        CHECK (next_sequence BETWEEN 0 AND 999999999),
    UNIQUE (from_role_code, from_participant_id, to_role_code, to_participant_id, test_flag)
);

-- Standing authorisations. Established manually with ECVAA (E0021 is a manual
-- flow); we record the outcome. The key itself arrives in E0071 and lives in
-- Secret Manager -- this table holds only the reference.
CREATE TABLE ecvnaa (
    ecvnaa_id       varchar(10) PRIMARY KEY,
    key_secret_ref  text,
    counterparty_id varchar(8)  NOT NULL,
    our_pc_flag     char(1)     NOT NULL CHECK (our_pc_flag IN ('P', 'C')),
    effective_from  date        NOT NULL,
    effective_to    date,
    confirmed_at    timestamptz
);

-- Every file we build. Bytes are immutable once written to GCS; the row is
-- the index into that archive.
CREATE TABLE outbound_file (
    id              bigint PRIMARY KEY,
    channel_id      int         NOT NULL REFERENCES channel(id),
    file_type       varchar(8)  NOT NULL,
    message_role    char(1)     NOT NULL CHECK (message_role IN ('D', 'R')),
    sequence_number bigint      NOT NULL,
    filename        varchar(14) NOT NULL,
    creation_time   timestamptz NOT NULL,
    gcs_uri         text,
    checksum        bigint      NOT NULL,
    record_count    int         NOT NULL,
    state           text        NOT NULL,
    sent_at         timestamptz,
    send_attempts   int         NOT NULL DEFAULT 0,
    last_error      text,
    -- IDD 2.2.7 response code from a NACK, if any.
    nack_code       int,
    -- IDD 2.2.8: a NACK for codes 1-3 (header problems) does NOT consume the
    -- sequence number, so the corrected file reuses it and points back here.
    -- Codes 4-7 do consume it and the next file takes a new number.
    supersedes      bigint REFERENCES outbound_file(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel_id, sequence_number, id)
);

-- A live sequence number is unique per channel. Superseded files keep their
-- number, so the constraint only applies to files not yet replaced.
CREATE UNIQUE INDEX outbound_file_live_sequence
    ON outbound_file (channel_id, sequence_number)
    WHERE state <> 'SUPERSEDED';

-- One ECVN. The EDN group has cardinality 1, so a file carries exactly one.
-- Note there is no settlement date here: an ECVN is a half-hourly profile
-- spanning Effective From -> Effective To, not a single day's trade.
CREATE TABLE notification (
    id               bigserial PRIMARY KEY,
    outbound_file_id bigint      NOT NULL REFERENCES outbound_file(id),
    ecvnaa_id        varchar(10) NOT NULL,
    ecvn_ecvnaa_id   varchar(10) NOT NULL,
    reference_code   varchar(10) NOT NULL,
    effective_from   date        NOT NULL,
    effective_to     date,
    state            text        NOT NULL,
    rejection_reason varchar(80),
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Per-period volumes. E0091 rejects per settlement period as well as per
-- notification, so state lives at this level too and partial acceptance is
-- normal.
CREATE TABLE notification_period (
    id                bigserial PRIMARY KEY,
    notification_id   bigint   NOT NULL REFERENCES notification(id) ON DELETE CASCADE,
    -- IDD 2.2.9: 1 to 46/48/50. Never assume 48.
    settlement_period smallint NOT NULL CHECK (settlement_period BETWEEN 1 AND 50),
    volume_mwh        numeric(10, 3) NOT NULL,
    state             text     NOT NULL,
    rejection_reason  varchar(80),
    UNIQUE (notification_id, settlement_period)
);

CREATE TABLE wman (
    id                bigserial PRIMARY KEY,
    outbound_file_id  bigint      NOT NULL REFERENCES outbound_file(id),
    settlement_date   date        NOT NULL,
    settlement_period smallint    NOT NULL CHECK (settlement_period BETWEEN 1 AND 50),
    bmu_id            varchar(11) NOT NULL,
    active            boolean     NOT NULL,
    state             text        NOT NULL,
    rejection_reason  varchar(80),
    UNIQUE (outbound_file_id, bmu_id)
);

-- Everything received, including files that failed to parse. A parse failure
-- still needs an ADT response, so it still needs a row.
CREATE TABLE inbound_file (
    id              bigserial PRIMARY KEY,
    filename        text        NOT NULL,
    file_type       varchar(8),
    from_role_code  char(2),
    -- Feedback flows reach us under two role codes: 'BP' as a party and 'EN'
    -- as an agent (IDD Flow Roles tab). Gap detection is per inbound channel,
    -- so the recipient role is part of the key.
    to_role_code    char(2),
    sequence_number bigint,
    received_at     timestamptz NOT NULL DEFAULT now(),
    gcs_uri         text,
    parse_state     text        NOT NULL,
    parse_error     text,
    response_code   int,
    ack_sent_at     timestamptz
);

CREATE INDEX inbound_sequence
    ON inbound_file (from_role_code, to_role_code, sequence_number);

-- File ids come from an explicit sequence so a filename can be built before
-- the row is inserted (IDD 2.2.5 names are derived from the id).
CREATE SEQUENCE outbound_file_id AS bigint START 1;