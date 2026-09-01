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


def record_sent(conn: Connection, file_id: int) -> None:
    """Transport succeeded. Says nothing about receipt."""
    _move_file(conn, file_id, states.SENT, sent_at=True)


def record_send_failed(conn: Connection, file_id: int, error: str) -> None:
    """Transport failed. The bytes are unchanged and the sequence number is
    still ours, so the retry sends the same file."""
    current = _file_state(conn, file_id)
    states.check_file_transition(current, states.SEND_FAILED)
    with conn.cursor() as cur:
        cur.execute(
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
    current = _file_state(conn, file_id)
    states.check_file_transition(current, target)
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE outbound_file
                  SET state = %s, nack_code = %s
                WHERE id = %s AND state = %s""",
            (target, None if response_code == adt.OK else response_code, file_id, current),
        )
        _expect_one(cur, file_id, current, target)


def mark_unacknowledged(conn: Connection, older_than: dt.datetime) -> list[int]:
    """Sweep files sent before `older_than` with nothing back.

    Returns the ids so the caller can alert. Deliberately does not alert
    itself: this module does no I/O beyond the database, and what counts as
    urgent depends on how close Gate Closure is.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE outbound_file
                  SET state = %s
                WHERE state = %s AND sent_at < %s
            RETURNING id""",
            (states.UNACKNOWLEDGED, states.SENT, older_than),
        )
        return [row[0] for row in cur.fetchall()]


def find_file_by_filename(conn: Connection, filename: str) -> int | None:
    """Correlate feedback back to the file that caused it.

    E0281 returns our own filename and sequence number (N0301, N0198), which
    is a more reliable key than the reference code: the reference code is ours
    to choose and may repeat across notifications, the filename is unique
    across all central systems within a month (IDD 2.2.5).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM outbound_file WHERE filename = %s", (filename,))
        row = cur.fetchone()
        return row[0] if row else None


def accept_notification(conn: Connection, notification_id: int) -> None:
    """The whole notification passed validation, periods included."""
    _move_item(conn, "notification", notification_id, states.ACCEPTED)
    with conn.cursor() as cur:
        cur.execute(
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
    _move_item(conn, "notification", notification_id, states.REJECTED, reason)
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE notification_period
                  SET state = %s, rejection_reason = %s
                WHERE notification_id = %s AND state = %s""",
            (states.REJECTED, reason[:80], notification_id, states.SUBMITTED),
        )
        for period, period_reason in (periods or {}).items():
            cur.execute(
                """UPDATE notification_period
                      SET rejection_reason = %s
                    WHERE notification_id = %s AND settlement_period = %s""",
                (period_reason[:80], notification_id, period),
            )


def submit_items(conn: Connection, table: str, outbound_file_id: int) -> None:
    """Move every item in a file from PENDING to SUBMITTED.

    Called once the file is sent. `table` is one of notification, sev, wman,
    delivered_volume -- they share the state vocabulary because they share the
    lifecycle.
    """
    if table not in _ITEM_TABLES:
        raise ValueError(f"not an item table: {table}")
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE {table} SET state = %s
                 WHERE outbound_file_id = %s AND state = %s""",
            (states.SUBMITTED, outbound_file_id, states.PENDING),
        )


_ITEM_TABLES = frozenset({"notification", "sev", "wman", "delivered_volume"})


def _file_state(conn: Connection, file_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM outbound_file WHERE id = %s", (file_id,))
        row = cur.fetchone()
        if row is None:
            raise states.TransitionError(f"no outbound file {file_id}")
        return row[0]


def _move_file(conn: Connection, file_id: int, target: str, sent_at: bool = False) -> None:
    current = _file_state(conn, file_id)
    states.check_file_transition(current, target)
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE outbound_file
                   SET state = %s{', sent_at = now()' if sent_at else ''}
                 WHERE id = %s AND state = %s""",
            (target, file_id, current),
        )
        _expect_one(cur, file_id, current, target)


def _move_item(
    conn: Connection, table: str, item_id: int, target: str, reason: str | None = None
) -> None:
    if table not in _ITEM_TABLES:
        raise ValueError(f"not an item table: {table}")
    with conn.cursor() as cur:
        cur.execute(f"SELECT state FROM {table} WHERE id = %s", (item_id,))
        row = cur.fetchone()
        if row is None:
            raise states.TransitionError(f"no {table} {item_id}")
        current = row[0]
        states.check_item_transition(current, target)
        cur.execute(
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
    Returns a list because nothing currently enforces uniqueness -- the caller
    decides what an ambiguous match means.
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
    effect from a period, not from the start of the day, so the accepted
    profile may be shorter than the one submitted.
    """
    conn.execute(
        """UPDATE notification
              SET transaction_id = %s, first_effective_period = %s
            WHERE id = %s""",
        (transaction_id, first_effective_period, notification_id),
    )


def reject_wman(
    conn: Connection,
    settlement_date: dt.date,
    settlement_period: int,
    reason: str,
    bmu_id: str | None = None,
) -> int:
    """Reject wholesale market activity rows. Returns the number affected.

    `bmu_id` None rejects every unit in that period, which is what a
    period-level exception means.
    """
    if bmu_id is None:
        cur = conn.execute(
            """UPDATE wman SET state = %s, rejection_reason = %s
                WHERE settlement_date = %s AND settlement_period = %s
                  AND state = %s""",
            (states.REJECTED, reason[:80], settlement_date, settlement_period,
             states.SUBMITTED),
        )
    else:
        cur = conn.execute(
            """UPDATE wman SET state = %s, rejection_reason = %s
                WHERE settlement_date = %s AND settlement_period = %s
                  AND bmu_id = %s AND state = %s""",
            (states.REJECTED, reason[:80], settlement_date, settlement_period,
             bmu_id, states.SUBMITTED),
        )
    return cur.rowcount