"""Building and parsing IDD files: header, body, footer.

One code path for all 29 ECVAA flows. The structure comes from the generated
spec, the field encoding from fields.py, and the footer checksum from
checksum.py. Adding a flow is a spec entry, not a module.

File shape (IDD Part 1 sections 2.2.1-2.2.3):

    AAA|E0041001|D|20260810093000|EN|CONSUSEN|EC|ECVAA|1|TEST1|<LF>
    EDN|ECVNAA0001|KEY0000001|ECVNAA0001|REF0000001|20260811||<LF>
    CD9|1|12.5|<LF>
    ZZZ|4|829768304|<LF>

A record of n fields carries n+1 separators: there is a trailing '|' before
the line feed and it is part of the record for checksum purposes.

Parse errors carry the IDD response code that should appear in the ADT record
of our reply (section 2.2.7), so the inbound handler does not have to
re-derive it:

    1  syntax error in header       4  syntax error in body
    5  syntax error in footer       6  incorrect record count
    7  incorrect checksum

That distinction is not cosmetic. Per section 2.2.8, a rejection for codes
1-3 does not consume the sender's sequence number, while 4-7 does.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field as dc_field

from . import fields as f
from .checksum import file_checksum
from .model import DataType, Flow, Record

RECORD_DELIMITER = b"\n"
SEPARATOR = "|"
HEADER_TYPE = "AAA"
FOOTER_TYPE = "ZZZ"

_FILE_TYPE = DataType.parse("text(8)")
_ROLE = DataType.parse("text(2)")
_PARTICIPANT = DataType.parse("text(8)")
_SEQUENCE = DataType.parse("integer(9)")
_TEST_FLAG = DataType.parse("text(4)")
_DATETIME = DataType.parse("datetime")

MAX_SEQUENCE = 999_999_999


class FileError(ValueError):
    """A file cannot be built, or an inbound file is malformed."""

    def __init__(self, message: str, response_code: int | None = None):
        super().__init__(message)
        self.response_code = response_code


@dataclass(frozen=True)
class Header:
    """The AAA record."""

    file_type: str            # E0041001
    message_role: str         # 'D' data, 'R' response
    creation_time: dt.datetime
    from_role_code: str
    from_participant_id: str
    to_role_code: str
    to_participant_id: str
    sequence_number: int
    test_flag: str | None = None   # 'OPER' or omitted for operational

    def __post_init__(self) -> None:
        if self.message_role not in ("D", "R"):
            raise FileError(f"message role must be 'D' or 'R', got {self.message_role!r}", 1)
        if not 0 <= self.sequence_number <= MAX_SEQUENCE:
            raise FileError(f"sequence number out of range: {self.sequence_number}", 1)

    def to_record(self) -> str:
        values = [
            f.encode(self.file_type, _FILE_TYPE),
            self.message_role,
            f.encode(self.creation_time, _DATETIME),
            f.encode(self.from_role_code, _ROLE),
            f.encode(self.from_participant_id, _PARTICIPANT),
            f.encode(self.to_role_code, _ROLE),
            f.encode(self.to_participant_id, _PARTICIPANT),
            f.encode(self.sequence_number, _SEQUENCE),
            "" if self.test_flag is None else f.encode(self.test_flag, _TEST_FLAG),
        ]
        return _join(HEADER_TYPE, values)

    @classmethod
    def from_record(cls, record: str) -> "Header":
        parts = _split(record, HEADER_TYPE, expected=9, code=1)
        try:
            return cls(
                file_type=f.parse(parts[0], _FILE_TYPE),           # type: ignore[arg-type]
                message_role=parts[1],
                creation_time=f.parse(parts[2], _DATETIME),        # type: ignore[arg-type]
                from_role_code=f.parse(parts[3], _ROLE),           # type: ignore[arg-type]
                from_participant_id=f.parse(parts[4], _PARTICIPANT),  # type: ignore[arg-type]
                to_role_code=f.parse(parts[5], _ROLE),             # type: ignore[arg-type]
                to_participant_id=f.parse(parts[6], _PARTICIPANT),  # type: ignore[arg-type]
                sequence_number=f.parse(parts[7], _SEQUENCE),      # type: ignore[arg-type]
                test_flag=parts[8] or None,
            )
        except f.FieldError as exc:
            raise FileError(f"header: {exc}", 1) from exc

    def response_header(self, our_role: str, our_participant: str) -> "Header":
        """The header of our ADT reply to this file.

        IDD 2.2.7: the header as received, with from/to reversed and message
        role set to response. Creation time and sequence number are those of
        the message being replied to, NOT ours — this is why replies do not
        consume one of our sequence numbers.
        """
        return Header(
            file_type=self.file_type,
            message_role="R",
            creation_time=self.creation_time,
            from_role_code=our_role,
            from_participant_id=our_participant,
            to_role_code=self.from_role_code,
            to_participant_id=self.from_participant_id,
            sequence_number=self.sequence_number,
            test_flag=self.test_flag,
        )


@dataclass
class Node:
    """One occurrence of a record type, with its values and nested records."""

    record_type: str
    values: dict[str, object] = dc_field(default_factory=dict)
    children: list["Node"] = dc_field(default_factory=list)

    def of_type(self, record_type: str) -> list["Node"]:
        return [c for c in self.children if c.record_type == record_type]


def _join(record_type: str, values: list[str]) -> str:
    # n fields -> n+1 separators, including a trailing one.
    return record_type + SEPARATOR + SEPARATOR.join(values) + SEPARATOR


def _split(record: str, expected_type: str, expected: int, code: int) -> list[str]:
    if not record.startswith(expected_type + SEPARATOR):
        raise FileError(f"expected a {expected_type} record, got {record[:3]!r}", code)
    if not record.endswith(SEPARATOR):
        raise FileError(f"{expected_type} record has no trailing separator", code)
    parts = record.split(SEPARATOR)[1:-1]
    if len(parts) != expected:
        raise FileError(
            f"{expected_type} record has {len(parts)} fields, expected {expected}", code
        )
    return parts


# --- build ------------------------------------------------------------------

def _build_record(node: Node, spec: Record) -> list[str]:
    if node.record_type != spec.record_type:
        raise FileError(f"expected {spec.record_type}, got {node.record_type}")

    unknown = set(node.values) - {x.item_id for x in spec.fields}
    if unknown:
        raise FileError(f"{spec.record_type}: unknown items {sorted(unknown)}")

    try:
        values = [f.encode_field(x, node.values.get(x.item_id)) for x in spec.fields]
    except f.FieldError as exc:
        raise FileError(f"{spec.record_type}: {exc}") from exc

    lines = [_join(spec.record_type, values)]
    lines += _build_group(node.children, spec.children, spec.record_type)
    return lines


def _build_group(nodes: list[Node], specs: tuple[Record, ...], parent: str) -> list[str]:
    remaining = deque(nodes)
    lines: list[str] = []

    for spec in specs:
        matched = []
        while remaining and remaining[0].record_type == spec.record_type:
            matched.append(remaining.popleft())

        if not spec.cardinality.permits(len(matched)):
            raise FileError(
                f"{parent}: {spec.record_type} appears {len(matched)} times, "
                f"spec allows {spec.cardinality}"
            )
        for node in matched:
            lines += _build_record(node, spec)

    if remaining:
        types = sorted({n.record_type for n in remaining})
        raise FileError(f"{parent}: unexpected or out-of-order records {types}")
    return lines


def build(flow: Flow, header: Header, body: list[Node]) -> bytes:
    """Render a flow to file bytes, including footer.

    Records and data items must appear in the order the spec states
    (IDD 2.2.4); out-of-order input is an error, not something to sort.
    """
    if header.file_type != flow.file_type:
        raise FileError(
            f"header file type {header.file_type} does not match flow {flow.file_type}"
        )

    records = [header.to_record()]
    records += _build_group(body, flow.records, flow.file_type)

    encoded = [r.encode("ascii") for r in records]
    footer = _join(FOOTER_TYPE, [str(len(encoded) + 1), str(file_checksum(encoded))])

    return RECORD_DELIMITER.join(encoded + [footer.encode("ascii")]) + RECORD_DELIMITER


# --- parse ------------------------------------------------------------------

def _parse_record(line: str, spec: Record) -> Node:
    parts = _split(line, spec.record_type, expected=len(spec.fields), code=4)
    values: dict[str, object] = {}
    for item, raw in zip(spec.fields, parts):
        try:
            parsed = f.parse_field(item, raw)
        except f.FieldError as exc:
            raise FileError(f"{spec.record_type}: {exc}", 4) from exc
        if parsed is not None:
            values[item.item_id] = parsed
    return Node(record_type=spec.record_type, values=values)


def _parse_group(lines: deque[str], specs: tuple[Record, ...], parent: str) -> list[Node]:
    nodes: list[Node] = []

    for spec in specs:
        count = 0
        while lines and lines[0].startswith(spec.record_type + SEPARATOR):
            node = _parse_record(lines.popleft(), spec)
            node.children = _parse_group(lines, spec.children, spec.record_type)
            nodes.append(node)
            count += 1

        if not spec.cardinality.permits(count):
            raise FileError(
                f"{parent}: {spec.record_type} appears {count} times, "
                f"spec allows {spec.cardinality}",
                4,
            )
    return nodes


def parse(payload: bytes, flow: Flow) -> tuple[Header, list[Node]]:
    """Parse file bytes against a flow spec, verifying count and checksum."""
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FileError("file contains non-ASCII bytes", 4) from exc

    records = text.split("\n")
    if records and records[-1] == "":
        records.pop()
    if len(records) < 2:
        raise FileError("file has no header or no footer", 1)

    header = Header.from_record(records[0])
    if header.file_type != flow.file_type:
        raise FileError(
            f"file type {header.file_type} does not match expected {flow.file_type}", 1
        )

    footer = records[-1]
    if not footer.startswith(FOOTER_TYPE + SEPARATOR):
        raise FileError("last record is not a ZZZ footer", 5)
    count_text, checksum_text = _split(footer, FOOTER_TYPE, expected=2, code=5)

    try:
        declared_count = int(count_text)
        declared_checksum = int(checksum_text)
    except ValueError as exc:
        raise FileError(f"malformed footer: {footer!r}", 5) from exc

    # Count first: a truncated file can produce a checksum collision, because
    # XOR is self-inverse and cancels duplicate records.
    if declared_count != len(records):
        raise FileError(
            f"record count is {declared_count}, file has {len(records)} records", 6
        )

    actual = file_checksum([r.encode("ascii") for r in records[:-1]])
    if actual != declared_checksum:
        raise FileError(f"checksum is {declared_checksum}, computed {actual}", 7)

    lines = deque(records[1:-1])
    body = _parse_group(lines, flow.records, flow.file_type)
    if lines:
        raise FileError(f"unexpected trailing records: {list(lines)[:3]}", 4)

    return header, body