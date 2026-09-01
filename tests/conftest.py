"""Shared fixtures for integration tests.

Requires Postgres. Point SETTLEMENT_TEST_DSN at it:

    SETTLEMENT_TEST_DSN="host=localhost port=5432 dbname=settlement_test user=..."

Without it every integration test skips, which is why the suite currently
reports twenty skips rather than twenty failures.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from consus_elexon_settlement import db, migrate

DSN = os.environ.get("SETTLEMENT_TEST_DSN")

NOW = dt.datetime(2026, 8, 10, 9, 30, tzinfo=dt.timezone.utc)

# Every table, in one place. A truncate list that drifts behind the migrations
# leaks state between tests, and the resulting failures look like logic bugs in
# whatever ran second. Ordered child-before-parent, though CASCADE makes that
# cosmetic.
TABLES = (
    "notification_period",
    "notification",
    "sev_period",
    "sev",
    "delivered_volume_period",
    "delivered_volume",
    "wman",
    "outbound_file",
    "inbound_file",
    "ecvnaa",
    "channel",
)


@pytest.fixture
def conn():
    if not DSN:
        pytest.skip("SETTLEMENT_TEST_DSN not set")

    connection = db.connect(DSN)
    migrate.migrate(connection)
    with connection.transaction():
        connection.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        # outbound_file ids come from an explicit sequence rather than a serial
        # column, so RESTART IDENTITY does not reach it.
        connection.execute("ALTER SEQUENCE outbound_file_id RESTART WITH 1")
    yield connection
    connection.close()


@pytest.fixture
def channel(conn) -> db.Channel:
    """Our ECVNA identity talking to ECVAA, on a test channel."""
    return make_channel(conn)


def make_channel(conn, role="EN", participant="CONSUSEN", flag="TST1") -> db.Channel:
    with conn.transaction():
        conn.execute(
            """INSERT INTO channel (from_role_code, from_participant_id,
                                    to_role_code, to_participant_id, test_flag)
                    VALUES (%s, %s, 'EC', 'ECVAA', %s)""",
            (role, participant, flag),
        )
    return db.get_channel(conn, role, participant, "EC", "ECVAA", flag)


def built_file(conn, channel, file_type="E0041001") -> int:
    """A file reserved and marked built, ready to be sent.

    The common starting point: most state tests care about what happens after
    a file exists, not about building one.
    """
    reserved = db.reserve_file(conn, channel, file_type, "D", NOW)
    db.record_built(conn, reserved.id, checksum=1234, record_count=4,
                    gcs_uri=f"local://{reserved.filename}")
    return reserved.id


def notification_in(conn, file_id, reference="REF0000001",
                    effective_from=dt.date(2026, 9, 1), periods=(37, 38)) -> int:
    """A notification with periods, in PENDING."""
    row = conn.execute(
        """INSERT INTO notification (outbound_file_id, ecvnaa_id, ecvn_ecvnaa_id,
                                     reference_code, effective_from, state)
                VALUES (%s, 'AUTH000001', 'AUTH000001', %s, %s, 'PENDING')
             RETURNING id""",
        (file_id, reference, effective_from),
    ).fetchone()
    notification_id = row[0]
    for period in periods:
        conn.execute(
            """INSERT INTO notification_period
                    (notification_id, settlement_period, volume_mwh, state)
                    VALUES (%s, %s, 0.900, 'PENDING')""",
            (notification_id, period),
        )
    return notification_id