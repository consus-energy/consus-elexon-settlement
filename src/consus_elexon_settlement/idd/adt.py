"""ADT acknowledgement records: our reply to every file we receive.

IDD Part 1 section 2.2.7. On receiving a data file we return a response file:
the header as received with from/to reversed and message role 'R', one ADT
record per problem found (or one with code 0 if none), then a ZZZ footer.

    AAA|E0091001|R|20260810093000|EN|CONSUSEN|EC|ECVAA|17|TEST1|
    ADT|20260810093015|20260810093016|EN0000000001|0||
    ZZZ|3|1234567890|

This is a receipt, not an acceptance. Business validation happens later and
comes back as a separate flow -- E0091 rejection, E0281 acceptance.

Response files do not consume one of our sequence numbers: the header carries
the sequence number of the message being replied to (2.2.7). Nothing here
touches the channel counter.

The ADT record is defined in IDD prose rather than the flow spreadsheet, so
it cannot come from the generated spec. That is why it lives here and not in
file.py, which is spec-driven throughout.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from . import fields as f
from .checksum import file_checksum
from .file import FOOTER_TYPE, RECORD_DELIMITER, Header, _join, _split, FileError
from .model import DataType

ADT_TYPE = "ADT"

_DATETIME = DataType.parse("datetime")
_FILENAME = DataType.parse("text(14)")
_CODE = DataType.parse("integer(3)")
_DATA = DataType.parse("text(80)")

# IDD 2.2.7 response codes. Section 2.2.8: codes 1-3 do NOT consume the
# sender's sequence number, so a corrected file reuses it. Codes 4-7 do.
OK = 0
SYNTAX_ERROR_HEADER = 1
UNEXPECTED_FILE_TYPE = 2
DUPLICATE_OR_OUT_OF_SEQUENCE = 3
SYNTAX_ERROR_BODY = 4
SYNTAX_ERROR_FOOTER = 5
INCORRECT_RECORD_COUNT = 6
INCORRECT_CHECKSUM = 7

DESCRIPTIONS = {
    OK: "Accepted",
    SYNTAX_ERROR_HEADER: "Syntax error in header",
    UNEXPECTED_FILE_TYPE: "Unexpected file type",
    DUPLICATE_OR_OUT_OF_SEQUENCE: "Duplicate or out of sequence",
    SYNTAX_ERROR_BODY: "Syntax error in body",
    SYNTAX_ERROR_FOOTER: "Syntax error in footer",
    INCORRECT_RECORD_COUNT: "Incorrect record count",
    INCORRECT_CHECKSUM: "Incorrect checksum",
}

# Codes where the sender keeps its sequence number and resends under the same
# one. Recorded here because the inbound handler needs it when the roles are
# reversed and we are the one being NACKed.
PRESERVES_SEQUENCE = frozenset({SYNTAX_ERROR_HEADER,
                                UNEXPECTED_FILE_TYPE,
                                DUPLICATE_OR_OUT_OF_SEQUENCE})


@dataclass(frozen=True)
class Acknowledgement:
    """One ADT record: the outcome of receiving a file."""

    received_time: dt.datetime
    response_time: dt.datetime
    filename: str
    response_code: int
    response_data: str | None = None

    def __post_init__(self) -> None:
        if self.response_code not in DESCRIPTIONS:
            raise FileError(f"unknown ADT response code: {self.response_code}")

    @property
    def accepted(self) -> bool:
        return self.response_code == OK

    @property
    def preserves_sequence(self) -> bool:
        """True when the sender may reuse its sequence number (IDD 2.2.8)."""
        return self.response_code in PRESERVES_SEQUENCE

    def to_record(self) -> str:
        return _join(ADT_TYPE, [
            f.encode(self.received_time, _DATETIME),
            f.encode(self.response_time, _DATETIME),
            f.encode(self.filename, _FILENAME),
            f.encode(self.response_code, _CODE),
            "" if self.response_data is None else f.encode(self.response_data, _DATA),
        ])

    @classmethod
    def from_record(cls, record: str) -> "Acknowledgement":
        parts = _split(record, ADT_TYPE, expected=5, code=SYNTAX_ERROR_BODY)
        try:
            return cls(
                received_time=f.parse(parts[0], _DATETIME),      # type: ignore[arg-type]
                response_time=f.parse(parts[1], _DATETIME),      # type: ignore[arg-type]
                filename=f.parse(parts[2], _FILENAME),           # type: ignore[arg-type]
                response_code=f.parse(parts[3], _CODE),          # type: ignore[arg-type]
                response_data=parts[4] or None,
            )
        except f.FieldError as exc:
            raise FileError(f"ADT: {exc}", SYNTAX_ERROR_BODY) from exc


def build_response(
    received: Header,
    our_role: str,
    our_participant: str,
    acknowledgements: list[Acknowledgement],
) -> bytes:
    """Render our response file to a received data file.

    `received` is the header of the file we are replying to; it supplies the
    creation time and sequence number, which are echoed rather than reissued.
    """
    if not acknowledgements:
        raise FileError("a response file needs at least one ADT record")

    header = received.response_header(our_role, our_participant)
    records = [header.to_record()] + [a.to_record() for a in acknowledgements]

    encoded = [r.encode("ascii") for r in records]
    footer = _join(FOOTER_TYPE, [str(len(encoded) + 1), str(file_checksum(encoded))])
    return RECORD_DELIMITER.join(encoded + [footer.encode("ascii")]) + RECORD_DELIMITER


def acknowledge(
    received: Header,
    filename: str,
    received_time: dt.datetime,
    our_role: str,
    our_participant: str,
    error: FileError | None = None,
    now: dt.datetime | None = None,
) -> bytes:
    """Build the response for one received file, accepted or rejected.

    The common case: parse succeeded or raised, and we reply accordingly.
    Multiple ADT records are only needed when a single file carries several
    distinct problems, which build_response supports directly.
    """
    ack = Acknowledgement(
        received_time=received_time,
        response_time=now or dt.datetime.now(dt.timezone.utc),
        filename=filename,
        response_code=OK if error is None else (error.response_code or SYNTAX_ERROR_BODY),
        response_data=None if error is None else str(error)[:80],
    )
    return build_response(received, our_role, our_participant, [ack])