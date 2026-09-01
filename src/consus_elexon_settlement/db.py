"""Database access and sequence allocation.

Plain psycopg, no ORM. The interesting logic is the sequence counter, and an
assessor should be able to read it without knowing a framework.

Why Postgres rather than Firestore: IDD 2.2.8 requires sequence numbers to be
contiguous per channel, and recipients use gaps to detect missing files. That
needs a row lock held across the counter read, the counter write and the file
row insert, in one transaction. SELECT ... FOR UPDATE makes that explicit.

The rule that follows from it: build once, send many. A file is built, its
bytes persisted, and retries send those same bytes. Regenerating on retry
allocates a second number and leaves a permanent gap at the first.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
import psycopg
from psycopg import Connection

from . import states
from .idd import adt


MAX_SEQUENCE = 999_999_999
FILENAME_LENGTH = 14


class SequenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Channel:
    id: int
    from_role_code: str
    from_participant_id: str
    to_role_code: str
    to_participant_id: str
    test_flag: str

    @property
    def operational(self) -> bool:
        return self.test_flag in ("", "OPER")


def connect(dsn: str) -> Connection:
    return psycopg.connect(dsn)


def get_channel(
    conn: Connection,
    from_role_code: str,
    from_participant_id: str,
    to_role_code: str,
    to_participant_id: str,
    test_flag: str,
) -> Channel:
    row = conn.execute(
        """
        SELECT id, from_role_code, from_participant_id,
               to_role_code, to_participant_id, test_flag
          FROM channel
         WHERE from_role_code = %s AND from_participant_id = %s
           AND to_role_code = %s AND to_participant_id = %s
           AND test_flag = %s
        """,
        (from_role_code, from_participant_id, to_role_code, to_participant_id, test_flag),
    ).fetchone()

    if row is None:
        raise SequenceError(
            f"no channel for {from_role_code}/{from_participant_id} -> "
            f"{to_role_code}/{to_participant_id} [{test_flag or 'OPER'}]"
        )
    return Channel(*row)


def allocate_sequence(conn: Connection, channel_id: int) -> int:
    """Take the next sequence number for a channel.

    MUST be called inside the same transaction that inserts the outbound file
    row. The row lock taken here is held until that transaction commits, so a
    crash between allocation and insert rolls the counter back rather than
    burning a number.
    """
    row = conn.execute(
        "SELECT next_sequence FROM channel WHERE id = %s FOR UPDATE",
        (channel_id,),
    ).fetchone()
    if row is None:
        raise SequenceError(f"no channel with id {channel_id}")

    allocated = row[0]
    # IDD 2.2.1: integer(9), rolling over from 999999999 to 0.
    following = 0 if allocated >= MAX_SEQUENCE else allocated + 1
    conn.execute(
        "UPDATE channel SET next_sequence = %s WHERE id = %s", (following, channel_id)
    )
    return allocated


def make_filename(role_code: str, file_id: int) -> str:
    """IDD 2.2.5: characters 1-2 sender role, 3-14 a unique identifier.

    Names must be unique across all central systems within a month and carry
    no extension. Longer names are truncated on receipt, so 14 is a hard
    limit. The file id is globally unique here, which keeps names distinct
    across channels even when their sequence numbers coincide.
    """
    name = f"{role_code}{file_id:012d}"
    if len(name) != FILENAME_LENGTH:
        raise SequenceError(f"filename {name!r} is not {FILENAME_LENGTH} characters")
    return name


@dataclass(frozen=True)
class ReservedFile:
    id: int
    sequence_number: int
    filename: str


def reserve_file(
    conn: Connection,
    channel: Channel,
    file_type: str,
    message_role: str,
    creation_time: dt.datetime,
    supersedes: int | None = None,
) -> ReservedFile:
    """Allocate a sequence number and insert the outbound file row.

    One transaction covers both. The returned sequence number and filename go
    into the header, so the file is built after this call and its checksum
    recorded afterwards.

    `supersedes` is for a NACK with response code 1-3: those do not consume
    the sender's sequence number (IDD 2.2.8), so the corrected file reuses the
    original number rather than taking a new one.
    """
    with conn.transaction():
        file_id = conn.execute("SELECT nextval('outbound_file_id')").fetchone()[0]

        if supersedes is None:
            sequence = allocate_sequence(conn, channel.id)
        else:
            row = conn.execute(
                "SELECT sequence_number FROM outbound_file WHERE id = %s FOR UPDATE",
                (supersedes,),
            ).fetchone()
            if row is None:
                raise SequenceError(f"no outbound file with id {supersedes}")
            sequence = row[0]
            conn.execute(
                "UPDATE outbound_file SET state = 'SUPERSEDED' WHERE id = %s",
                (supersedes,),
            )

        filename = make_filename(channel.from_role_code, file_id)
        conn.execute(
            """
            INSERT INTO outbound_file (
                id, channel_id, file_type, message_role, sequence_number,
                filename, creation_time, checksum, record_count, state, supersedes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 0, 'RESERVED', %s)
            """,
            (
                file_id,
                channel.id,
                file_type,
                message_role,
                sequence,
                filename,
                creation_time,
                supersedes,
            ),
        )

    return ReservedFile(id=file_id, sequence_number=sequence, filename=filename)


def record_built(
    conn: Connection, file_id: int, checksum: int, record_count: int, gcs_uri: str
) -> None:
    """Mark a reserved file as built once its bytes are archived."""
    with conn.transaction():
        conn.execute(
            """
            UPDATE outbound_file
               SET checksum = %s, record_count = %s, gcs_uri = %s, state = 'BUILT'
             WHERE id = %s AND state = 'RESERVED'
            """,
            (checksum, record_count, gcs_uri, file_id),
        )


def sequence_gaps(conn: Connection, channel_id: int) -> list[int]:
    """Sequence numbers missing from a channel's live outbound files.

    Should always be empty. If it is not, ECVAA will stop processing at the
    gap (IDD 2.2.8) and the fix is a manual agreement with them.
    """
    rows = conn.execute(
        """
        SELECT sequence_number FROM outbound_file
         WHERE channel_id = %s AND state <> 'SUPERSEDED'
         ORDER BY sequence_number
        """,
        (channel_id,),
    ).fetchall()

    numbers = [r[0] for r in rows]
    if not numbers:
        return []
    return sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))





# --- state transitions ------------------------------------------------------
#
# Every transition is guarded twice: the legality check in states.py, and a
# WHERE clause on the current state. The second matters because two processes
# may act on the same feedback -- the poller and a manual replay, say -- and
# the loser must fail rather than double-apply.
#
# Each transition runs in its own transaction. Postgres aborts a whole
# transaction on error, so a TransitionError raised mid-transaction would
# poison the connection: a handler that catches it and carries on would then
# fail on every subsequent statement. One bad notification would take down the
# rest of the batch, including feedback that needed acting on before Gate
# Closure. Wrapping each transition contains the damage to the transition.


def record_sent(conn: Connection, file_id: int) -> None:
    """Transport succeeded. Says nothing about receipt."""
    _move_file(conn, file_id, states.SENT, sent_at=True)


def record_send_failed(conn: Connection, file_id: int, error: str) -> None:
    """Transport failed. The bytes are unchanged and the sequence number is
    still ours, so the retry sends the same file."""
    with conn.transaction():
        current = _file_state(conn, file_id)
        states.check_file_transition(current, states.SEND_FAILED)
        cur = conn.execute(
            """UPDATE outbound_file
                  SET state = %s, send_attempts = send_attempts + 1, last_error = %s
                WHERE id = %s AND state = %s""",
            (states.SEND_FAILED, error[:500], file_id, current),
        )
        _expect_one(cur, file_id, current, states.SEND_FAILED)


def record_receipt_ack(conn: Connection, file_id: int, response_code: int) -> None:
    """An ADT came back.

    Code 0 is receipt acknowledgement and the file is done. Codes 1-3 are
    header-level NACKs: the sequence number is NOT consumed (IDD 2.2.8), so
    the file is superseded and a correction reuses the number. Codes 4-7 do
    consume it, so the file is also superseded but the next file takes a new
    number -- reserve_file already handles that distinction.
    """
    target = states.RECEIPT_ACKED if response_code == adt.OK else states.SUPERSEDED
    with conn.transaction():
        current = _file_state(conn, file_id)
        states.check_file_transition(current, target)
        cur = conn.execute(
            """UPDATE outbound_file
                  SET state = %s, nack_code = %s
                WHERE id = %s AND state = %s""",
            (target,
             None if response_code == adt.OK else response_code,
             file_id, current),
        )
        _expect_one(cur, file_id, current, target)


def mark_unacknowledged(conn: Connection, older_than: dt.datetime) -> list[int]:
    """Sweep files sent before `older_than` with nothing back.

    Returns the ids so the caller can alert. Deliberately does not alert
    itself: this module does no I/O beyond the database, and what counts as
    urgent depends on how close Gate Closure is.
    """
    with conn.transaction():
        rows = conn.execute(
            """UPDATE outbound_file
                  SET state = %s
                WHERE state = %s AND sent_at < %s
            RETURNING id""",
            (states.UNACKNOWLEDGED, states.SENT, older_than),
        ).fetchall()
    return [row[0] for row in rows]


def find_file_by_filename(conn: Connection, filename: str) -> int | None:
    """Correlate feedback back to the file that caused it.

    E0281 returns our own filename and sequence number (N0301, N0198), which
    is a more reliable key than the reference code: the reference code is ours
    to choose and may repeat across notifications, the filename is unique
    across all central systems within a month (IDD 2.2.5).
    """
    row = conn.execute(
        "SELECT id FROM outbound_file WHERE filename = %s", (filename,)
    ).fetchone()
    return row[0] if row else None


def accept_notification(conn: Connection, notification_id: int) -> None:
    """The whole notification passed validation, periods included."""
    with conn.transaction():
        _move_item_locked(conn, "notification", notification_id, states.ACCEPTED)
        conn.execute(
            """UPDATE notification_period SET state = %s
                WHERE notification_id = %s AND state = %s""",
            (states.ACCEPTED, notification_id, states.SUBMITTED),
        )


def reject_notification(
    conn: Connection,
    notification_id: int,
    reason: str,
    periods: dict[int, str] | None = None,
) -> None:
    """Rejection, at notification level and optionally per period.

    E0091 carries a reason on the notification and may name settlement
    periods. Periods not named are still rejected -- the notification as a
    whole did not take effect -- but the specific reason is only known for
    those listed, so the rest inherit the notification's reason rather than
    being left blank.
    """
    with conn.transaction():
        _move_item_locked(conn, "notification", notification_id,
                          states.REJECTED, reason)
        conn.execute(
            """UPDATE notification_period
                  SET state = %s, rejection_reason = %s
                WHERE notification_id = %s AND state = %s""",
            (states.REJECTED, reason[:80], notification_id, states.SUBMITTED),
        )
        for period, period_reason in (periods or {}).items():
            conn.execute(
                """UPDATE notification_period
                      SET rejection_reason = %s
                    WHERE notification_id = %s AND settlement_period = %s""",
                (period_reason[:80], notification_id, period),
            )


def reject_wman(
    conn: Connection,
    settlement_date: dt.date,
    settlement_period: int,
    reason: str,
    bmu_id: str | None = None,
) -> int:
    """Reject wholesale market activity rows. Returns the number affected.

    `bmu_id` None rejects every unit still submitted in that period, which is
    what a period-level exception means. Units already rejected individually
    are left alone -- the WHERE clause on SUBMITTED handles that.
    """
    with conn.transaction():
        if bmu_id is None:
            cur = conn.execute(
                """UPDATE wman SET state = %s, rejection_reason = %s
                    WHERE settlement_date = %s AND settlement_period = %s
                      AND state = %s""",
                (states.REJECTED, reason[:80], settlement_date,
                 settlement_period, states.SUBMITTED),
            )
        else:
            cur = conn.execute(
                """UPDATE wman SET state = %s, rejection_reason = %s
                    WHERE settlement_date = %s AND settlement_period = %s
                      AND bmu_id = %s AND state = %s""",
                (states.REJECTED, reason[:80], settlement_date,
                 settlement_period, bmu_id, states.SUBMITTED),
            )
        return cur.rowcount


def submit_items(conn: Connection, table: str, outbound_file_id: int) -> None:
    """Move every item in a file from PENDING to SUBMITTED.

    Called once the file is sent. `table` is one of notification, sev, wman,
    delivered_volume -- they share the state vocabulary because they share the
    lifecycle.
    """
    if table not in _ITEM_TABLES:
        raise ValueError(f"not an item table: {table}")
    with conn.transaction():
        conn.execute(
            f"""UPDATE {table} SET state = %s
                 WHERE outbound_file_id = %s AND state = %s""",
            (states.SUBMITTED, outbound_file_id, states.PENDING),
        )


# --- inbound ----------------------------------------------------------------


def record_inbound(
    conn: Connection, filename: str, gcs_uri: str, received_at: dt.datetime
) -> int:
    """Record a received file before it is parsed.

    Inserted first, updated after. That order means a file which crashes the
    parser still leaves evidence it arrived -- the case where evidence matters
    most, and the one an audit trail built after parsing would miss.

    A repeated filename is not an error: ECVAA resends after a NACK, and the
    second copy is the one that counts.
    """
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO inbound_file (filename, gcs_uri, received_at, parse_state)
                    VALUES (%s, %s, %s, 'PENDING')
               ON CONFLICT (filename) DO UPDATE SET gcs_uri = EXCLUDED.gcs_uri
                 RETURNING id""",
            (filename, gcs_uri, received_at),
        ).fetchone()
    return row[0]


def record_parse_result(
    conn: Connection,
    filename: str,
    file_type: str | None,
    from_role_code: str | None,
    to_role_code: str | None,
    sequence_number: int | None,
    parse_state: str,
    parse_error: str | None,
    response_code: int,
) -> None:
    """Store what the router made of the file.

    The response code is recorded whether the file parsed or not, because it
    is what we told the sender and we may have to justify it later.
    """
    with conn.transaction():
        conn.execute(
            """UPDATE inbound_file
                  SET file_type = %s, from_role_code = %s, to_role_code = %s,
                      sequence_number = %s, parse_state = %s, parse_error = %s,
                      response_code = %s
                WHERE filename = %s""",
            (file_type, from_role_code, to_role_code, sequence_number,
             parse_state, parse_error[:500] if parse_error else None,
             response_code, filename),
        )


def record_handled(conn: Connection, filename: str, error: str | None = None) -> None:
    """The handler finished, successfully or not.

    Separate from the parse result because they fail independently: a file can
    parse cleanly and then fail to be actioned because our database was down.
    Only the first of those is the sender's concern.
    """
    with conn.transaction():
        conn.execute(
            """UPDATE inbound_file
                  SET handled_at = now(), handler_error = %s
                WHERE filename = %s""",
            (error[:500] if error else None, filename),
        )


def record_ack_sent(conn: Connection, filename: str) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE inbound_file SET ack_sent_at = now() WHERE filename = %s",
            (filename,),
        )


def inbound_sequence_gaps(
    conn: Connection, from_role_code: str, to_role_code: str
) -> list[int]:
    """Missing sequence numbers in files received on a channel.

    ECVAA numbers its files to us contiguously, so a gap means one was lost in
    transit. SVAA tolerates gaps in its own numbering, so this is only
    meaningful for ECVAA channels and the caller decides which to check.
    """
    rows = conn.execute(
        """SELECT sequence_number FROM inbound_file
            WHERE from_role_code = %s AND to_role_code = %s
              AND sequence_number IS NOT NULL
            ORDER BY sequence_number""",
        (from_role_code, to_role_code),
    ).fetchall()
    seen = [r[0] for r in rows]
    if not seen:
        return []
    present = set(seen)
    return [n for n in range(seen[0], seen[-1] + 1) if n not in present]


# --- correlation ------------------------------------------------------------


def find_notification_in_file(conn: Connection, outbound_file_id: int) -> int | None:
    """The notification carried by a file.

    EDN has cardinality 1, so a file carries exactly one. Returning a single
    id rather than a list encodes that.
    """
    row = conn.execute(
        "SELECT id FROM notification WHERE outbound_file_id = %s", (outbound_file_id,)
    ).fetchone()
    return row[0] if row else None


def find_notifications_by_reference(
    conn: Connection,
    ecvnaa_id: str,
    ecvn_ecvnaa_id: str,
    reference_code: str,
    effective_from: dt.date,
) -> list[int]:
    """Notifications matching a rejection's business key.

    E0091 carries no filename, so this is the only way back to the row.
    Returns a list because the caller decides what an ambiguous match means --
    and applying a rejection to the wrong notification would mark a live
    position failed while the failed one still looked healthy.
    """
    rows = conn.execute(
        """SELECT id FROM notification
            WHERE ecvnaa_id = %s AND ecvn_ecvnaa_id = %s
              AND reference_code = %s AND effective_from = %s""",
        (ecvnaa_id, ecvn_ecvnaa_id, reference_code, effective_from),
    ).fetchall()
    return [row[0] for row in rows]


def record_acceptance_detail(
    conn: Connection,
    notification_id: int,
    transaction_id: int,
    first_effective_period: int,
) -> None:
    """Store ECVAA's handle for an accepted notification.

    The transaction id is what a query to ECVAA is raised against. The first
    effective period matters because a notification submitted mid-day takes
    effect from a period, not from midnight, so the accepted profile may be
    shorter than the one submitted.
    """
    with conn.transaction():
        conn.execute(
            """UPDATE notification
                  SET transaction_id = %s, first_effective_period = %s
                WHERE id = %s""",
            (transaction_id, first_effective_period, notification_id),
        )


# --- SVAA item state --------------------------------------------------------


def find_outstanding_sev(conn: Connection, bmu_id: str) -> list[int]:
    """Submitted expected volumes for a BM Unit awaiting feedback.

    P0330 acceptance carries only the BM Unit id -- no filename, no dates, no
    sequence number. Correlation therefore depends on there being exactly one
    outstanding submission for the unit. Returns a list so the caller treats
    ambiguity as an error rather than picking one.
    """
    rows = conn.execute(
        """SELECT id FROM sev WHERE bmu_id = %s AND state = %s ORDER BY created_at""",
        (bmu_id, states.SUBMITTED),
    ).fetchall()
    return [r[0] for r in rows]


def accept_sev(conn: Connection, sev_id: int) -> None:
    with conn.transaction():
        _move_item_locked(conn, "sev", sev_id, states.ACCEPTED)
        conn.execute(
            "UPDATE sev_period SET state = %s WHERE sev_id = %s AND state = %s",
            (states.ACCEPTED, sev_id, states.SUBMITTED),
        )


def reject_sev(
    conn: Connection, sev_id: int, reason: str, settlement_period: int | None = None
) -> None:
    """Reject an expected volume, wholly or for one period.

    P0329 makes every field optional except the reason, so a rejection may
    name a period or not. Where it does not, the whole submission is rejected:
    a rejection naming nothing cannot be assumed partial.
    """
    with conn.transaction():
        if settlement_period is None:
            _move_item_locked(conn, "sev", sev_id, states.REJECTED, reason)
            conn.execute(
                "UPDATE sev_period SET state = %s WHERE sev_id = %s AND state = %s",
                (states.REJECTED, sev_id, states.SUBMITTED),
            )
        else:
            conn.execute(
                """UPDATE sev_period SET state = %s
                    WHERE sev_id = %s AND settlement_period = %s AND state = %s""",
                (states.REJECTED, sev_id, settlement_period, states.SUBMITTED),
            )


def find_outstanding_delivered(
    conn: Connection, settlement_date: dt.date, bmu_id: str | None = None
) -> list[int]:
    """Delivered volumes awaiting feedback for a settlement date."""
    if bmu_id is None:
        rows = conn.execute(
            """SELECT id FROM delivered_volume
                WHERE settlement_date = %s AND state = %s ORDER BY created_at""",
            (settlement_date, states.SUBMITTED),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id FROM delivered_volume
                WHERE settlement_date = %s AND bmu_id = %s AND state = %s
                ORDER BY created_at""",
            (settlement_date, bmu_id, states.SUBMITTED),
        ).fetchall()
    return [r[0] for r in rows]


def accept_delivered(conn: Connection, delivered_id: int) -> None:
    with conn.transaction():
        _move_item_locked(conn, "delivered_volume", delivered_id, states.ACCEPTED)
        conn.execute(
            """UPDATE delivered_volume_period SET state = %s
                WHERE delivered_volume_id = %s AND state = %s""",
            (states.ACCEPTED, delivered_id, states.SUBMITTED),
        )


def reject_delivered(
    conn: Connection,
    delivered_id: int,
    reason: str,
    settlement_period: int | None = None,
) -> None:
    with conn.transaction():
        if settlement_period is None:
            _move_item_locked(conn, "delivered_volume", delivered_id,
                              states.REJECTED, reason)
            conn.execute(
                """UPDATE delivered_volume_period SET state = %s
                    WHERE delivered_volume_id = %s AND state = %s""",
                (states.REJECTED, delivered_id, states.SUBMITTED),
            )
        else:
            conn.execute(
                """UPDATE delivered_volume_period SET state = %s
                    WHERE delivered_volume_id = %s AND settlement_period = %s
                      AND state = %s""",
                (states.REJECTED, delivered_id, settlement_period, states.SUBMITTED),
            )


def confirm_ecvnaa(
    conn: Connection,
    ecvnaa_id: str,
    key_secret_ref: str | None,
    effective_from: dt.date | None,
) -> None:
    """Record that an authorisation is in force.

    The key itself is never stored here -- only a reference to where it lives
    in the secret store. A credential in a settlement database is a credential
    in every backup of that database.
    """
    with conn.transaction():
        conn.execute(
            """UPDATE ecvnaa
                  SET confirmed_at = now(),
                      key_secret_ref = COALESCE(%s, key_secret_ref),
                      effective_from = COALESCE(%s, effective_from)
                WHERE ecvnaa_id = %s""",
            (key_secret_ref, effective_from, ecvnaa_id),
        )


# --- internals --------------------------------------------------------------

_ITEM_TABLES = frozenset({"notification", "sev", "wman", "delivered_volume"})


def _file_state(conn: Connection, file_id: int) -> str:
    row = conn.execute(
        "SELECT state FROM outbound_file WHERE id = %s", (file_id,)
    ).fetchone()
    if row is None:
        raise states.TransitionError(f"no outbound file {file_id}")
    return row[0]


def _move_file(
    conn: Connection, file_id: int, target: str, sent_at: bool = False
) -> None:
    with conn.transaction():
        current = _file_state(conn, file_id)
        states.check_file_transition(current, target)
        cur = conn.execute(
            f"""UPDATE outbound_file
                   SET state = %s{', sent_at = now()' if sent_at else ''}
                 WHERE id = %s AND state = %s""",
            (target, file_id, current),
        )
        _expect_one(cur, file_id, current, target)


def _move_item_locked(
    conn: Connection, table: str, item_id: int, target: str, reason: str | None = None
) -> None:
    """Move an item's state. Caller already holds a transaction.

    Named _locked to make the requirement explicit: calling it outside a
    transaction would leave a failed legality check with nothing to roll back,
    and the cascade to the period rows half applied.
    """
    if table not in _ITEM_TABLES:
        raise ValueError(f"not an item table: {table}")
    row = conn.execute(
        f"SELECT state FROM {table} WHERE id = %s", (item_id,)
    ).fetchone()
    if row is None:
        raise states.TransitionError(f"no {table} {item_id}")
    current = row[0]
    states.check_item_transition(current, target)
    cur = conn.execute(
        f"""UPDATE {table} SET state = %s, rejection_reason = %s
             WHERE id = %s AND state = %s""",
        (target, reason[:80] if reason else None, item_id, current),
    )
    _expect_one(cur, item_id, current, target)


def _expect_one(cur, entity_id: int, current: str, target: str) -> None:
    """A transition that changed no rows means someone else got there first.

    Raising rather than passing silently: two processes applying the same
    feedback is a real possibility, and the loser needs to know it lost rather
    than assume it succeeded.
    """
    if cur.rowcount != 1:
        raise states.TransitionError(
            f"{entity_id}: state changed from under us during {current} -> {target}"
        )