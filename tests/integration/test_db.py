"""Tests for sequence allocation.

Requires a Postgres instance. Point SETTLEMENT_TEST_DSN at it, e.g.

    SETTLEMENT_TEST_DSN="host=localhost port=5432 dbname=settlement_test user=..."

These are the tests that matter most in this module: a duplicate or a gap in a
sequence number is not a bug we find in logs, it is ECVAA refusing to process
anything further until a manual agreement fixes it (IDD 2.2.8).
"""

import datetime as dt
import os
import threading

import pytest

from consus_elexon_settlement import db, migrate

DSN = os.environ.get("SETTLEMENT_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SETTLEMENT_TEST_DSN not set")

NOW = dt.datetime(2026, 8, 10, 9, 30, tzinfo=dt.timezone.utc)


@pytest.fixture
def conn():
    connection = db.connect(DSN)
    migrate.migrate(connection)
    with connection.transaction():
        connection.execute(
            "TRUNCATE outbound_file, notification, notification_period, wman, "
            "channel RESTART IDENTITY CASCADE"
        )
        connection.execute("ALTER SEQUENCE outbound_file_id RESTART WITH 1")
    yield connection
    connection.close()


def make_channel(conn, role="EN", participant="CONSUSEN", flag="TST1") -> db.Channel:
    with conn.transaction():
        conn.execute(
            "INSERT INTO channel (from_role_code, from_participant_id, "
            "to_role_code, to_participant_id, test_flag) VALUES (%s, %s, 'EC', 'ECVAA', %s)",
            (role, participant, flag),
        )
    return db.get_channel(conn, role, participant, "EC", "ECVAA", flag)


# --- basics -----------------------------------------------------------------

def test_sequence_starts_at_one(conn):
    # IDD 2.2.8: "Note that sequence numbers start from 1."
    channel = make_channel(conn)
    assert db.reserve_file(conn, channel, "E0041001", "D", NOW).sequence_number == 1


def test_sequence_is_contiguous(conn):
    channel = make_channel(conn)
    numbers = [
        db.reserve_file(conn, channel, "E0041001", "D", NOW).sequence_number
        for _ in range(10)
    ]
    assert numbers == list(range(1, 11))
    assert db.sequence_gaps(conn, channel.id) == []


def test_channels_have_independent_counters(conn):
    # We send E0041 as 'EN' with our ECVNA Id and E0511 as 'VT' with our Party
    # Id. Sharing a counter between them corrupts both sequences.
    ecvna = make_channel(conn, role="EN", participant="CONSUSEN")
    vtp = make_channel(conn, role="VT", participant="CONSUS")

    db.reserve_file(conn, ecvna, "E0041001", "D", NOW)
    db.reserve_file(conn, ecvna, "E0041001", "D", NOW)
    first_vtp = db.reserve_file(conn, vtp, "E0511001", "D", NOW)

    assert first_vtp.sequence_number == 1
    assert db.sequence_gaps(conn, ecvna.id) == []
    assert db.sequence_gaps(conn, vtp.id) == []


def test_test_and_operational_channels_are_separate_rows(conn):
    test = make_channel(conn, flag="TST1")
    operational = make_channel(conn, flag="OPER")
    assert test.id != operational.id
    assert operational.operational and not test.operational


# --- filenames --------------------------------------------------------------

def test_filename_is_role_plus_unique_id(conn):
    channel = make_channel(conn)
    reserved = db.reserve_file(conn, channel, "E0041001", "D", NOW)
    assert reserved.filename == "EN000000000001"
    assert len(reserved.filename) == 14


def test_filenames_unique_across_channels_with_equal_sequences(conn):
    ecvna = make_channel(conn, role="EN", participant="CONSUSEN")
    vtp = make_channel(conn, role="VT", participant="CONSUS")

    first = db.reserve_file(conn, ecvna, "E0041001", "D", NOW)
    second = db.reserve_file(conn, vtp, "E0511001", "D", NOW)

    assert first.sequence_number == second.sequence_number == 1
    assert first.filename != second.filename


# --- crash safety -----------------------------------------------------------

def test_rollback_does_not_burn_a_number(conn):
    """A crash between allocation and insert must not leave a gap."""
    channel = make_channel(conn)
    db.reserve_file(conn, channel, "E0041001", "D", NOW)

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with conn.transaction():
            db.allocate_sequence(conn, channel.id)
            raise Boom

    assert db.reserve_file(conn, channel, "E0041001", "D", NOW).sequence_number == 2
    assert db.sequence_gaps(conn, channel.id) == []


def test_concurrent_allocation_has_no_duplicates_or_gaps(conn):
    """Eight writers, twenty-five files each, one contiguous run."""
    channel = make_channel(conn)
    workers, per_worker = 8, 25
    results: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(workers)

    def worker():
        own = db.connect(DSN)
        try:
            start.wait()
            allocated = [
                db.reserve_file(own, channel, "E0041001", "D", NOW).sequence_number
                for _ in range(per_worker)
            ]
        finally:
            own.close()
        with lock:
            results.extend(allocated)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    total = workers * per_worker
    assert len(results) == total
    assert sorted(results) == list(range(1, total + 1))  # no duplicates, no gaps
    assert db.sequence_gaps(conn, channel.id) == []


def test_duplicate_sequence_is_rejected_by_the_database(conn):
    """Belt and braces: the constraint holds even if the code is wrong."""
    channel = make_channel(conn)
    db.reserve_file(conn, channel, "E0041001", "D", NOW)

    with pytest.raises(Exception):
        with conn.transaction():
            conn.execute(
                "INSERT INTO outbound_file (id, channel_id, file_type, message_role, "
                "sequence_number, filename, creation_time, checksum, record_count, state) "
                "VALUES (999, %s, 'E0041001', 'D', 1, 'EN000000000999', %s, 0, 0, 'BUILT')",
                (channel.id, NOW),
            )


# --- NACK handling ----------------------------------------------------------

def test_header_nack_reuses_the_same_sequence_number(conn):
    """IDD 2.2.8: response codes 1-3 do not consume the sequence number."""
    channel = make_channel(conn)
    original = db.reserve_file(conn, channel, "E0041001", "D", NOW)
    db.reserve_file(conn, channel, "E0041001", "D", NOW)  # a later file

    corrected = db.reserve_file(
        conn, channel, "E0041001", "D", NOW, supersedes=original.id
    )

    assert corrected.sequence_number == original.sequence_number
    assert corrected.id != original.id
    assert db.sequence_gaps(conn, channel.id) == []


def test_body_nack_consumes_the_number(conn):
    """Codes 4-7 do consume it: the next file simply takes the next number."""
    channel = make_channel(conn)
    rejected = db.reserve_file(conn, channel, "E0041001", "D", NOW)
    following = db.reserve_file(conn, channel, "E0041001", "D", NOW)

    assert following.sequence_number == rejected.sequence_number + 1


# --- rollover ---------------------------------------------------------------

def test_rollover_from_999999999_to_zero(conn):
    # IDD 2.2.1: integer(9), rolling over from 999999999 to 0.
    channel = make_channel(conn)
    with conn.transaction():
        conn.execute(
            "UPDATE channel SET next_sequence = 999999999 WHERE id = %s", (channel.id,)
        )

    assert db.reserve_file(conn, channel, "E0041001", "D", NOW).sequence_number == 999999999
    assert db.reserve_file(conn, channel, "E0041001", "D", NOW).sequence_number == 0