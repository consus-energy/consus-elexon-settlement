"""Volume and capacity: a full settlement day at portfolio scale.

Register refs V01 to V06. Run against synthetic MSID Pairs, not live assets --
this measures whether the software copes with the file sizes and record counts
a 500-pair portfolio produces, which is a question about our systems and not
about how many batteries are installed.

500 pairs is deliberately above the five-year maximum projection of 375, so
the answer stays valid for the life of the qualification.

WHY THIS MATTERS AT ALL. A P0282 carrying 500 pairs across 48 periods is
24,000 volume records in one file. If constructing and transmitting that takes
longer than the margin before Gate Closure, every submission is late and the
first sign of it is a rejection. The point of this test is to know the number
rather than assume it.

Not run by the default suite -- it takes minutes rather than seconds. Run it
explicitly:

    uv run pytest tests/volume -q -s

The -s matters: the timings are printed, and the timings are the result.
"""

from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal
from pathlib import Path

import pytest

from consus_elexon_settlement import deadlines
from consus_elexon_settlement.flows import delivered as delivered_flow
from consus_elexon_settlement.flows import sev as sev_flow
from consus_elexon_settlement.flows.wman import ActiveUnit, Wman
from consus_elexon_settlement.idd import spec, spec_svaa
from consus_elexon_settlement.idd.file import Header, build

# Above the five-year maximum projection of 375 MSID Pairs.
PAIRS = 500

# Gate Closure is one hour before the settlement period. Construction and
# transmission must leave room for a human to react to a rejection, so we hold
# ourselves to a fraction of that hour rather than to the hour itself.
BUDGET_SECONDS = 60.0

NORMAL_DAY = dt.date(2026, 9, 15)   # 48 periods
LONG_DAY = dt.date(2026, 10, 25)    # 50 periods, the largest case in practice

pytestmark = pytest.mark.volume


def msid(n: int) -> int:
    """A synthetic 13-digit MSID.

    Real MSIDs are allocated by the distributor. These are structurally valid
    and deliberately not real: the test measures file construction, not
    registration.
    """
    return 1500000000000 + n


def bmu(n: int) -> str:
    """A synthetic Secondary BM Unit id, in the BSCP602 format.

    V_ marks a Secondary BM Unit, _F is the GSP Group, CNRG is our MPID.
    """
    return f"V__FCNRG{n % 1000:03d}"


def a_header(file_type: str, sequence: int) -> Header:
    return Header(
        file_type=file_type,
        message_role="D",
        creation_time=dt.datetime.now(dt.timezone.utc),
        from_role_code="VT",
        from_participant_id="CONSUSVT",
        to_role_code="G",
        to_participant_id="UKDC",
        sequence_number=sequence,
        test_flag="TST1",
    )


def periods_for(day: dt.date) -> list[int]:
    return list(range(1, deadlines.periods_in_day(day) + 1))


# --- V03 · delivered volumes at 500 pairs ------------------------------------

def test_delivered_volume_at_portfolio_scale():
    """V03. A full settlement day of Delivered Volumes for 500 MSID Pairs.

    One P0282 carrying every pair and every period, which is how it would be
    submitted: one file per settlement day, not one per pair.
    """
    periods = periods_for(NORMAL_DAY)

    built = time.perf_counter()
    delivered = delivered_flow.Delivered(
        settlement_date=NORMAL_DAY,
        pairs=tuple(
            delivered_flow.PairVolumes(
                import_msid=msid(i),
                export_msid=msid(i) + 500,
                gsp_group_id="_F",
                bmu_id=bmu(i),
                periods=tuple(
                    delivered_flow.DeliveredPeriod(p, Decimal("0.1000"))
                    for p in periods
                ),
            )
            for i in range(PAIRS)
        ),
    )
    domain = time.perf_counter() - built

    rendered = time.perf_counter()
    payload = build(
        spec_svaa.SPEC.flows[delivered_flow.FILE_TYPE],
        a_header(delivered_flow.FILE_TYPE, 1),
        delivered_flow.to_nodes(delivered),
    )
    render = time.perf_counter() - rendered

    records = payload.count(b"\n")
    print(f"\nV03  P0282  {PAIRS} pairs x {len(periods)} periods")
    print(f"     {records:,} records, {len(payload):,} bytes")
    print(f"     domain {domain:.2f}s, render {render:.2f}s, total {domain+render:.2f}s")

    # Header, MSA, then per pair: MSB group, MSC unit, MSJ pair, and one MSP
    # per period. Grouping collapses MSB and MSC where they repeat.
    assert records > PAIRS * len(periods)
    assert domain + render < BUDGET_SECONDS

    # The file must round-trip: a file we cannot read back is a file we cannot
    # reconstruct for an audit.
    parsed = time.perf_counter()
    from consus_elexon_settlement.idd.file import parse
    header, body = parse(payload, spec_svaa.SPEC.flows[delivered_flow.FILE_TYPE])
    reread = delivered_flow.from_nodes(body)
    print(f"     parse {time.perf_counter()-parsed:.2f}s")

    assert len(reread.pairs) == PAIRS
    assert reread.settlement_date == NORMAL_DAY


# --- V03 · expected volumes at 500 pairs -------------------------------------

def test_expected_volume_at_portfolio_scale():
    """V03. Submitted Expected Volumes for the same portfolio.

    SEV is per BM Unit rather than per MSID Pair, but a portfolio of 500 pairs
    across distinct units is the worst case and the one worth measuring.
    """
    periods = periods_for(NORMAL_DAY)

    started = time.perf_counter()
    expected = sev_flow.Sev(
        effective_from=NORMAL_DAY,
        effective_to=NORMAL_DAY,
        units=tuple(
            sev_flow.UnitVolumes(
                bmu_id=f"V__FCNRG{i:03d}" if i < 1000 else bmu(i),
                periods=tuple(
                    sev_flow.ExpectedPeriod(p, Decimal("-0.1200")) for p in periods
                ),
            )
            for i in range(PAIRS)
        ),
    )
    payload = build(
        spec_svaa.SPEC.flows[sev_flow.FILE_TYPE],
        a_header(sev_flow.FILE_TYPE, 2),
        sev_flow.to_nodes(expected),
    )
    elapsed = time.perf_counter() - started

    print(f"\nV03  P0328  {PAIRS} units x {len(periods)} periods")
    print(f"     {payload.count(chr(10).encode()):,} records, {len(payload):,} bytes, {elapsed:.2f}s")

    assert elapsed < BUDGET_SECONDS


# --- V04 · time against the Gate Closure margin ------------------------------

def test_full_day_fits_within_the_gate_closure_margin():
    """V04. Everything one settlement period requires, timed together.

    Gate Closure is an hour before the period. This measures what we spend of
    that hour on construction, so the remainder is available for transmission,
    acceptance, and a person acting on a rejection.
    """
    periods = periods_for(NORMAL_DAY)
    started = time.perf_counter()

    # WMAN: which units are active. One file per settlement period.
    wman_payload = build(
        spec.SPEC.flows["E0511001"],
        a_header("E0511001", 3),
        __import__(
            "consus_elexon_settlement.flows.wman", fromlist=["to_nodes"]
        ).to_nodes(Wman(
            settlement_date=NORMAL_DAY,
            settlement_period=37,
            units=tuple(ActiveUnit(bmu(i)) for i in range(PAIRS)),
        )),
    )

    # SEV for the period.
    sev_payload = build(
        spec_svaa.SPEC.flows[sev_flow.FILE_TYPE],
        a_header(sev_flow.FILE_TYPE, 4),
        sev_flow.to_nodes(sev_flow.Sev(
            effective_from=NORMAL_DAY,
            effective_to=NORMAL_DAY,
            units=tuple(
                sev_flow.UnitVolumes(bmu(i), (
                    sev_flow.ExpectedPeriod(37, Decimal("-0.1200")),
                ))
                for i in range(PAIRS)
            ),
        )),
    )

    elapsed = time.perf_counter() - started
    total = len(wman_payload) + len(sev_payload)

    print(f"\nV04  one settlement period, {PAIRS} units")
    print(f"     WMAN {len(wman_payload):,} bytes, SEV {len(sev_payload):,} bytes")
    print(f"     {elapsed:.2f}s, {elapsed/3600*100:.2f}% of the Gate Closure hour")

    assert elapsed < BUDGET_SECONDS
    assert total > 0


# --- V05 · the 50-period day -------------------------------------------------

def test_fifty_period_day_at_portfolio_scale():
    """V05. The largest volume case that occurs in practice.

    The October clock change produces a 50-period day. Period count is derived
    from the timezone rather than assumed, so this also confirms that
    derivation holds at scale.
    """
    periods = periods_for(LONG_DAY)
    assert len(periods) == 50, "expected a 50-period day"

    started = time.perf_counter()
    delivered = delivered_flow.Delivered(
        settlement_date=LONG_DAY,
        pairs=tuple(
            delivered_flow.PairVolumes(
                import_msid=msid(i),
                gsp_group_id="_F",
                bmu_id=bmu(i),
                periods=tuple(
                    delivered_flow.DeliveredPeriod(p, Decimal("0.1000"))
                    for p in periods
                ),
            )
            for i in range(PAIRS)
        ),
    )
    payload = build(
        spec_svaa.SPEC.flows[delivered_flow.FILE_TYPE],
        a_header(delivered_flow.FILE_TYPE, 5),
        delivered_flow.to_nodes(delivered),
    )
    elapsed = time.perf_counter() - started

    print(f"\nV05  P0282 on a 50-period day, {PAIRS} pairs")
    print(f"     {len(payload):,} bytes, {elapsed:.2f}s")

    assert elapsed < BUDGET_SECONDS


# --- V06 · sequence integrity under load -------------------------------------

@pytest.mark.skipif(
    not __import__("os").environ.get("SETTLEMENT_TEST_DSN"),
    reason="needs a database",
)
def test_sequence_stays_contiguous_across_a_full_day(conn):
    """V06. 48 periods of submissions, sequence numbering checked end to end.

    A gap in the sequence stops ECVAA processing and cannot be corrected
    retrospectively -- it is resolved by agreement with Elexon, not by us. So
    the test is not that allocation is fast but that it is exact.
    """
    from consus_elexon_settlement import db
    from tests.conftest import make_channel

    channel = make_channel(conn, role="VT", participant="CONSUSVT")

    started = time.perf_counter()
    allocated = []
    for period in range(1, 49):
        reserved = db.reserve_file(
            conn,
            channel=channel,
            file_type="E0511001",
            message_role="D",
            creation_time=dt.datetime.now(dt.timezone.utc),
        )
        allocated.append(reserved.sequence_number)
    elapsed = time.perf_counter() - started

    print(f"\nV06  48 sequence allocations in {elapsed:.2f}s")

    assert allocated == list(range(allocated[0], allocated[0] + 48)), \
        "sequence numbers are not contiguous"
    assert len(set(allocated)) == 48, "duplicate sequence number allocated"