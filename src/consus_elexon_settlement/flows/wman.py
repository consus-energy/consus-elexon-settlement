"""ECVAA-I051: Wholesale Market Activity Notification (E0511).

Tells ECVAA which of our Trading Secondary BM Units are wholesale-active in a
given settlement period. Must arrive before Gate Closure for that period
(BSCP602 2.18.1). ECVAA collates the day's notifications and passes them to
SVAA in the Daily WMAN Report, which is what drives baselining.

Structure (spec E0511001):

    SDP  1     Settlement Date / Period
      N0200    Settlement Date            date
      N0201    Settlement Period          integer(2)
    WMA  1-*   Wholesale Market Active BM Units
      N0034    BM Unit Id                 text(11)
      N0652    Active indicator           boolean

WMA nests under SDP. One file covers one settlement period; several periods
means several files, each with its own sequence number.

Sent under our VTP identity ('VT' + Party Id), not the ECVNA one. E0041 uses
'EN' + ECVNA Id. Two identities, two sequence counters.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..idd.file import Node

FILE_TYPE = "E0511001"

SDP = "SDP"
WMA = "WMA"

SETTLEMENT_DATE = "N0200"
SETTLEMENT_PERIOD = "N0201"
BMU_ID = "N0034"
ACTIVE = "N0652"


@dataclass(frozen=True)
class ActiveUnit:
    """One BM Unit's activity state for the period."""

    bmu_id: str
    active: bool = True


@dataclass(frozen=True)
class Wman:
    """A wholesale market activity notification for one settlement period."""

    settlement_date: dt.date
    settlement_period: int
    units: tuple[ActiveUnit, ...]

    def __post_init__(self) -> None:
        # IDD 2.2.9: 46, 48 or 50 periods depending on clock change. Range is
        # checked here; whether period 49 exists on this particular date is a
        # calendar question the caller owns.
        if not 1 <= self.settlement_period <= 50:
            raise ValueError(f"settlement period out of range: {self.settlement_period}")
        if not self.units:
            raise ValueError("WMA has cardinality 1-*, so at least one BM Unit is required")
        seen = {u.bmu_id for u in self.units}
        if len(seen) != len(self.units):
            raise ValueError("duplicate BM Unit in a single notification")


def to_nodes(wman: Wman) -> list[Node]:
    """Domain object to the Node tree file.build expects."""
    return [
        Node(
            record_type=SDP,
            values={
                SETTLEMENT_DATE: wman.settlement_date,
                SETTLEMENT_PERIOD: wman.settlement_period,
            },
            children=[
                Node(record_type=WMA, values={BMU_ID: u.bmu_id, ACTIVE: u.active})
                for u in wman.units
            ],
        )
    ]


def from_nodes(nodes: list[Node]) -> Wman:
    """Node tree back to a domain object. Used in round-trip tests, and when
    reading a file back out of the archive during reconciliation."""
    if len(nodes) != 1 or nodes[0].record_type != SDP:
        raise ValueError(f"expected a single {SDP} record, got {[n.record_type for n in nodes]}")

    sdp = nodes[0]
    return Wman(
        settlement_date=sdp.values[SETTLEMENT_DATE],       # type: ignore[arg-type]
        settlement_period=sdp.values[SETTLEMENT_PERIOD],   # type: ignore[arg-type]
        units=tuple(
            ActiveUnit(bmu_id=n.values[BMU_ID], active=n.values[ACTIVE])  # type: ignore[arg-type]
            for n in sdp.of_type(WMA)
        ),
    )