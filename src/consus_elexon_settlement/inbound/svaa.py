"""Inbound SVAA feedback on Submitted Expected Volumes and Delivered Volumes.

Five flows that change what we believe about a submission:

    P0329  SEV rejection          -- the expected volume was not accepted
    P0330  SEV acceptance         -- it was
    P0331  SEV warning            -- accepted, but a period is zero
    P0283  Delivered rejection    -- the delivered volume failed validation
    P0284  Delivered confirmation -- it passed

One asymmetry shapes this module. P0330 acceptance carries only the BM Unit
id -- no filename, no sequence number, no effective dates. So a SEV acceptance
can only be correlated to the most recent outstanding submission for that
unit. ECVAA's E0281 hands our own filename back; SVAA does not. Where more
than one submission for a unit is outstanding, correlation is ambiguous and
the handler raises rather than guessing.

P0329 rejection is the opposite: every field is optional except the reason.
A rejection may therefore identify the unit, the effective dates and the
period, or almost nothing. The parser keeps whatever arrived rather than
requiring a shape the flow does not guarantee.

Versions: P0283 and P0285 have a second version adding AMSID fields for asset
metering. We are VTP-only and do not hold AMVLP, so version 001 is expected.
Confirm which version SVAA sends before go-live -- registering a handler
against the wrong version means the file is received, acknowledged, and never
acted on.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from ..idd.file import Node

# --- file types -------------------------------------------------------------

SEV_REJECTION_FILE_TYPE = "P0329001"
SEV_ACCEPTANCE_FILE_TYPE = "P0330001"
SEV_WARNING_FILE_TYPE = "P0331001"
DELIVERED_REJECTION_FILE_TYPE = "P0283001"
DELIVERED_CONFIRMATION_FILE_TYPE = "P0284001"

BSR = "BSR"
BSA = "BSA"
BSX = "BSX"
MSR = "MSR"
MSF = "MSF"

# --- item ids (synthetic: the SVAA tab omits N-numbers) ---------------------

BMU_ID = "bm_unit_id"
SETTLEMENT_DATE = "settlement_date"
SETTLEMENT_PERIOD = "settlement_period_id"
GSP_GROUP_ID = "gsp_group_id"
IMPORT_MSID = "import_msid"
EXPORT_MSID = "export_msid"
SEV_FROM = "sev_effective_from_date"
SEV_TO = "sev_effective_to_date"
SEV_VOLUME = "submitted_expected_volume"
SEV_REASON = "submitted_expected_volume_rejection_reason"
DELIVERED_VOLUME = "delivered_volume"
DELIVERED_REASON = "delivered_volume_rejection_reason"


@dataclass(frozen=True)
class SevRejection:
    """One rejected expected volume.

    Only the reason is mandatory. Everything else is optional in the flow, so
    a rejection may name the unit and period or almost nothing. Fields are
    kept as they arrived: inventing a default would make a partial rejection
    look like a complete one.
    """

    reason: str
    bmu_id: str | None = None
    effective_from: dt.date | None = None
    effective_to: dt.date | None = None
    settlement_period: int | None = None
    volume_mwh: Decimal | None = None


@dataclass(frozen=True)
class SevAcceptance:
    """An accepted expected volume, identified only by BM Unit.

    That is the whole flow: BSA carries bm_unit_id and nothing else. There is
    no way to tell from the file which submission was accepted, so
    correlation depends on there being exactly one outstanding.
    """

    bmu_id: str


@dataclass(frozen=True)
class SevWarning:
    """A period has indicator 'S' but an expected volume of zero.

    Not a rejection: the submission stands. But a zero expected volume for a
    Submitted pair usually means a forecast produced nothing rather than
    genuinely expecting nothing, so it is worth investigating before the
    deviation is measured against it.
    """

    bmu_id: str
    settlement_date: dt.date


@dataclass(frozen=True)
class DeliveredRejection:
    """One rejected delivered volume. As with P0329, only the reason is
    mandatory."""

    reason: str
    settlement_date: dt.date | None = None
    gsp_group_id: str | None = None
    bmu_id: str | None = None
    import_msid: int | None = None
    export_msid: int | None = None
    settlement_period: int | None = None
    volume_mwh: Decimal | None = None


@dataclass(frozen=True)
class DeliveredConfirmation:
    """Confirmation that a delivered volume passed validation.

    MSF has cardinality 1-1, so one confirmation per file, and only the
    settlement date is mandatory.
    """

    settlement_date: dt.date
    gsp_group_id: str | None = None
    bmu_id: str | None = None
    import_msid: int | None = None
    export_msid: int | None = None
    settlement_period: int | None = None
    volume_mwh: Decimal | None = None


def parse_sev_rejections(body: list[Node]) -> list[SevRejection]:
    """BSR repeats, so a file may carry many rejections."""
    return [
        SevRejection(
            reason=n.values[SEV_REASON],                       # type: ignore[arg-type]
            bmu_id=n.values.get(BMU_ID),                       # type: ignore[arg-type]
            effective_from=n.values.get(SEV_FROM),             # type: ignore[arg-type]
            effective_to=n.values.get(SEV_TO),                 # type: ignore[arg-type]
            settlement_period=n.values.get(SETTLEMENT_PERIOD), # type: ignore[arg-type]
            volume_mwh=n.values.get(SEV_VOLUME),               # type: ignore[arg-type]
        )
        for n in body
        if n.record_type == BSR
    ]


def parse_sev_acceptances(body: list[Node]) -> list[SevAcceptance]:
    return [
        SevAcceptance(bmu_id=n.values[BMU_ID])  # type: ignore[arg-type]
        for n in body
        if n.record_type == BSA
    ]


def parse_sev_warnings(body: list[Node]) -> list[SevWarning]:
    return [
        SevWarning(
            bmu_id=n.values[BMU_ID],                    # type: ignore[arg-type]
            settlement_date=n.values[SETTLEMENT_DATE],  # type: ignore[arg-type]
        )
        for n in body
        if n.record_type == BSX
    ]


def parse_delivered_rejections(body: list[Node]) -> list[DeliveredRejection]:
    return [
        DeliveredRejection(
            reason=n.values[DELIVERED_REASON],                 # type: ignore[arg-type]
            settlement_date=n.values.get(SETTLEMENT_DATE),     # type: ignore[arg-type]
            gsp_group_id=n.values.get(GSP_GROUP_ID),           # type: ignore[arg-type]
            bmu_id=n.values.get(BMU_ID),                       # type: ignore[arg-type]
            import_msid=n.values.get(IMPORT_MSID),             # type: ignore[arg-type]
            export_msid=n.values.get(EXPORT_MSID),             # type: ignore[arg-type]
            settlement_period=n.values.get(SETTLEMENT_PERIOD), # type: ignore[arg-type]
            volume_mwh=n.values.get(DELIVERED_VOLUME),         # type: ignore[arg-type]
        )
        for n in body
        if n.record_type == MSR
    ]


def parse_delivered_confirmation(body: list[Node]) -> DeliveredConfirmation:
    if len(body) != 1 or body[0].record_type != MSF:
        raise ValueError(
            f"expected a single {MSF} record, got {[n.record_type for n in body]}"
        )
    n = body[0]
    return DeliveredConfirmation(
        settlement_date=n.values[SETTLEMENT_DATE],         # type: ignore[arg-type]
        gsp_group_id=n.values.get(GSP_GROUP_ID),           # type: ignore[arg-type]
        bmu_id=n.values.get(BMU_ID),                       # type: ignore[arg-type]
        import_msid=n.values.get(IMPORT_MSID),             # type: ignore[arg-type]
        export_msid=n.values.get(EXPORT_MSID),             # type: ignore[arg-type]
        settlement_period=n.values.get(SETTLEMENT_PERIOD), # type: ignore[arg-type]
        volume_mwh=n.values.get(DELIVERED_VOLUME),         # type: ignore[arg-type]
    )