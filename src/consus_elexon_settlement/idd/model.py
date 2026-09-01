"""Structural model for IDD Part 1 flow definitions.

These types describe the *shape* of a flow: which record types appear, in what
order, how they nest, how many times they may repeat, and what each field's
data type is. They carry no encoding logic — that lives in fields.py — and no
I/O.

Populated by the generated spec.py. Do not hand-edit spec.py; regenerate it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Literal

Presence = Literal["M", "O", "N"]  # mandatory, optional, unused


@dataclass(frozen=True)
class DataType:
    """A parsed IDD data type, e.g. text(10), decimal(10,3), date."""

    kind: str
    length: int | None = None
    decimals: int | None = None

    _PATTERN = re.compile(r"^(?P<kind>[a-z0-9]+)(?:\((?P<n>\d+)(?:,(?P<d>\d+))?\))?$")

    # The spreadsheet is not perfectly consistent across tabs. The SAA tab
    # writes some string fields with the Oracle type name rather than the IDD
    # one; they are variable-length strings, i.e. text. Aliasing here keeps
    # the deviation in one visible place rather than spread through the
    # generated specs.
    _ALIASES = {"varchar2": "text"}

    @classmethod
    def parse(cls, raw: str) -> "DataType":
        # Some cells carry a space before the bracket, e.g. 'decimal (14,4)'.
        normalised = "".join(raw.split()).lower()
        match = cls._PATTERN.match(normalised)
        if not match:
            raise ValueError(f"unrecognised IDD data type: {raw!r}")
        n = match.group("n")
        d = match.group("d")
        return cls(
            kind=cls._ALIASES.get(match.group("kind"), match.group("kind")),
            length=int(n) if n is not None else None,
            decimals=int(d) if d is not None else None,
        )

    def __str__(self) -> str:
        if self.decimals is not None:
            return f"{self.kind}({self.length},{self.decimals})"
        if self.length is not None:
            return f"{self.kind}({self.length})"
        return self.kind


@dataclass(frozen=True)
class Field:
    """A data item within a record type."""

    item_id: str          # N0080
    name: str             # ECVNAA Id
    data_type: DataType
    presence: Presence
    valid_set: str | None = None
    comment: str | None = None
    # True when the spreadsheet gave no N-number and the key was derived from
    # the item name. The SVAA tab omits them entirely.
    synthetic_id: bool = False

    @property
    def required(self) -> bool:
        return self.presence == "M"


@dataclass(frozen=True)
class Cardinality:
    """How many times a record type may appear at its level."""

    minimum: int
    maximum: int | None          # None means unbounded
    clock_change: bool = False   # the 46-50 special case: 46, 48 or 50 only

    @classmethod
    def parse(cls, raw: str) -> "Cardinality":
        text = raw.strip()
        if text == "46-50":
            # IDD 2.2.4: means 46, 48 or 50 — but not 47 or 49.
            return cls(minimum=46, maximum=50, clock_change=True)
        if "-" in text:
            low, high = text.split("-", 1)
            return cls(minimum=int(low), maximum=None if high == "*" else int(high))
        return cls(minimum=int(text), maximum=int(text))

    @property
    def repeating(self) -> bool:
        return self.maximum is None or self.maximum > 1

    @property
    def optional(self) -> bool:
        return self.minimum == 0

    def permits(self, count: int) -> bool:
        if count < self.minimum:
            return False
        if self.maximum is not None and count > self.maximum:
            return False
        if self.clock_change and count not in (46, 48, 50):
            return False
        return True


@dataclass(frozen=True)
class Record:
    """A record type: its fields, then any nested record types."""

    record_type: str            # EDN
    name: str                   # ECVNs
    cardinality: Cardinality
    fields: tuple[Field, ...] = ()
    children: tuple["Record", ...] = ()


@dataclass(frozen=True)
class Flow:
    """One physical file type, e.g. E0041001."""

    file_id: str                # E0041
    version: str                # 001
    name: str                   # ECVAA-I004: ECVNs
    records: tuple[Record, ...] = ()
    comment: str | None = None

    @property
    def file_type(self) -> str:
        """The 8-character File Type field of the AAA header."""
        return f"{self.file_id}{self.version}"


@dataclass(frozen=True)
class Spec:
    """All flows for one central system, keyed on file type."""

    source: str                          # spreadsheet filename and version
    flows: dict[str, Flow] = dc_field(default_factory=dict)

    def __getitem__(self, file_type: str) -> Flow:
        return self.flows[file_type]