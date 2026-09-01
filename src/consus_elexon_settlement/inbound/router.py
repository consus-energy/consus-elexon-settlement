"""Dispatching received files to their handlers.

Every file we receive gets three things done to it, in this order:

    1. parsed against its flow spec
    2. handed to the handler registered for that file type
    3. acknowledged with an ADT response

Step 3 happens whether or not steps 1 and 2 succeeded. A file we cannot parse
still needs a reply, and the response code is how the sender learns why
(IDD 2.2.7). Silently dropping an unparseable file is the worst outcome: the
sender believes it was delivered.

The handler runs before the acknowledgement but its failure does not change
the response code. Receipt acknowledgement is syntactic -- it says the file
arrived and parsed, not that we agree with it or that our database accepted
it. Conflating the two would tell ECVAA a file was bad when in fact our own
storage was down.

We hold two identities: 'VT' plus our Party Id for WMAN and the SVAA flows,
'EN' plus our ECVNA Id for ECVNs. Inbound files are addressed to one or the
other, and the reply must come from whichever was addressed. Replying as the
wrong identity would be rejected, and would also consume nothing sensible.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from ..idd import adt
from ..idd.file import FileError, Header, Node, parse
from ..idd.model import Flow, Spec


class Handler(Protocol):
    """Handles one parsed inbound file.

    Returning normally means handled. Raising means the file was understood
    but could not be actioned -- which is recorded, but does not change the
    acknowledgement.
    """

    def __call__(self, header: Header, body: list[Node]) -> None: ...


@dataclass(frozen=True)
class Received:
    """The outcome of receiving one file.

    Returned rather than acted on, so the caller decides what to do with the
    response bytes and how to alert on failure. The router does no I/O.
    """

    filename: str
    header: Header | None
    response: bytes
    response_code: int
    error: FileError | None = None
    handler_error: Exception | None = None

    @property
    def parsed(self) -> bool:
        return self.error is None

    @property
    def handled(self) -> bool:
        """Parsed and the handler completed. A file can be parsed but not
        handled, which is our failure, not the sender's."""
        return self.parsed and self.handler_error is None


class Router:
    """File type to handler, with parsing and acknowledgement around it."""

    def __init__(self, identities: dict[str, str]) -> None:
        """`identities` maps role code to participant id, for example
        {"VT": "CONSUSVT", "EN": "CONSUSEN"}.

        A file addressed to a role and participant we do not hold is not ours.
        We reject it rather than handling it: acting on someone else's file
        would be worse than refusing one of our own.
        """
        if not identities:
            raise ValueError("at least one identity is required")
        self.identities = dict(identities)
        self._specs: list[Spec] = []
        self._handlers: dict[str, Handler] = {}

    def add_spec(self, spec: Spec) -> None:
        """Register a central system's flows.

        Order matters only if two systems define the same file type, which
        they do not -- file ids are unique across ECVAA, SVAA, CRA and SAA.
        """
        self._specs.append(spec)

    def register(self, file_type: str, handler: Handler) -> None:
        if file_type in self._handlers:
            raise ValueError(f"handler already registered for {file_type}")
        self._handlers[file_type] = handler

    def flow(self, file_type: str) -> Flow | None:
        for spec in self._specs:
            if file_type in spec.flows:
                return spec.flows[file_type]
        return None

    def addressed_to_us(self, header: Header) -> bool:
        return self.identities.get(header.to_role) == header.to_participant

    def receive(
        self,
        payload: bytes,
        filename: str,
        received_time: dt.datetime | None = None,
    ) -> Received:
        """Parse, dispatch and acknowledge one received file.

        Raises FileError only when the header itself cannot be read, because
        without a header there is nobody to address a reply to. Every other
        failure comes back on Received with a response to send.
        """
        received_time = received_time or _now()

        # The header is read before the flow is known, because the file type
        # is in the header. Response code 1 is 'syntax error in header', but
        # we cannot send it: building a reply needs the header we just failed
        # to read. This is the one case the caller must handle itself.
        try:
            first_line = payload.decode("ascii").split("\n", 1)[0]
            header = Header.from_record(first_line)
        except (FileError, UnicodeDecodeError, IndexError) as exc:
            raise FileError(
                f"unreadable header in {filename}: {exc}", adt.SYNTAX_ERROR_HEADER
            ) from exc

        if not self.addressed_to_us(header):
            return self._reject(
                header, filename, received_time,
                FileError(
                    f"addressed to {header.to_role}/{header.to_participant}, not us",
                    adt.UNEXPECTED_FILE_TYPE,
                ),
                reply_as=None,
            )

        flow = self.flow(header.file_type)
        if flow is None:
            return self._reject(
                header, filename, received_time,
                FileError(
                    f"unknown file type {header.file_type}", adt.UNEXPECTED_FILE_TYPE
                ),
            )

        try:
            _, body = parse(payload, flow)
        except FileError as exc:
            return self._reject(header, filename, received_time, exc)

        # Parsed. Hand to the handler if one is registered. A file type we
        # know but do not handle is not an error at this layer: it may be a
        # report we reconcile later rather than act on now.
        handler_error: Exception | None = None
        handler = self._handlers.get(header.file_type)
        if handler is not None:
            try:
                handler(header, body)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                handler_error = exc

        return Received(
            filename=filename,
            header=header,
            response=adt.acknowledge(
                header, filename, received_time,
                header.to_role, header.to_participant,
            ),
            response_code=adt.OK,
            handler_error=handler_error,
        )

    def _reject(
        self,
        header: Header,
        filename: str,
        received_time: dt.datetime,
        error: FileError,
        reply_as: tuple[str, str] | None = (),  # type: ignore[assignment]
    ) -> Received:
        """Build a rejection response.

        `reply_as` defaults to the identity the file was addressed to. Passing
        None means we are not that participant, in which case we reply as our
        first identity: the sender has misaddressed the file and needs telling,
        and there is no correct identity to use.
        """
        if reply_as == ():
            role, participant = header.to_role, header.to_participant
        elif reply_as is None:
            role, participant = next(iter(self.identities.items()))
        else:
            role, participant = reply_as

        return Received(
            filename=filename,
            header=header,
            response=adt.acknowledge(
                header, filename, received_time, role, participant, error=error
            ),
            response_code=error.response_code or adt.SYNTAX_ERROR_BODY,
            error=error,
        )


def _now() -> dt.datetime:
    """GMT, naive. IDD 2.2.2: header times are GMT, and the field encoder
    formats naive datetimes."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)