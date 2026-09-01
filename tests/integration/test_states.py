"""State transitions against a real database.

The state machine is the part of this system most likely to be subtly wrong,
because the interesting cases are concurrent and none of them can be reached
from a unit test. Two processes acting on the same feedback, a NACK arriving
after a file is already superseded, a sweep racing an acknowledgement.

The guarding is deliberately doubled: states.py checks legality, and every
UPDATE carries a WHERE clause on the current state. The first catches
programmer error, the second catches concurrency. These tests exercise both.
"""

from __future__ import annotations

import datetime as dt

import pytest

from consus_elexon_settlement import db, states
from consus_elexon_settlement.idd import adt

from ..conftest import NOW, built_file, notification_in


# --- file lifecycle ---------------------------------------------------------

def test_built_file_can_be_sent(conn, channel):
    file_id = built_file(conn, channel)
    db.record_sent(conn, file_id)
    assert _state(conn, file_id) == states.SENT


def test_send_failure_is_retryable(conn, channel):
    """A failed send leaves the file intact and the sequence number ours.

    This is the whole reason for build-once-send-many: the bytes and the
    number survive the failure, so the retry is the same file rather than a
    new one leaving a gap behind it.
    """
    file_id = built_file(conn, channel)
    db.record_send_failed(conn, file_id, "connection refused")
    assert _state(conn, file_id) == states.SEND_FAILED

    db.record_sent(conn, file_id)
    assert _state(conn, file_id) == states.SENT

    attempts = conn.execute(
        "SELECT send_attempts, last_error FROM outbound_file WHERE id = %s", (file_id,)
    ).fetchone()
    assert attempts[0] == 1
    assert "connection refused" in attempts[1]


def test_receipt_ack_is_not_acceptance(conn, channel):
    """Response code 0 means the file arrived and parsed. Nothing more.

    RECEIPT_ACKED is terminal for the file. Whether the contents are agreed is
    a separate question answered by a separate flow, and the notification
    inside is still SUBMITTED at this point.
    """
    file_id = built_file(conn, channel)
    notification_id = notification_in(conn, file_id)
    db.record_sent(conn, file_id)
    db.submit_items(conn, "notification", file_id)

    db.record_receipt_ack(conn, file_id, adt.OK)

    assert _state(conn, file_id) == states.RECEIPT_ACKED
    assert _state(conn, notification_id, "notification") == states.SUBMITTED


def test_header_nack_supersedes_and_preserves_sequence(conn, channel):
    """IDD 2.2.8: response codes 1-3 do not consume the sequence number.

    The corrected file reuses it. Getting this wrong leaves a gap that ECVAA
    stops processing at, and a gap cannot be corrected retrospectively.
    """
    first = built_file(conn, channel)
    original_sequence = _sequence(conn, first)
    db.record_sent(conn, first)

    db.record_receipt_ack(conn, first, adt.SYNTAX_ERROR_HEADER)
    assert _state(conn, first) == states.SUPERSEDED

    correction = db.reserve_file(conn, channel, "E0041001", "D", NOW, supersedes=first)
    assert correction.sequence_number == original_sequence


def test_body_nack_consumes_sequence(conn, channel):
    """Codes 4-7 do consume the number, so the next file takes a new one."""
    first = built_file(conn, channel)
    original_sequence = _sequence(conn, first)
    db.record_sent(conn, first)
    db.record_receipt_ack(conn, first, adt.INCORRECT_CHECKSUM)

    nxt = db.reserve_file(conn, channel, "E0041001", "D", NOW)
    assert nxt.sequence_number == original_sequence + 1


def test_illegal_transition_raises(conn, channel):
    file_id = built_file(conn, channel)
    db.record_sent(conn, file_id)
    db.record_receipt_ack(conn, file_id, adt.OK)

    # RECEIPT_ACKED is terminal apart from being superseded.
    with pytest.raises(states.TransitionError, match="cannot move file"):
        db.record_sent(conn, file_id)


def test_transition_on_stale_state_raises(conn, channel):
    """Simulates two processes acting on the same feedback.

    The second must fail rather than double-apply: it needs to know it lost,
    not assume it succeeded.
    """
    file_id = built_file(conn, channel)
    db.record_sent(conn, file_id)

    # Something else moved it on while we were deciding.
    conn.execute(
        "UPDATE outbound_file SET state = %s WHERE id = %s",
        (states.RECEIPT_ACKED, file_id),
    )

    with pytest.raises(states.TransitionError):
        db.record_receipt_ack(conn, file_id, adt.OK)


# --- the deadline sweep -----------------------------------------------------

def test_unacknowledged_sweep_finds_silent_files(conn, channel):
    """Silence is the failure mode that looks like success.

    A sent file with nothing back is a position that may be unhedged. The
    sweep is what makes that visible before Gate Closure rather than at
    cash-out.
    """
    quiet = built_file(conn, channel)
    db.record_sent(conn, quiet)
    conn.execute(
        "UPDATE outbound_file SET sent_at = %s WHERE id = %s",
        (NOW - dt.timedelta(hours=2), quiet),
    )

    answered = built_file(conn, channel)
    db.record_sent(conn, answered)
    db.record_receipt_ack(conn, answered, adt.OK)

    swept = db.mark_unacknowledged(conn, older_than=NOW - dt.timedelta(hours=1))

    assert swept == [quiet]
    assert _state(conn, quiet) == states.UNACKNOWLEDGED
    assert _state(conn, answered) == states.RECEIPT_ACKED


def test_unacknowledged_file_can_still_be_acked(conn, channel):
    """A late acknowledgement is not an error. It is a relief."""
    file_id = built_file(conn, channel)
    db.record_sent(conn, file_id)
    conn.execute(
        "UPDATE outbound_file SET sent_at = %s WHERE id = %s",
        (NOW - dt.timedelta(hours=2), file_id),
    )
    db.mark_unacknowledged(conn, older_than=NOW - dt.timedelta(hours=1))

    db.record_receipt_ack(conn, file_id, adt.OK)
    assert _state(conn, file_id) == states.RECEIPT_ACKED


# --- notifications ----------------------------------------------------------

def test_acceptance_cascades_to_periods(conn, channel):
    file_id = built_file(conn, channel)
    notification_id = notification_in(conn, file_id, periods=(37, 38, 39))
    db.record_sent(conn, file_id)
    db.submit_items(conn, "notification", file_id)

    db.accept_notification(conn, notification_id)

    assert _state(conn, notification_id, "notification") == states.ACCEPTED
    assert _period_states(conn, notification_id) == {states.ACCEPTED}


def test_rejection_records_reason_per_period(conn, channel):
    """E0091 names periods but carries no per-period reason: N0187 is on EDX
    only. Named periods therefore inherit the notification reason.

    All periods are rejected regardless of which were named, because the
    notification as a whole did not take effect.
    """
    file_id = built_file(conn, channel)
    notification_id = notification_in(conn, file_id, periods=(37, 38))
    db.record_sent(conn, file_id)
    db.submit_items(conn, "notification", file_id)

    db.reject_notification(
        conn, notification_id,
        reason="credit cover exceeded",
        periods={37: "credit cover exceeded"},
    )

    assert _state(conn, notification_id, "notification") == states.REJECTED
    assert _period_states(conn, notification_id) == {states.REJECTED}

    reasons = dict(conn.execute(
        """SELECT settlement_period, rejection_reason FROM notification_period
            WHERE notification_id = %s""",
        (notification_id,),
    ).fetchall())
    assert reasons[37] == "credit cover exceeded"
    assert reasons[38] == "credit cover exceeded"


def test_accepted_notification_cannot_be_rejected(conn, channel):
    """Feedback arriving twice, or out of order, must not overwrite an
    outcome. A rejected notification is corrected by a new submission, which
    is a new row -- moving this one would lose what was rejected and why."""
    file_id = built_file(conn, channel)
    notification_id = notification_in(conn, file_id)
    db.record_sent(conn, file_id)
    db.submit_items(conn, "notification", file_id)
    db.accept_notification(conn, notification_id)

    with pytest.raises(states.TransitionError, match="terminal"):
        db.reject_notification(conn, notification_id, reason="too late")


# --- correlation ------------------------------------------------------------

def test_business_key_is_unique(conn, channel):
    """E0091 carries no filename, so the business key must identify exactly
    one notification. Migration 0003 enforces it; without that a rejection
    could mark a live position failed while the failed one looked healthy."""
    import psycopg

    first = built_file(conn, channel)
    notification_in(conn, first, reference="REF0000001")

    second = built_file(conn, channel)
    with pytest.raises(psycopg.errors.UniqueViolation):
        notification_in(conn, second, reference="REF0000001")


def test_find_notification_by_reference(conn, channel):
    file_id = built_file(conn, channel)
    notification_id = notification_in(conn, file_id, reference="REF0000009")

    found = db.find_notifications_by_reference(
        conn,
        ecvnaa_id="AUTH000001",
        ecvn_ecvnaa_id="AUTH000001",
        reference_code="REF0000009",
        effective_from=dt.date(2026, 9, 1),
    )
    assert found == [notification_id]


def test_find_file_by_filename(conn, channel):
    """E0281 hands our own filename back, which is the reliable correlation
    key: reference codes are ours to choose and may repeat, filenames are
    unique across central systems within a month (IDD 2.2.5)."""
    file_id = built_file(conn, channel)
    filename = conn.execute(
        "SELECT filename FROM outbound_file WHERE id = %s", (file_id,)
    ).fetchone()[0]

    assert db.find_file_by_filename(conn, filename) == file_id
    assert db.find_file_by_filename(conn, "NOSUCHFILE123") is None


# --- WMAN -------------------------------------------------------------------

def test_wman_rejection_at_period_and_unit_level(conn, channel):
    """E0521 rejects either the whole period or named units. Treating one as
    the other would be wrong in a direction that matters: a period-level
    rejection means SVAA does not know we were active at all."""
    file_id = built_file(conn, channel, file_type="E0511001")
    for bmu in ("2__ABCDE001", "2__ABCDE002"):
        conn.execute(
            """INSERT INTO wman (outbound_file_id, settlement_date,
                                 settlement_period, bmu_id, active, state)
                    VALUES (%s, %s, 37, %s, true, 'PENDING')""",
            (file_id, dt.date(2026, 9, 1), bmu),
        )
    db.record_sent(conn, file_id)
    db.submit_items(conn, "wman", file_id)

    affected = db.reject_wman(
        conn, dt.date(2026, 9, 1), 37, "BM Unit not baselined", bmu_id="2__ABCDE001"
    )
    assert affected == 1

    remaining = db.reject_wman(
        conn, dt.date(2026, 9, 1), 37, "period rejected"
    )
    assert remaining == 1


# --- helpers ----------------------------------------------------------------

def _state(conn, entity_id: int, table: str = "outbound_file") -> str:
    return conn.execute(
        f"SELECT state FROM {table} WHERE id = %s", (entity_id,)
    ).fetchone()[0]


def _sequence(conn, file_id: int) -> int:
    return conn.execute(
        "SELECT sequence_number FROM outbound_file WHERE id = %s", (file_id,)
    ).fetchone()[0]


def _period_states(conn, notification_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT state FROM notification_period WHERE notification_id = %s",
        (notification_id,),
    ).fetchall()
    return {r[0] for r in rows}