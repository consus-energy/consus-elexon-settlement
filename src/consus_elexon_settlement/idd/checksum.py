"""IDD Part 1 s2.2.2 file checksum.

Pure functions. No I/O.

Algorithm (IDD Part 1 v64.0, section 2.2.2):
    - initialise to zero
    - consider each record in turn, INCLUDING the AAA header, EXCLUDING the
      ZZZ trailer
    - break each record into four-byte sections, EXCLUDING the end-of-line
      character, padded with nulls if required, and XOR them into the checksum
    - the result is a 32-bit unsigned value

The IDD pseudocode packs each section big-endian and pads by shifting left,
which is equivalent to right-padding the final section with NUL bytes:

    FOR (j = 0; j < 4; i++, j++)
        IF i < num_chars: value = ((value << 8) + record_buffer[i])
        ELSE:             value = value << 8

A "record" here is the full on-the-wire record text with the trailing field
separator ('|') included and the LF stripped. Per s2.2.3 a record of n fields
carries n+1 separators, so the trailing '|' is part of the record and is
checksummed.
"""

from __future__ import annotations

from typing import Iterable, Sequence

MASK32 = 0xFFFFFFFF
LF = 0x0A
FIELD_SEPARATOR = b"|"


class ChecksumError(ValueError):
    """Record content is not valid input to the checksum."""


def record_checksum(record: bytes) -> int:
    """XOR of the record's big-endian 4-byte sections, NUL-padded."""
    acc = 0
    for i in range(0, len(record), 4):
        acc ^= int.from_bytes(record[i : i + 4].ljust(4, b"\x00"), "big")
    return acc & MASK32


def _validate(record: bytes) -> None:
    if not isinstance(record, (bytes, bytearray)):
        raise ChecksumError(f"record must be bytes, got {type(record).__name__}")
    if LF in record:
        raise ChecksumError("record contains LF; strip the record delimiter first")
    if any(b > 0x7F for b in record):
        raise ChecksumError("record contains non-ASCII bytes")


def file_checksum(records: Iterable[bytes]) -> int:
    """Checksum over header + data records. Do not pass the ZZZ trailer.

    Each record must be bytes with the LF delimiter already stripped.
    """
    acc = 0
    for record in records:
        _validate(record)
        acc ^= record_checksum(record)
    return acc & MASK32


def split_records(payload: bytes) -> list[bytes]:
    """Split file bytes on LF, dropping the trailing empty element.

    Tolerates a missing final LF. Does not strip CR: a CR in the payload is a
    malformed file and will surface as a checksum mismatch rather than being
    silently repaired.
    """
    records = payload.split(b"\n")
    if records and records[-1] == b"":
        records.pop()
    return records


def checksum_of_file(payload: bytes) -> int:
    """Checksum of a complete file's bytes: all records except the last.

    The last record is assumed to be the ZZZ trailer. Used for verifying
    inbound files.
    """
    records = split_records(payload)
    if len(records) < 2:
        raise ChecksumError("file must contain at least a header and a trailer")
    if not records[-1].startswith(b"ZZZ"):
        raise ChecksumError("last record is not a ZZZ trailer")
    return file_checksum(records[:-1])


def verify(payload: bytes, expected: int) -> bool:
    return checksum_of_file(payload) == (expected & MASK32)


def record_count(records: Sequence[bytes]) -> int:
    """ZZZ field 2: count of records including header and footer."""
    return len(records) + 1