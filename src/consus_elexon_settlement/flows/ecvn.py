"""ECVAA-I004: Energy Contract Volume Notification (E0041).

Tells ECVAA the volume contracted between two Energy Accounts. This is how a
trade made on the exchange becomes a settled position: without it, delivered
volume is cashed out at the imbalance price in full.

Structure (spec E0041001):

    EDN  1     ECVNs
      N0080    ECVNAA Id                text(10)   M
      N0297    ECVNAA Key               text(10)   M   <- credential
      N0310    ECVN ECVNAA Id           text(10)   M
      N0077    ECVN Reference Code      text(10)   M
      N0081    Effective From Date      date       M
      N0083    Effective To Date        date       O
      OTD  0-1 Omitted Data No Change             <- disabled, never emitted
      CD9  0-* Energy Contract Volumes
        N0201  Settlement Period        integer(2) M
        N0085  energy contract volume   decimal(10,3) M, signed, MWh

Three things about this flow that are easy to get wrong:

1.  EDN has cardinality 1, so one notification per file. Several notifications
    means several files, each consuming a sequence number.

2.  An ECVN is a profile spanning Effective From to Effective To, not a single
    day's trade. The CD9 periods repeat across every day in that range. A
    one-day notification sets both dates the same.

3.  The ECVNA Id is NOT a field here. It is the From Participant Id in the AAA
    header (IDD ECVAA-I004). N0080 is the *authorisation* id, which is a
    different thing: it identifies the standing agreement between the two
    trading parties, established manually under BSCP71.

Sent under our ECVNA identity ('EN' + ECVNA Id), not the VTP one. E0511 uses
'VT' + Party Id. Two identities, two sequence counters.

The key (N0297) is a credential. It is deliberately not part of the domain
object -- it is supplied at build time from Secret Manager, so an Ecvn can be
persisted, logged or replayed without carrying it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from ..idd.file import Node

FILE_TYPE = "E0041001"

EDN = "EDN"
OTD = "OTD"
CD9 = "CD9"

ECVNAA_ID = "N0080"
ECVNAA_KEY = "N0297"
ECVN_ECVNAA_ID = "N0310"
REFERENCE_CODE = "N0077"
EFFECTIVE_FROM = "N0081"
EFFECTIVE_TO = "N0083"
SETTLEMENT_PERIOD = "N0201"
VOLUME_MWH = "N0085"

# decimal(10,3): ten digits total, three after the point.
MAX_VOLUME = Decimal("9999999.999")


@dataclass(frozen=True)
class ContractVolume:
    """One settlement period's contracted volume.

    Signed, MWh, from party 1 to party 2 as named in the authorisation. A
    negative value is a flow in the opposite direction, not an error.
    """

    settlement_period: int
    volume_mwh: Decimal

    def __post_init__(self) -> None:
        if not 1 <= self.settlement_period <= 50:
            raise ValueError(f"settlement period out of range: {self.settlement_period}")
        if abs(self.volume_mwh) > MAX_VOLUME:
            raise ValueError(f"volume exceeds decimal(10,3): {self.volume_mwh}")
        if -self.volume_mwh.as_tuple().exponent > 3:
            raise ValueError(f"volume has more than three decimal places: {self.volume_mwh}")


@dataclass(frozen=True)
class Ecvn:
    """One energy contract volume notification.

    `volumes` empty is a valid file: CD9 has cardinality 0-*. Confirm with
    Elexon whether that is how a notification is withdrawn, since N0085 is
    mandatory within CD9 and so a null volume cannot be expressed as an empty
    field. Recorded as an open question rather than assumed.
    """

    ecvnaa_id: str
    ecvn_ecvnaa_id: str
    reference_code: str
    effective_from: dt.date
    volumes: tuple[ContractVolume, ...]
    effective_to: dt.date | None = None

    def __post_init__(self) -> None:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError(
                f"effective to {self.effective_to} precedes from {self.effective_from}"
            )
        periods = [v.settlement_period for v in self.volumes]
        if len(set(periods)) != len(periods):
            raise ValueError("duplicate settlement period in a single notification")
        if periods != sorted(periods):
            # IDD 2.2.4: records appear in spec order. Sorting here would hide
            # a caller bug that will matter when the file is reconciled.
            raise ValueError("settlement periods must be in ascending order")

    @property
    def is_withdrawal(self) -> bool:
        return not self.volumes


def to_nodes(ecvn: Ecvn, ecvnaa_key: str) -> list[Node]:
    """Domain object to the Node tree file.build expects.

    `ecvnaa_key` is read from Secret Manager at call time. It is not held on
    the Ecvn so that the notification can be stored and replayed without the
    credential travelling with it.

    OTD is never emitted: the spec marks it disabled (added for P98), and its
    cardinality is 0-1, so omitting it is valid.
    """
    values: dict[str, object] = {
        ECVNAA_ID: ecvn.ecvnaa_id,
        ECVNAA_KEY: ecvnaa_key,
        ECVN_ECVNAA_ID: ecvn.ecvn_ecvnaa_id,
        REFERENCE_CODE: ecvn.reference_code,
        EFFECTIVE_FROM: ecvn.effective_from,
    }
    if ecvn.effective_to is not None:
        values[EFFECTIVE_TO] = ecvn.effective_to

    return [
        Node(
            record_type=EDN,
            values=values,
            children=[
                Node(
                    record_type=CD9,
                    values={
                        SETTLEMENT_PERIOD: v.settlement_period,
                        VOLUME_MWH: v.volume_mwh,
                    },
                )
                for v in ecvn.volumes
            ],
        )
    ]


def from_nodes(nodes: list[Node]) -> tuple[Ecvn, str]:
    """Node tree back to a domain object, plus the key that was on the wire.

    Returned separately for the same reason it is passed separately: the
    caller decides whether to keep it. Round-trip tests compare the Ecvn and
    discard the key.
    """
    if len(nodes) != 1 or nodes[0].record_type != EDN:
        raise ValueError(
            f"expected a single {EDN} record, got {[n.record_type for n in nodes]}"
        )

    edn = nodes[0]
    ecvn = Ecvn(
        ecvnaa_id=edn.values[ECVNAA_ID],                    # type: ignore[arg-type]
        ecvn_ecvnaa_id=edn.values[ECVN_ECVNAA_ID],          # type: ignore[arg-type]
        reference_code=edn.values[REFERENCE_CODE],          # type: ignore[arg-type]
        effective_from=edn.values[EFFECTIVE_FROM],          # type: ignore[arg-type]
        effective_to=edn.values.get(EFFECTIVE_TO),          # type: ignore[arg-type]
        volumes=tuple(
            ContractVolume(
                settlement_period=n.values[SETTLEMENT_PERIOD],  # type: ignore[arg-type]
                volume_mwh=n.values[VOLUME_MWH],               # type: ignore[arg-type]
            )
            for n in edn.of_type(CD9)
        ),
    )
    return ecvn, edn.values[ECVNAA_KEY]                     # type: ignore[return-value]