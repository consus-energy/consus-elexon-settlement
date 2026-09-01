"""Inbound ECVAA feedback: rejection, acceptance, WMAN exception.

These are the three flows that change what we believe about a submission. The
reports (E0131, E0141, E0221) are informational and reconciled separately.

The distinction that matters throughout: receipt acknowledgement is not
acceptance. A file can be receipt-acked and then wholly rejected here, hours
later. Nothing is settled until an acceptance arrives.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from ..idd.file import Node

# --- E0091 ECVN Feedback (rejection) ---------------------------------------

REJECTION_FILE_TYPE = "E0091001"
EDX = "EDX"
CD2 = "CD2"

# --- E0281 ECVN Acceptance Feedback ----------------------------------------

ACCEPTANCE_FILE_TYPE = "E0281001"
EDA = "EDA"
CD9 = "CD9"

# --- E0521 WMAN Exception Report -------------------------------------------

WMAN_EXCEPTION_FILE_TYPE = "E0521001"
WMR = "WMR"
WMJ = "WMJ"

# Item ids, shared where the flows agree.
ECVNA_ID = "N0078"
ECVNAA_ID = "N0080"
ECVN_ECVNAA_ID = "N0310"
REFERENCE_CODE = "N0077"
EFFECTIVE_FROM = "N0081"
EFFECTIVE_TO = "N0083"
REASON = "N0187"
SETTLEMENT_PERIOD = "N0201"
VOLUME = "N0085"
FIRST_EFFECTIVE_PERIOD = "N0370"
OUR_FILENAME = "N0301"
OUR_SEQUENCE = "N0198"
TRANSACTION_ID = "N0369"
SETTLEMENT_DATE = "N0200"
BMU_ID = "N0034"


@dataclass(frozen=True)
class RejectedPeriod:
    """One period within a rejected notification.

    N0085 is optional here, unlike in the acceptance, so a period can be
    named without a volume.
    """

    settlement_period: int
    volume_mwh: Decimal | None = None


@dataclass(frozen=True)
class EcvnRejection:
    """E0091. The notification failed business validation and is not in
    effect. The reason is 80 characters of free text."""

    ecvna_id: str
    ecvnaa_id: str
    ecvn_ecvnaa_id: str
    reference_code: str
    effective_from: dt.date
    reason: str
    periods: tuple[RejectedPeriod, ...] = ()
    effective_to: dt.date | None = None


@dataclass(frozen=True)
class AcceptedPeriod:
    settlement_period: int
    volume_mwh: Decimal


@dataclass(frozen=True)
class EcvnAcceptance:
    """E0281. The notification passed validation and is in effect.

    Carries our own filename and sequence number back to us, which is how a
    submission is correlated without guessing from the reference code. The
    transaction id is ECVAA's handle for it and belongs in any query we raise.

    first_effective_period matters: an ECVN submitted mid-day takes effect
    from a period, not from the start of the day.
    """

    ecvna_id: str
    ecvnaa_id: str
    ecvn_ecvnaa_id: str
    reference_code: str
    effective_from: dt.date
    first_effective_period: int
    our_filename: str
    our_sequence_number: int
    transaction_id: int
    periods: tuple[AcceptedPeriod, ...] = ()
    effective_to: dt.date | None = None


@dataclass(frozen=True)
class RejectedUnit:
    bmu_id: str
    reason: str


@dataclass(frozen=True)
class WmanException:
    """E0521. Rejection of a wholesale market activity notification.

    Rejection happens at two levels: the whole period, or named BM Units
    within it. A file with a period-level reason and no WMJ records is a
    complete rejection; WMJ records without a period reason reject only those
    units. Treating either as total would be wrong in one direction or the
    other, so both are kept.
    """

    settlement_date: dt.date
    settlement_period: int
    reason: str | None = None
    units: tuple[RejectedUnit, ...] = ()

    @property
    def whole_period_rejected(self) -> bool:
        return self.reason is not None and not self.units


def parse_rejection(body: list[Node]) -> EcvnRejection:
    edx = _single(body, EDX)
    return EcvnRejection(
        ecvna_id=edx.values[ECVNA_ID],                    # type: ignore[arg-type]
        ecvnaa_id=edx.values[ECVNAA_ID],                  # type: ignore[arg-type]
        ecvn_ecvnaa_id=edx.values[ECVN_ECVNAA_ID],        # type: ignore[arg-type]
        reference_code=edx.values[REFERENCE_CODE],        # type: ignore[arg-type]
        effective_from=edx.values[EFFECTIVE_FROM],        # type: ignore[arg-type]
        effective_to=edx.values.get(EFFECTIVE_TO),        # type: ignore[arg-type]
        reason=edx.values[REASON],                        # type: ignore[arg-type]
        periods=tuple(
            RejectedPeriod(
                settlement_period=n.values[SETTLEMENT_PERIOD],  # type: ignore[arg-type]
                volume_mwh=n.values.get(VOLUME),               # type: ignore[arg-type]
            )
            for n in edx.of_type(CD2)
        ),
    )


def parse_acceptance(body: list[Node]) -> EcvnAcceptance:
    eda = _single(body, EDA)
    return EcvnAcceptance(
        ecvna_id=eda.values[ECVNA_ID],                          # type: ignore[arg-type]
        ecvnaa_id=eda.values[ECVNAA_ID],                        # type: ignore[arg-type]
        ecvn_ecvnaa_id=eda.values[ECVN_ECVNAA_ID],              # type: ignore[arg-type]
        reference_code=eda.values[REFERENCE_CODE],              # type: ignore[arg-type]
        effective_from=eda.values[EFFECTIVE_FROM],              # type: ignore[arg-type]
        effective_to=eda.values.get(EFFECTIVE_TO),              # type: ignore[arg-type]
        first_effective_period=eda.values[FIRST_EFFECTIVE_PERIOD],  # type: ignore[arg-type]
        our_filename=eda.values[OUR_FILENAME],                  # type: ignore[arg-type]
        our_sequence_number=eda.values[OUR_SEQUENCE],           # type: ignore[arg-type]
        transaction_id=eda.values[TRANSACTION_ID],              # type: ignore[arg-type]
        periods=tuple(
            AcceptedPeriod(
                settlement_period=n.values[SETTLEMENT_PERIOD],  # type: ignore[arg-type]
                volume_mwh=n.values[VOLUME],                    # type: ignore[arg-type]
            )
            for n in eda.of_type(CD9)
        ),
    )


def parse_wman_exception(body: list[Node]) -> WmanException:
    wmr = _single(body, WMR)
    return WmanException(
        settlement_date=wmr.values[SETTLEMENT_DATE],      # type: ignore[arg-type]
        settlement_period=wmr.values[SETTLEMENT_PERIOD],  # type: ignore[arg-type]
        reason=wmr.values.get(REASON),                    # type: ignore[arg-type]
        units=tuple(
            RejectedUnit(
                bmu_id=n.values[BMU_ID],                  # type: ignore[arg-type]
                reason=n.values[REASON],                  # type: ignore[arg-type]
            )
            for n in wmr.of_type(WMJ)
        ),
    )


def _single(body: list[Node], record_type: str) -> Node:
    if len(body) != 1 or body[0].record_type != record_type:
        raise ValueError(
            f"expected a single {record_type} record, got {[n.record_type for n in body]}"
        )
    return body[0]



# --- E0071 ECVNAA Feedback (confirmation) ----------------------------------
#
# Confirms a standing authorisation has been processed, and carries the
# ECVNAA Key we must quote on every subsequent ECVN. Establishing the
# authorisation is a manual process under BSCP71; this is the only automated
# point at which the key reaches us.
#
# Three versions exist (E0071001/002/003) differing in optional records.
# The EAD record carrying the key is present in all three, so the parser
# reads that record rather than assuming a version.

ECVNAA_FEEDBACK_FILE_TYPES = ("E0071001", "E0071002", "E0071003")
EAD = "EAD"
ECVNAA_KEY_FIELD = "N0297"
EFFECTIVE_FROM_FIELD = "N0081"


@dataclass(frozen=True)
class EcvnaaConfirmation:
    """An authorisation has been processed.

    The key is optional in the flow: a confirmation of an amendment or
    termination may carry none. A confirmation without a key is not an error,
    but it is also not something to store as if it were one.
    """

    ecvnaa_id: str
    ecvnaa_key: str | None = None
    effective_from: dt.date | None = None

    @property
    def carries_key(self) -> bool:
        return self.ecvnaa_key is not None


def parse_ecvnaa_confirmation(body: list[Node]) -> EcvnaaConfirmation:
    """Read the EAD record, wherever it sits in the file.

    The surrounding records differ by version and carry counterparty details
    we already hold. Searching for EAD rather than indexing by position means
    the parser works across all three versions.
    """
    ead = next((n for n in body if n.record_type == EAD), None)
    if ead is None:
        raise ValueError(f"no {EAD} record; got {[n.record_type for n in body]}")

    return EcvnaaConfirmation(
        ecvnaa_id=ead.values[ECVNAA_ID],                      # type: ignore[arg-type]
        ecvnaa_key=ead.values.get(ECVNAA_KEY_FIELD),          # type: ignore[arg-type]
        effective_from=ead.values.get(EFFECTIVE_FROM_FIELD),  # type: ignore[arg-type]
    )