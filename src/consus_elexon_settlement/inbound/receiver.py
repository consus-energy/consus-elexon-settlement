"""Receiving files: archive, record, route, acknowledge.

The mirror of outbound.sender. The router decides what a file means; this
decides what happens around that -- archiving the bytes, writing down that it
arrived, and sending the acknowledgement back.

The order is deliberate and not interchangeable:

    1. archive the raw bytes
    2. insert the inbound_file row
    3. route (parse, dispatch to handler)
    4. record the parse result
    5. send the acknowledgement
    6. record that the acknowledgement went

Steps 1 and 2 happen before parsing so that a file which crashes the parser
still leaves evidence it arrived. That is precisely the case where evidence
matters most, and it is the one an audit trail built after parsing would miss.

Step 5 comes after 4 so that if sending the acknowledgement fails, we know
what we would have said. Elexon will resend; we will have the record.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .. import db
from ..archive import Archive, AlreadyArchived, object_key
from ..idd.file import FileError
from ..outbound.transport import Transport
from .router import Received, Router


@dataclass(frozen=True)
class Collected:
    """What happened to one received file."""

    filename: str
    received: Received | None
    archived: bool
    acknowledged: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.received is not None and self.received.handled and self.acknowledged


class Receiver:
    """Archives, records, routes and acknowledges inbound files."""

    def __init__(
        self,
        connect,
        archive: Archive,
        router: Router,
        transport: Transport,
        response_name,
    ) -> None:
        self._connect = connect
        self._archive = archive
        self._router = router
        self._transport = transport
        self._response_name = response_name

    def collect(self) -> list[Collected]:
        """Process every waiting file.

        One failure does not stop the rest. A malformed file must not prevent
        the next one being read, because that next one may be a rejection
        needing action before Gate Closure.
        """
        return [
            self.receive_one(filename, payload)
            for filename, payload in self._transport.collect()
        ]

    def receive_one(self, filename: str, payload: bytes) -> Collected:
        received_at = dt.datetime.now(dt.timezone.utc)

        # 1 and 2: evidence first, interpretation second.
        try:
            key = object_key("inbound", received_at, filename, 0)
            uri = self._archive.put(key, payload)
        except AlreadyArchived:
            # A resend of a file we already hold. The bytes are already
            # evidence; carry on and let the router decide what it means.
            uri = ""
        except Exception as exc:  # noqa: BLE001
            return Collected(filename, None, archived=False,
                             acknowledged=False, error=f"archive failed: {exc}")

        with self._connect() as conn:
            db.record_inbound(conn, filename, uri, received_at)

        # 3: parse and dispatch.
        try:
            received = self._router.receive(payload, filename, received_at)
        except FileError as exc:
            # The header could not be read, so there is nobody to reply to.
            # Recorded, then left for a human: an unreadable header is either
            # corruption in transit or a file that is not ours.
            with self._connect() as conn:
                db.record_parse_result(
                    conn, filename, None, None, None, None,
                    parse_state="PARSE_FAILED", parse_error=str(exc),
                    response_code=exc.response_code or 1,
                )
            return Collected(filename, None, archived=True,
                             acknowledged=False, error=str(exc))

        # 4: what we made of it.
        header = received.header
        with self._connect() as conn:
            db.record_parse_result(
                conn,
                filename=filename,
                file_type=header.file_type if header else None,
                from_role_code=header.from_role_code if header else None,
                to_role_code=header.to_role_code if header else None,
                sequence_number=header.sequence_number if header else None,
                parse_state="PARSED" if received.parsed else "PARSE_FAILED",
                parse_error=str(received.error) if received.error else None,
                response_code=received.response_code,
            )
            db.record_handled(
                conn, filename,
                str(received.handler_error) if received.handler_error else None,
            )

        # 5 and 6: reply, then note that we did.
        try:
            self._transport.send(self._response_name(filename), received.response)
        except Exception as exc:  # noqa: BLE001
            return Collected(filename, received, archived=True,
                             acknowledged=False, error=f"ack failed: {exc}")

        with self._connect() as conn:
            db.record_ack_sent(conn, filename)

        return Collected(filename, received, archived=True, acknowledged=True)