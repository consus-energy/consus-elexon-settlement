"""SVAA P0282: MSID Pair Delivered Volume Notification.

Due at D+1 for every settlement period in which we traded (BSCP602 2.2A.1).
Where a pair is Submitted rather than Baselined, SVAA does not calculate the
delivered volume for us, so we determine and submit it.

Unlike the SEV, this comes from metered data rather than from our forecast.
Different source, different timescale, arriving from the site metering chain
a day after the event.

Structure (spec P0282001), five levels:

    MSA  1      Settlement Date
      settlement_date         date          M
      MSB  1-*  GSP Group
        gsp_group_id          text(2)       M
        MSC  1-*  Secondary BM Unit
          bm_unit_id          text(11)      M
          MSI  1-*  MSID Details
            import_msid       integer(13)   M
            export_msid       integer(13)   O    absent = no export meter
            MSP  1-50  Secondary BM Unit Period Data
              settlement_period_id  integer(2)  M
              <volume>              decimal(14,4) M

The nesting is faithful to the file. Persistence flattens it -- one row per
MSID pair -- because 'what did we submit for this pair' is the question that
gets asked, not 'what was in that file'. build() regroups on the way out.

Item ids are synthetic: the SVAA tab omits N-numbers for every field.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal

from ..idd.file import Node

FILE_TYPE = "P0282001"

MSA = "MSA"
MSB = "MSB"
MSC = "MSC"
MSI = "MSI"
MSP = "MSP"

SETTLEMENT_DATE = "settlement_date"
GSP_GROUP_ID = "gsp_group_id"
BMU_ID = "bm_unit_id"
IMPORT_MSID = "import_msid"
EXPORT_MSID = "export_msid"
SETTLEMENT_PERIOD = "settlement_period_id"

# TODO confirm against spec_svaa: the MSP volume field name was truncated when
# the spec was read. Expected 'delivered_volume', decimal(14,4), by symmetry
# with P0328. Check before first build:
#   python3 -c "s=open('src/consus_elexon_settlement/idd/spec_svaa.py').read();
#               i=s.find(\"record_type='MSP'\"); print(s[i:i+1600])"
VOLUME = "delivered_volume"

MAX_VOLUME = Decimal("9999999999.9999")   # decimal(14,4)


@dataclass(frozen=True)
class DeliveredPeriod:
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
class PairVolumes:
    """Delivered volumes for one MSID Pair.

    export_msid absent means the pair has no export meter, which is the normal
    case for a behind-the-meter battery that does not export.
    """

    import_msid: int
    periods: tuple[DeliveredPeriod, ...]
    gsp_group_id: str
    bmu_id: str
    export_msid: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.periods) <= 50:
            raise ValueError(f"MSP cardinality is 1-50, got {len(self.periods)}")
        ids = [p.settlement_period for p in self.periods]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate settlement period")
        if ids != sorted(ids):
            raise ValueError("settlement periods must be in ascending order")
        if len(str(self.import_msid)) > 13:
            raise ValueError(f"import MSID exceeds 13 digits: {self.import_msid}")
        if self.export_msid is not None and len(str(self.export_msid)) > 13:
            raise ValueError(f"export MSID exceeds 13 digits: {self.export_msid}")


@dataclass(frozen=True)
class Delivered:
    """A delivered volume notification for one settlement date.

    Pairs are supplied flat, as they are stored. to_nodes groups them into the
    GSP group and BM Unit levels the file requires.
    """

    settlement_date: dt.date
    pairs: tuple[PairVolumes, ...]

    def __post_init__(self) -> None:
        if not self.pairs:
            raise ValueError("MSB cardinality is 1-*, so at least one pair is required")
        keys = [(p.gsp_group_id, p.bmu_id, p.import_msid) for p in self.pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate MSID pair within a BM Unit")


def to_nodes(delivered: Delivered) -> list[Node]:
    """Flat pairs to the nested Node tree the file requires.

    Grouping preserves first-seen order at each level rather than sorting.
    The IDD requires records in spec order, which constrains record *types*,
    not the order of repeats; keeping input order makes a built file
    comparable to its source without a canonical sort nobody agreed on.
    """
    groups: OrderedDict[str, OrderedDict[str, list[PairVolumes]]] = OrderedDict()
    for pair in delivered.pairs:
        groups.setdefault(pair.gsp_group_id, OrderedDict()) \
              .setdefault(pair.bmu_id, []).append(pair)

    return [
        Node(
            record_type=MSA,
            values={SETTLEMENT_DATE: delivered.settlement_date},
            children=[
                Node(
                    record_type=MSB,
                    values={GSP_GROUP_ID: gsp_group_id},
                    children=[
                        Node(
                            record_type=MSC,
                            values={BMU_ID: bmu_id},
                            children=[_pair_node(p) for p in pairs],
                        )
                        for bmu_id, pairs in units.items()
                    ],
                )
                for gsp_group_id, units in groups.items()
            ],
        )
    ]


def _pair_node(pair: PairVolumes) -> Node:
    values: dict[str, object] = {IMPORT_MSID: pair.import_msid}
    if pair.export_msid is not None:
        values[EXPORT_MSID] = pair.export_msid
    return Node(
        record_type=MSI,
        values=values,
        children=[
            Node(
                record_type=MSP,
                values={
                    SETTLEMENT_PERIOD: p.settlement_period,
                    VOLUME: p.volume_mwh,
                },
            )
            for p in pair.periods
        ],
    )


def from_nodes(nodes: list[Node]) -> Delivered:
    """Nested tree back to flat pairs, carrying the group and unit down."""
    if len(nodes) != 1 or nodes[0].record_type != MSA:
        raise ValueError(
            f"expected a single {MSA} record, got {[n.record_type for n in nodes]}"
        )
    msa = nodes[0]

    pairs: list[PairVolumes] = []
    for msb in msa.of_type(MSB):
        gsp_group_id = msb.values[GSP_GROUP_ID]
        for msc in msb.of_type(MSC):
            bmu_id = msc.values[BMU_ID]
            for msi in msc.of_type(MSI):
                pairs.append(
                    PairVolumes(
                        gsp_group_id=gsp_group_id,          # type: ignore[arg-type]
                        bmu_id=bmu_id,                      # type: ignore[arg-type]
                        import_msid=msi.values[IMPORT_MSID],  # type: ignore[arg-type]
                        export_msid=msi.values.get(EXPORT_MSID),  # type: ignore[arg-type]
                        periods=tuple(
                            DeliveredPeriod(
                                settlement_period=p.values[SETTLEMENT_PERIOD],  # type: ignore[arg-type]
                                volume_mwh=p.values[VOLUME],                   # type: ignore[arg-type]
                            )
                            for p in msi.of_type(MSP)
                        ),
                    )
                )

    return Delivered(
        settlement_date=msa.values[SETTLEMENT_DATE],  # type: ignore[arg-type]
        pairs=tuple(pairs),
    )