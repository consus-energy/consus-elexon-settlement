"""SVAA P0282: MSID Pair Delivered Volume Notification, version 002.

Due at D+1 for every settlement period in which we traded (BSCP602 2.2A.1).

WHAT THIS REPORTS. Not the metered volume. SVAA already holds that, supplied
by the HHDAs under BSCP503, and uses this file to work out how much of it was
ours (BSCP602 Appendix 3.6). The Delivered Volume is capped by the metered
volume, so claiming more than the meter shows produces a P0285 exception
rather than settling. It is our deviation, computed from our own dispatch.

VERSION 002, not 001. The SVA Data Catalogue index gives 002 for the VTP
route, and BSCP602 footnote 14 explains why: P375 introduced new versions of
P0282, P0283, P0284 and P0285 carrying AMSID Pair Delivered Volumes.

The difference is not cosmetic. v001 nests MSI under MSC; v002 renames that
record to MSJ and adds an optional ASJ/ASP branch for asset metering:

    MSA  1     settlement_date
      MSB  1-*   gsp_group_id
        MSC  1-*   bm_unit_id
          MSJ  1-*   import_msid, export_msid?      <- MSI in v001
            MSP  0-50  settlement_period_id, delivered_volume
            ASJ  0-*   import_amsid, export_amsid?  <- AMVLP only
              ASP  1-50  settlement_period_id, delivered_volume

A file built with MSI records against the v002 spec is rejected outright, so
this is the kind of mistake that only surfaces on test day.

We hold no asset metering and are not an AMVLP, so ASJ is never emitted. Its
cardinality is 0-*, which makes omitting it valid. MSP relaxed from 1-50 to
0-50 in v002 because a pair might carry only AMSID data; we always send
periods, so PairVolumes keeps the stricter 1-50 rule.

The file nests five levels. Persistence flattens it -- one row per MSID pair --
because 'what did we submit for this pair' is the question that gets asked,
not 'what was in that file'. to_nodes regroups on the way out.

Item ids are synthetic: the SVAA tab omits N-numbers for every field.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal

from ..idd.file import Node

FILE_TYPE = "P0282002"

MSA = "MSA"
MSB = "MSB"
MSC = "MSC"
# Renamed from MSI in v001. Not a cosmetic change: the record type is matched
# by name, so an MSI record in a v002 file is unrecognised.
MSJ = "MSJ"
MSP = "MSP"

SETTLEMENT_DATE = "settlement_date"
GSP_GROUP_ID = "gsp_group_id"
BMU_ID = "bm_unit_id"
IMPORT_MSID = "import_msid"
EXPORT_MSID = "export_msid"
SETTLEMENT_PERIOD = "settlement_period_id"
VOLUME = "delivered_volume"

MAX_VOLUME = Decimal("9999999999.9999")   # decimal(14,4)


@dataclass(frozen=True)
class DeliveredPeriod:
    """One settlement period's delivered volume.

    CVA convention: positive is Export, negative is Import. A turn-down
    reduces import, which reads as a positive deviation.
    """

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

    export_msid absent means the pair has no export meter. BSCP602 1.1.1 is
    explicit that an MSID Pair must contain an Import Metering System but need
    not contain an Export one.
    """

    import_msid: int
    periods: tuple[DeliveredPeriod, ...]
    gsp_group_id: str
    bmu_id: str
    export_msid: int | None = None

    def __post_init__(self) -> None:
        # v002 relaxes MSP to 0-50 because a pair may carry only AMSID data.
        # We never send an AMSID branch, so a pair with no periods would be an
        # empty submission rather than a valid one.
        if not 1 <= len(self.periods) <= 50:
            raise ValueError(f"expected 1 to 50 periods, got {len(self.periods)}")
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

    Grouping preserves first-seen order at each level rather than sorting. The
    IDD requires records in spec order, which constrains record *types*, not
    the order of repeats; keeping input order makes a built file comparable to
    its source without a canonical sort nobody agreed on.
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
        record_type=MSJ,
        values=values,
        # No ASJ branch: we hold no asset metering and are not an AMVLP.
        # ASJ is 0-*, so omitting it is valid.
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
    """Nested tree back to flat pairs, carrying group and unit down.

    ASJ records are ignored rather than rejected. We never emit them, but a
    file read back from the archive after a version change should not fail on
    a branch we simply do not use.
    """
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
            for msj in msc.of_type(MSJ):
                pairs.append(
                    PairVolumes(
                        gsp_group_id=gsp_group_id,                # type: ignore[arg-type]
                        bmu_id=bmu_id,                            # type: ignore[arg-type]
                        import_msid=msj.values[IMPORT_MSID],      # type: ignore[arg-type]
                        export_msid=msj.values.get(EXPORT_MSID),  # type: ignore[arg-type]
                        periods=tuple(
                            DeliveredPeriod(
                                settlement_period=p.values[SETTLEMENT_PERIOD],  # type: ignore[arg-type]
                                volume_mwh=p.values[VOLUME],                   # type: ignore[arg-type]
                            )
                            for p in msj.of_type(MSP)
                        ),
                    )
                )

    return Delivered(
        settlement_date=msa.values[SETTLEMENT_DATE],  # type: ignore[arg-type]
        pairs=tuple(pairs),
    )