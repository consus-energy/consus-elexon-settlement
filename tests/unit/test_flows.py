"""Round-trip tests for the outbound flow modules.

Each flow is built to bytes and parsed back. Equality of the domain object is
the assertion: it catches a field written to the wrong item id, a group nested
at the wrong level, and a value that does not survive encoding -- all of which
would otherwise surface as a rejection from Elexon rather than a test failure.

Values are chosen to fit the spec's field widths. BM Unit ids are text(11) and
MSIDs are integer(13); a fixture that exceeds either is rejected at build time,
which is correct behaviour but makes for a confusing test failure.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from consus_elexon_settlement.flows import delivered, ecvn, sev, wman
from consus_elexon_settlement.idd import file, spec, spec_svaa

UTC = dt.timezone.utc
BMU = "2__ABCDE001"          # text(11)
DATE = dt.date(2026, 9, 1)


def header(file_type: str, role: str, participant: str, sequence: int) -> file.Header:
    return file.Header(
        file_type=file_type,
        message_role="D",
        # IDD 2.2.2: header times are GMT, and fields.py requires an aware
        # datetime rather than assuming the local zone.
        creation_time=dt.datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        from_role_code=role,
        from_participant_id=participant,
        to_role_code="EC",
        to_participant_id="ECVAA",
        sequence_number=sequence,
        test_flag="TST1",       # text(4)
    )


def round_trip(flow, hdr, nodes):
    payload = file.build(flow, hdr, nodes)
    _, body = file.parse(payload, flow)
    return body


def test_wman_round_trip():
    original = wman.Wman(DATE, 37, (wman.ActiveUnit(BMU),))
    body = round_trip(
        spec.SPEC.flows[wman.FILE_TYPE],
        header(wman.FILE_TYPE, "VT", "CONSUSVT", 1),
        wman.to_nodes(original),
    )
    assert wman.from_nodes(body) == original


def test_wman_rejects_empty_units():
    # WMA has cardinality 1-*, so a notification naming no units is not a
    # valid file. Caught in the domain object rather than at build time, so
    # the error names the rule rather than the record type.
    with pytest.raises(ValueError, match="1-"):
        wman.Wman(DATE, 37, ())


def test_wman_rejects_period_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        wman.Wman(DATE, 51, (wman.ActiveUnit(BMU),))


def test_wman_accepts_period_50():
    # IDD 2.2.9: a long clock-change day has 50 periods. Never assume 48.
    assert wman.Wman(DATE, 50, (wman.ActiveUnit(BMU),)).settlement_period == 50


def test_ecvn_round_trip():
    original = ecvn.Ecvn(
        ecvnaa_id="AUTH000001",
        ecvn_ecvnaa_id="AUTH000001",
        reference_code="REF0000001",
        effective_from=DATE,
        volumes=(ecvn.ContractVolume(37, Decimal("0.900")),),
    )
    body = round_trip(
        spec.SPEC.flows[ecvn.FILE_TYPE],
        header(ecvn.FILE_TYPE, "EN", "CONSUSEN", 1),
        ecvn.to_nodes(original, ecvnaa_key="KEY1234567"),
    )
    parsed, key = ecvn.from_nodes(body)
    assert parsed == original
    # The key travels on the wire but is deliberately not part of the domain
    # object, so that an Ecvn can be stored and replayed without it.
    assert key == "KEY1234567"


def test_ecvn_rejects_unsorted_periods():
    # IDD 2.2.4 requires records in spec order. Sorting silently would hide a
    # caller bug that only surfaces during reconciliation.
    with pytest.raises(ValueError, match="ascending"):
        ecvn.Ecvn(
            "AUTH000001", "AUTH000001", "REF0000001", DATE,
            (ecvn.ContractVolume(38, Decimal("0.1")),
             ecvn.ContractVolume(37, Decimal("0.2"))),
        )


def test_ecvn_withdrawal_has_no_volumes():
    # CD9 is 0-*, so a file with no volumes is structurally valid.
    withdrawal = ecvn.Ecvn("AUTH000001", "AUTH000001", "REF0000001", DATE, ())
    assert withdrawal.is_withdrawal


def test_sev_round_trip_per_period():
    original = sev.Sev(
        effective_from=DATE,
        effective_to=DATE,
        units=(sev.UnitVolumes(BMU, (sev.ExpectedPeriod(37, Decimal("-1.2500")),)),),
    )
    body = round_trip(
        spec_svaa.SPEC.flows[sev.FILE_TYPE],
        header(sev.FILE_TYPE, "VT", "CONSUSVT", 2),
        sev.to_nodes(original),
    )
    assert sev.from_nodes(body) == original
    assert not original.is_default


def test_sev_default_has_no_end_date():
    # BSCP602 2.13.1: a Default SEV has no Effective To Date and stands until
    # replaced. It is the standing fallback that stops Settlement Expected
    # Volume going null when no per-period value is registered (2.13.7).
    default = sev.Sev(DATE, (sev.UnitVolumes(BMU, (sev.ExpectedPeriod(1, Decimal("0")),)),))
    assert default.is_default
    assert default.effective_to is None


def test_sev_import_converts_to_negative():
    # CVA convention: positive is Export, negative is Import. Site load is an
    # import, so a site-load expected volume is negative. One conversion
    # function, so the sign confusion lives in one place.
    assert sev.import_to_cva(Decimal("1.5")) == Decimal("-1.5")


def test_delivered_round_trip():
    original = delivered.Delivered(
        settlement_date=DATE,
        pairs=(
            delivered.PairVolumes(
                import_msid=1234567890123,         # integer(13)
                periods=(delivered.DeliveredPeriod(37, Decimal("0.9000")),),
                gsp_group_id="_A",
                bmu_id=BMU,
            ),
        ),
    )
    body = round_trip(
        spec_svaa.SPEC.flows[delivered.FILE_TYPE],
        header(delivered.FILE_TYPE, "VT", "CONSUSVT", 3),
        delivered.to_nodes(original),
    )
    assert delivered.from_nodes(body) == original


def test_delivered_groups_pairs_by_gsp_and_unit():
    """The file nests GSP group -> BM Unit -> MSID pair; storage is flat.

    Two pairs in the same group and unit must produce one MSB and one MSC with
    two MSI children, not two of each. Getting this wrong produces a file that
    parses but misrepresents the portfolio.
    """
    original = delivered.Delivered(
        DATE,
        (
            delivered.PairVolumes(1111111111111, (delivered.DeliveredPeriod(37, Decimal("0.1")),), "_A", BMU),
            delivered.PairVolumes(2222222222222, (delivered.DeliveredPeriod(37, Decimal("0.2")),), "_A", BMU),
        ),
    )
    nodes = delivered.to_nodes(original)
    msa = nodes[0]
    assert len(msa.of_type("MSB")) == 1
    msb = msa.of_type("MSB")[0]
    assert len(msb.of_type("MSC")) == 1
    assert len(msb.of_type("MSC")[0].of_type("MSI")) == 2


def test_delivered_export_msid_optional():
    # MSI has Export MSID optional, which is how a pair with no export meter
    # is expressed -- the normal case for a battery that does not export.
    pair = delivered.PairVolumes(
        1234567890123, (delivered.DeliveredPeriod(1, Decimal("0")),), "_A", BMU
    )
    assert pair.export_msid is None