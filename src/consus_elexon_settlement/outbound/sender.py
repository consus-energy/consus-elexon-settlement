"""Outbound: domain object to bytes on the wire.

The mirror of inbound.receiver. One method, four steps, in an order that is
not negotiable:

    1. reserve  -- allocate the sequence number and insert the file row
    2. build    -- render the bytes, compute the checksum
    3. archive  -- write the bytes immutably, record checksum and record count
    4. send     -- hand to transport

Steps 1 to 3 happen once. Step 4 may happen many times. That is the whole
discipline: BUILD ONCE, SEND MANY. A retry re-sends the archived bytes under
the original sequence number. Regenerating would allocate a second number and
leave a permanent gap at the first, and ECVAA stops processing at a gap
(IDD 2.2.8). A gap cannot be corrected retrospectively -- it is fixed by
agreement with Elexon, not by code. See ADR-0002.

Two identities. WMAN and the SVAA flows go out as the VTP ('VT' plus our Party
Id); ECVNs go out as the ECVN Agent ('EN' plus our ECVNA Id), because only an
ECVNA may submit one. Separate channels, separate counters. The caller picks
the identity by passing the right channel; nothing here infers it, because an
inference that is wrong corrupts both sequences silently. See ADR-0004.

Transport is a protocol, not a dependency. Encryption sits behind the same
interface, so whether XSec is in the path is invisible from here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .. import db, states
from ..archive import Archive, object_key
from ..idd.file import Header, Node, build
from ..idd.model import Flow
from .transport import Transport


class SendError(RuntimeError):
    """Transport failed. The file is intact and retryable."""


@dataclass(frozen=True)
class Sent:
    """The outcome of sending one file."""

    file_id: int
    filename: str
    sequence_number: int
    payload: bytes
    gcs_uri: str
    delivered: bool
    error: str | None = None


class Sender:
    """Builds, archives and sends outbound files.

    Holds a connection factory rather than a connection: this runs from a
    scheduler that may be long-lived, and a connection held open for hours is
    a connection that will be dead at Gate Closure.
    """

    def __init__(
        self,
        connect,
        archive: Archive,
        transport: Transport,
    ) -> None:
        self._connect = connect
        self._archive = archive
        self._transport = transport

    def send(
        self,
        channel: db.Channel,
        flow: Flow,
        body: list[Node],
        creation_time: dt.datetime | None = None,
        supersedes: int | None = None,
    ) -> Sent:
        """Reserve, build, archive and send one file.

        `supersedes` is for a correction after a header-level NACK (response
        codes 1 to 3). Those do not consume the sender's sequence number
        (IDD 2.2.8), so the corrected file reuses it and the original is
        marked SUPERSEDED. Any other NACK does consume the number and the
        correction takes a new one, which is the default path.
        """
        creation_time = creation_time or _now()
        if creation_time.tzinfo is None:
            # fields.py rejects naive datetimes for the same reason: a naive
            # value is an assumption waiting to be wrong on a clock-change day.
            raise SendError("creation_time must be timezone-aware")

        with self._connect() as conn:
            reserved = db.reserve_file(
                conn,
                channel=channel,
                file_type=flow.file_type,
                message_role="D",
                creation_time=creation_time,
                supersedes=supersedes,
            )

            header = Header(
                file_type=flow.file_type,
                message_role="D",
                creation_time=creation_time,
                from_role_code=channel.from_role_code,
                from_participant_id=channel.from_participant_id,
                to_role_code=channel.to_role_code,
                to_participant_id=channel.to_participant_id,
                sequence_number=reserved.sequence_number,
                # The test flag is a property of the channel, not a parameter.
                # An operational file cannot be built on a test channel, which
                # is the structural guarantee rather than a config check
                # somebody can forget. See ADR-0010.
                test_flag=channel.test_flag or None,
            )

            payload = build(flow, header, body)

            key = object_key("outbound", creation_time, reserved.filename, reserved.id)
            uri = self._archive.put(key, payload)

            db.record_built(
                conn,
                file_id=reserved.id,
                checksum=_checksum_of(payload),
                record_count=_record_count_of(payload),
                gcs_uri=uri,
            )

        # Sending happens outside the reserve/build transaction. If transport
        # hangs -- and with XSec in the path it can, since encryption is an
        # asynchronous file handover -- the file is already archived and
        # recoverable. Holding the transaction open across a network call
        # would block the sequence counter for every other file on the
        # channel.
        return self._deliver(reserved.id, reserved.filename,
                             reserved.sequence_number, payload, uri)

    def retry(self, file_id: int) -> Sent:
        """Re-send an already-built file.

        Reads the bytes back from the archive rather than rebuilding. The
        archive is authoritative: what it holds is what went on the wire, and
        a rebuild could differ if any input changed in between.
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT filename, sequence_number, gcs_uri, state
                     FROM outbound_file WHERE id = %s""",
                (file_id,),
            ).fetchone()

        if row is None:
            raise SendError(f"no outbound file {file_id}")
        filename, sequence_number, uri, state = row
        if state not in (states.BUILT, states.SEND_FAILED, states.SENT):
            raise SendError(f"file {file_id} is {state} and is not retryable")
        if uri is None:
            raise SendError(f"file {file_id} has no archived bytes")

        payload = self._archive.get(uri)
        return self._deliver(file_id, filename, sequence_number, payload, uri)

    def _deliver(
        self, file_id: int, filename: str, sequence_number: int,
        payload: bytes, uri: str,
    ) -> Sent:
        try:
            self._transport.send(filename, payload)
        except Exception as exc:  # noqa: BLE001 - recorded, then reported
            with self._connect() as conn:
                db.record_send_failed(conn, file_id, str(exc))
            return Sent(file_id, filename, sequence_number, payload, uri,
                        delivered=False, error=str(exc))

        with self._connect() as conn:
            db.record_sent(conn, file_id)
        return Sent(file_id, filename, sequence_number, payload, uri, delivered=True)


def _footer_fields(payload: bytes) -> list[str]:
    """The ZZZ trailer, split into fields.

    Read back from the rendered bytes rather than recomputed. What the footer
    says is what the recipient will verify, so recording anything else would
    record a number nobody checks.

    Tolerant of the record delimiter: rstrip removes a trailing newline in
    either LF or CRLF form, and rsplit finds the last line regardless. The
    IDD gives LF (2.2.4), but the sample file supplied by Elexon uses CRLF,
    and that question is not yet settled -- so nothing here depends on it.
    """
    last_line = payload.rstrip(b"\r\n").rsplit(b"\n", 1)[-1]
    return last_line.decode("ascii").rstrip("\r").split("|")


def _checksum_of(payload: bytes) -> int:
    return int(_footer_fields(payload)[2])


def _record_count_of(payload: bytes) -> int:
    """The record count from the footer, including header and trailer.

    Previously counted newlines in the payload, which assumed both a trailing
    delimiter and an LF-only file. Reading the footer removes both
    assumptions, and records the number we actually declared rather than a
    second opinion about it.
    """
    return int(_footer_fields(payload)[1])


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)