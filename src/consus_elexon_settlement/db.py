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