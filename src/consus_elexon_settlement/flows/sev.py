"""SVAA P0328: BM Unit Submitted Expected Volume Notification.

For MSID Pairs declared 'Submitted' rather than 'Baselined' (P0326), we tell
SVAA what we expect the BM Unit to do absent our action. SVAA adds this to the
baselined pairs' calculated values to form the Settlement Expected Volume,
which replaces the FPN for a Baselined BM Unit.

Structure (spec P0328001):

    BSD  1      SEV Settlement Date Range
      sev_effective_from_date   date            M
      sev_effective_to_date     date            O    null = Default SEV
      BSB  1-*  SEV BM Unit
        bm_unit_id              text(11)        M
        BSP  1-50 SEV BM Unit Period Data
          settlement_period_id  integer(2)      M
          submitted_expected_volume decimal(14,4) M

Two submissions share this flow, distinguished only by effective_to:

    Default    -- effective_to absent. Registered by 23:59 the day before it
                  takes effect and stands until replaced (BSCP602 2.13.1).
    Per-period -- effective_to set. Before Gate Closure (2.13.2).

If neither is in force before Gate Closure, SVAA sets Settlement Expected
Volume to null and the deviation is lost (2.13.7). Always keep a Default.

SIGN CONVENTION. submitted_expected_volume is CVA: positive is Export,
negative is Import. Site load is an import, so a site-load expected volume is
NEGATIVE. The conversion from our forecast happens here and nowhere else.

Item ids are synthetic: the SVAA tab of the IDD spreadsheet omits N-numbers
for every field, so gen_spec derives stable keys from the item names.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from ..idd.file import Node

FILE_TYPE = "P0328001"

BSD = "BSD"
BSB = "BSB"
BSP = "BSP"

EFFECTIVE_FROM = "sev_effective_from_date"
EFFECTIVE_TO = "sev_effective_to_date"
BMU_ID = "bm_unit_id"
SETTLEMENT_PERIOD = "settlement_period_id"
VOLUME = "submitted_expected_volume"

MAX_VOLUME = Decimal("9999999999.9999")   # decimal(14,4)


def import_to_cva(import_mwh: Decimal) -> Decimal:
    """Site import (positive) to CVA convention (negative).

    One function, one direction of confusion. Everything upstream of here
    thinks in site import; everything downstream is on the wire.
    """
    return -import_mwh


@dataclass(frozen=True)
class ExpectedPeriod:
    """One settlement period's expected volume, already in CVA convention."""

    settlement_period: int
    volume_mwh: Decimal

    def __post_init__(self) -> None:
        if not 1 <= self.settlement_period <= 50:
            raise ValueError(f"settlement period out of range: {self.settlement_period}")
        if abs(self.volume_mwh) > MAX_VOLUME:
            raise ValueError(f"volume exceeds decimal(14,4): {self.volume_mwh}")
        if -self.volume_mwh.as_tuple().exponent > 4:
            raise ValueError(f"volume has more than four decimal places: {self.volume_mwh}")


@dataclass(frozen=True)
class UnitVolumes:
    """Expected volumes for one BM Unit across the period range."""

    bmu_id: str
    periods: tuple[ExpectedPeriod, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.periods) <= 50:
            raise ValueError(f"BSP cardinality is 1-50, got {len(self.periods)}")
        ids = [p.settlement_period for p in self.periods]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate settlement period")
        if ids != sorted(ids):
            raise ValueError("settlement periods must be in ascending order")


@dataclass(frozen=True)
class Sev:
    """A Submitted Expected Volume notification."""

    effective_from: dt.date
    units: tuple[UnitVolumes, ...]
    effective_to: dt.date | None = None

    def __post_init__(self) -> None:
        if not self.units:
            raise ValueError("BSB cardinality is 1-*, so at least one BM Unit is required")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError(
                f"effective to {self.effective_to} precedes from {self.effective_from}"
            )
        ids = [u.bmu_id for u in self.units]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate BM Unit in a single notification")

    @property
    def is_default(self) -> bool:
        """A Default SEV: no end date, stands until replaced."""
        return self.effective_to is None


def to_nodes(sev: Sev) -> list[Node]:
    values: dict[str, object] = {EFFECTIVE_FROM: sev.effective_from}
    if sev.effective_to is not None:
        values[EFFECTIVE_TO] = sev.effective_to

    return [
        Node(
            record_type=BSD,
            values=values,
            children=[
                Node(
                    record_type=BSB,
                    values={BMU_ID: u.bmu_id},
                    children=[
                        Node(
                            record_type=BSP,
                            values={
                                SETTLEMENT_PERIOD: p.settlement_period,
                                VOLUME: p.volume_mwh,
                            },
                        )
                        for p in u.periods
                    ],
                )
                for u in sev.units
            ],
        )
    ]


def from_nodes(nodes: list[Node]) -> Sev:
    if len(nodes) != 1 or nodes[0].record_type != BSD:
        raise ValueError(
            f"expected a single {BSD} record, got {[n.record_type for n in nodes]}"
        )
    bsd = nodes[0]
    return Sev(
        effective_from=bsd.values[EFFECTIVE_FROM],      # type: ignore[arg-type]
        effective_to=bsd.values.get(EFFECTIVE_TO),      # type: ignore[arg-type]
        units=tuple(
            UnitVolumes(
                bmu_id=b.values[BMU_ID],                # type: ignore[arg-type]
                periods=tuple(
                    ExpectedPeriod(
                        settlement_period=p.values[SETTLEMENT_PERIOD],  # type: ignore[arg-type]
                        volume_mwh=p.values[VOLUME],                   # type: ignore[arg-type]
                    )
                    for p in b.of_type(BSP)
                ),
            )
            for b in bsd.of_type(BSB)
        ),
    )