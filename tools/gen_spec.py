#!/usr/bin/env python3
"""Generate idd/spec.py from the NETA IDD Part 1 spreadsheet.

The spreadsheet is authoritative for physical file structure (IDD Part 1
section 2.2.4). Transcribing it by hand is a typo away from a NACK 4 with an
opaque error, so we derive the spec mechanically and commit the result. On a
new IDD version, re-run this and review the diff.

    python tools/gen_spec.py docs/IDD_Part1_spreadsheet_v47.xls \\
        --tab ECVAA -o src/consus_elexon_settlement/idd/spec.py

Spreadsheet layout (columns, 0-indexed):

    0   Id                      E0041 | EDN | N0080
    1   type                    F (file type) | R (record type) | D (data item)
    2   flow version / range    '001' for F rows, cardinality for R rows
    3-11 L1..L9                 nesting; 'G' marks a group at that level,
                                '1'/'O'/'N' marks a data item at that level
    12  data type               text(10), decimal(10,3), date, ...
    13  Valid Set
    14  item name / group description
    15  Comments

Nesting is by column position: the level column holding the marker gives the
depth. A record at L2 is a child of the most recent record at L1.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path
from typing import Any, Iterator, NamedTuple

import xlrd
import xlrd.book

# The IDD workbooks carry defined names xlrd cannot evaluate. We only read
# cell values, so skip that parsing step rather than failing to open the file.
xlrd.book.Book.names_epilogue = lambda self: None  # type: ignore[method-assign]

SLUG = re.compile(r"[^a-z0-9]+")


def slug(name: str) -> str:
    """A stable key from an item name, for tabs that omit N-numbers."""
    return SLUG.sub("_", name.lower()).strip("_")


ID, TYPE, RANGE = 0, 1, 2
LEVELS = range(3, 12)  # L1..L9
DATA_TYPE, VALID_SET, NAME, COMMENT = 12, 13, 14, 15


class Row(NamedTuple):
    number: int
    id: str
    type: str
    range: str
    level: int | None
    marker: str
    data_type: str
    valid_set: str
    name: str
    comment: str


def _cell(value: Any) -> str:
    """Normalise a cell to a stripped string.

    xlrd yields numbers as floats, so the cardinality '1' arrives as 1.0 and
    a level marker '1' likewise. Integral floats become plain integers.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_rows(path: Path, tab: str) -> Iterator[Row]:
    book = xlrd.open_workbook(str(path))
    sheet = book.sheet_by_name(tab)

    for index in range(sheet.nrows):
        values = [_cell(c.value) for c in sheet.row(index)]
        values += [""] * (16 - len(values))

        row_type = values[TYPE]
        if row_type not in ("F", "R", "D"):
            continue

        level: int | None = None
        marker = ""
        for column in LEVELS:
            if values[column]:
                level = column - LEVELS.start + 1
                marker = values[column]
                break

        yield Row(
            number=index + 1,
            id=values[ID],
            type=row_type,
            range=values[RANGE],
            level=level,
            marker=marker,
            data_type=values[DATA_TYPE],
            valid_set=values[VALID_SET],
            name=values[NAME],
            comment=values[COMMENT],
        )


class SpecError(ValueError):
    pass


def parse(path: Path, tab: str) -> list[dict]:
    """Build a list of flow dicts. Structure only; no encoding decisions."""
    flows: list[dict] = []
    flow: dict | None = None
    stack: list[tuple[int, dict]] = []  # (level, record)

    for row in read_rows(path, tab):
        if row.type == "F":
            flow = {
                "file_id": row.id,
                "version": row.range,
                "name": row.name,
                "comment": row.comment or None,
                "records": [],
            }
            flows.append(flow)
            stack = []
            continue

        if flow is None:
            raise SpecError(f"row {row.number}: {row.id} appears before any file type")
        if row.level is None:
            raise SpecError(f"row {row.number}: {row.id} has no level marker")

        if row.type == "R":
            if row.marker != "G":
                raise SpecError(
                    f"row {row.number}: record {row.id} marked {row.marker!r}, expected 'G'"
                )
            record = {
                "record_type": row.id,
                "name": row.name,
                "cardinality": row.range or "1",
                "fields": [],
                "children": [],
                "comment": row.comment or None,
            }
            while stack and stack[-1][0] >= row.level:
                stack.pop()
            if stack:
                stack[-1][1]["children"].append(record)
            else:
                flow["records"].append(record)
            stack.append((row.level, record))
            continue

        # row.type == 'D'
        if not stack:
            raise SpecError(f"row {row.number}: item {row.id} outside any record")
        # A data item sits one level deeper than its owning record.
        while stack and stack[-1][0] >= row.level:
            stack.pop()
        if not stack:
            raise SpecError(f"row {row.number}: item {row.id} has no parent record")

        presence = {"1": "M", "O": "O", "N": "N"}.get(row.marker)
        if presence is None:
            raise SpecError(
                f"row {row.number}: item {row.id} has unknown marker {row.marker!r}"
            )

        # The ECVAA, CRA and SAA tabs give every data item an N-number. The
        # SVAA tab gives none of them one: its items are identified by name
        # and position only. Since values are keyed on item_id throughout, we
        # synthesise a stable key from the name for those. Names are unique
        # within a record, which is checked below.
        item_id = row.id or slug(row.name)
        if not item_id:
            raise SpecError(f"row {row.number}: data item has neither an id nor a name")

        existing = {x["item_id"] for x in stack[-1][1]["fields"]}
        if item_id in existing:
            raise SpecError(
                f"row {row.number}: duplicate item key {item_id!r} in record "
                f"{stack[-1][1]['record_type']}"
            )

        stack[-1][1]["fields"].append(
            {
                "item_id": item_id,
                "name": row.name,
                "data_type": row.data_type,
                "presence": presence,
                "valid_set": row.valid_set or None,
                "comment": row.comment or None,
                "synthetic_id": not row.id,
            }
        )

    return flows


# --- emit -------------------------------------------------------------------

def _lit(value: str | None) -> str:
    return "None" if value is None else repr(value)


def _emit_field(field: dict, indent: str) -> list[str]:
    return [
        f"{indent}Field(",
        f"{indent}    item_id={field['item_id']!r},",
        f"{indent}    name={_lit(field['name'])},",
        f"{indent}    data_type=DataType.parse({field['data_type']!r}),",
        f"{indent}    presence={field['presence']!r},",
        f"{indent}    valid_set={_lit(field['valid_set'])},",
        f"{indent}    comment={_lit(field['comment'])},",
        f"{indent}    synthetic_id={field['synthetic_id']!r},",
        f"{indent}),",
    ]


def _emit_record(record: dict, indent: str) -> list[str]:
    lines = [
        f"{indent}Record(",
        f"{indent}    record_type={record['record_type']!r},",
        f"{indent}    name={_lit(record['name'])},",
        f"{indent}    cardinality=Cardinality.parse({record['cardinality']!r}),",
    ]

    if record["fields"]:
        lines.append(f"{indent}    fields=(")
        for field in record["fields"]:
            lines += _emit_field(field, indent + " " * 8)
        lines.append(f"{indent}    ),")

    if record["children"]:
        lines.append(f"{indent}    children=(")
        for child in record["children"]:
            lines += _emit_record(child, indent + " " * 8)
        lines.append(f"{indent}    ),")

    lines.append(f"{indent}),")
    return lines


def emit(flows: list[dict], source: str, tab: str) -> str:
    lines = [
        '"""IDD flow definitions. GENERATED FILE — DO NOT EDIT.',
        "",
        f"Source: {source}, tab {tab!r}",
        f"Generated: {dt.date.today().isoformat()} by tools/gen_spec.py",
        "",
        "Regenerate on a new IDD version and review the diff.",
        '"""',
        "",
        "from .model import Cardinality, DataType, Field, Flow, Record, Spec",
        "",
        "SPEC = Spec(",
        f"    source={source!r},",
        "    flows={",
    ]

    for flow in flows:
        file_type = f"{flow['file_id']}{flow['version']}"
        lines += [
            f"        {file_type!r}: Flow(",
            f"            file_id={flow['file_id']!r},",
            f"            version={flow['version']!r},",
            f"            name={_lit(flow['name'])},",
            f"            comment={_lit(flow['comment'])},",
            "            records=(",
        ]
        for record in flow["records"]:
            lines += _emit_record(record, " " * 16)
        lines += ["            ),", "        ),"]

    lines += ["    },", ")", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spreadsheet", type=Path)
    parser.add_argument("--tab", default="ECVAA")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    flows = parse(args.spreadsheet, args.tab)
    args.output.write_text(emit(flows, args.spreadsheet.name, args.tab))

    print(f"{len(flows)} flows -> {args.output}")


if __name__ == "__main__":
    main()