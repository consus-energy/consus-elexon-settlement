"""Submitting: the domain rows land, and only move when the file is sent.

Against a real database, because persistence is the thing worth testing here.
File construction is already covered by the flow round-trip tests.

The case that matters most is a FAILED send. The rows must exist, linked to
the archived file, still PENDING, so a retry can find and complete them. A
submitter that recorded nothing on failure would leave a file in the archive
that nothing referenced, and a sequence number spent on a submission with no
record of what it contained.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from consus_elexon_settlement import db, states
from consus_elexon_settlement.archive import LocalArchive
from consus_elexon_settlement.flows.delivered import (
    Delivered,
    DeliveredPeriod,
    PairVolumes,
)
from consus_elexon_settlement.flows.ecvn import ContractVolume, Ecvn
from consus_elexon_settlement.flows.sev import ExpectedPeriod, Sev, UnitVolumes
from consus_elexon_settlement.flows.wman import ActiveUnit, Wman
from consus_elexon_settlement.outbound.sender import Sender
from consus_elexon_settlement.outbound.submissions import Submitter, deviation
from consus_elexon_settlement.outbound.transport import (
    LocalTransport,
    TransportError,
)

from ..conftest import make_channel

DATE = dt.date(2026, 9, 15)
PERIOD = 37
BMU = "V__ACNRG001"
IMPORT_MSID = 1300035399160
EXPORT_MSID = 1300035399161


class FailingTransport:
    """Accepts nothing.

    Stands in for a network blip or an FTP timeout, which are ordinary rather
    than exceptional -- IDD 2.3 gives the FTP success code as our only
    confirmation of sending, so a failure here is genuinely 'we do not know'.
    """

    def send(self, filename: str, payload: bytes) -> None:
        raise TransportError("connection refused")

    def collect(self) -> list[tuple[str, bytes]]:
        return []


def build_submitter(conn, tmp_path: Path, transport) -> Submitter:
    """A submitter over the test connection.

    The connection factory returns the same connection every time rather than
    opening new ones. Production opens per operation; here a single connection
    keeps everything inside the fixture's transaction so the truncate cleans
    up properly.
    """
    sender = Sender(
        connect=lambda: _NoClose(conn),
        archive=LocalArchive(root=tmp_path / "archive"),
        transport=transport,
    )
    return Submitter(connect=lambda: _NoClose(conn), sender=sender)


class _NoClose:
    """Wraps a connection so `with` does not close or commit it.

    Sender and Submitter both use `with self._connect() as conn`, which on a
    real psycopg connection commits and closes. In tests we want one
    connection for the whole case, so this makes the context manager a no-op
    while passing everything else through.
    """

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def submitter(conn, tmp_path: Path) -> Submitter:
    return build_submitter(conn, tmp_path, LocalTransport(
        outbox=tmp_path / "out", inbox=tmp_path / "in",
    ))


@pytest.fixture
def failing_submitter(conn, tmp_path: Path) -> Submitter:
    return build_submitter(conn, tmp_path, FailingTransport())


@pytest.fixture
def ecvaa(conn) -> db.Channel:
    return make_channel(conn, role="VT", participant="CONSUSVT")


@pytest.fixture
def agent(conn) -> db.Channel:
    return make_channel(conn, role="EN", participant="CONSUSEN")


# --- fixtures for the four flows --------------------------------------------

def a_wman() -> Wman:
    return Wman(DATE, PERIOD, (ActiveUnit(BMU),))


def an_ecvn() -> Ecvn:
    return Ecvn(
        ecvnaa_id="AUTH000001",
        ecvn_ecvnaa_id="AUTH000001",
        reference_code="REF0000001",
        effective_from=DATE,
        effective_to=DATE,
        volumes=(
            ContractVolume(PERIOD, Decimal("0.400")),
            ContractVolume(PERIOD + 1, Decimal("0.300")),
        ),
    )


def a_sev(effective_to: dt.date | None = DATE) -> Sev:
    return Sev(
        effective_from=DATE,
        effective_to=effective_to,
        units=(UnitVolumes(BMU, (
            ExpectedPeriod(PERIOD, Decimal("-0.1200")),
        )),),
    )


def a_delivered() -> Delivered:
    return Delivered(DATE, (PairVolumes(
        import_msid=IMPORT_MSID,
        export_msid=EXPORT_MSID,
        gsp_group_id="_A",
        bmu_id=BMU,
        periods=(DeliveredPeriod(PERIOD, Decimal("0.1000")),),
    ),))


# --- the happy path ---------------------------------------------------------

def test_wman_records_a_row_per_unit(submitter, ecvaa, conn):
    sent = submitter.wman(ecvaa, Wman(DATE, PERIOD, (
        ActiveUnit("V__ACNRG001"), ActiveUnit("V__ACNRG002"),
    )))
    assert sent.delivered

    rows = conn.execute(
        """SELECT bmu_id, state FROM wman
            WHERE outbound_file_id = %s ORDER BY bmu_id""",
        (sent.file_id,),
    ).fetchall()
    assert rows == [
        ("V__ACNRG001", states.SUBMITTED),
        ("V__ACNRG002", states.SUBMITTED),
    ]


def test_ecvn_records_notification_and_periods(submitter, agent, conn):
    sent = submitter.ecvn(agent, an_ecvn(), ecvnaa_key="KEY1234567")

    notification = conn.execute(
        """SELECT id, reference_code, effective_from, effective_to, state
             FROM notification WHERE outbound_file_id = %s""",
        (sent.file_id,),
    ).fetchone()
    assert notification[1] == "REF0000001"
    assert notification[4] == states.SUBMITTED

    periods = conn.execute(
        """SELECT settlement_period, volume_mwh, state FROM notification_period
            WHERE notification_id = %s ORDER BY settlement_period""",
        (notification[0],),
    ).fetchall()
    assert [(p[0], p[1]) for p in periods] == [
        (PERIOD, Decimal("0.400")), (PERIOD + 1, Decimal("0.300")),
    ]
    # Periods move with their parent -- see ADR-0006. Leaving them PENDING
    # would make the later cascade find nothing.
    assert {p[2] for p in periods} == {states.SUBMITTED}


def test_ecvn_key_is_not_persisted(submitter, agent, conn):
    """The ECVNAA Key travels on the wire but must not be stored.

    A credential in a settlement database is a credential in every backup of
    that database. It is passed to the submitter and used at build time only.
    """
    submitter.ecvn(agent, an_ecvn(), ecvnaa_key="KEY1234567")

    columns = conn.execute(
        """SELECT column_name FROM information_schema.columns
            WHERE table_name = 'notification'"""
    ).fetchall()
    assert not any("key" in c[0].lower() for c in columns)


def test_sev_records_unit_and_periods(submitter, ecvaa, conn):
    sent = submitter.sev(ecvaa, a_sev())

    unit = conn.execute(
        """SELECT id, bmu_id, effective_from, effective_to, state
             FROM sev WHERE outbound_file_id = %s""",
        (sent.file_id,),
    ).fetchone()
    assert unit[1] == BMU
    assert unit[3] == DATE
    assert unit[4] == states.SUBMITTED

    periods = conn.execute(
        "SELECT settlement_period, volume_mwh, state FROM sev_period WHERE sev_id = %s",
        (unit[0],),
    ).fetchall()
    assert periods == [(PERIOD, Decimal("-0.1200"), states.SUBMITTED)]


def test_default_sev_has_no_effective_to(submitter, ecvaa, conn):
    """A Default SEV stands until replaced. NULL effective_to is the marker,
    not a missing value -- it is the safety net that stops Settlement Expected
    Volume going NULL when no per-period value is registered (BSCP602
    2.13.7)."""
    sent = submitter.sev(ecvaa, a_sev(effective_to=None))

    effective_to = conn.execute(
        "SELECT effective_to FROM sev WHERE outbound_file_id = %s", (sent.file_id,),
    ).fetchone()[0]
    assert effective_to is None


def test_delivered_records_pair_and_periods(submitter, ecvaa, conn):
    sent = submitter.delivered(ecvaa, a_delivered())

    pair = conn.execute(
        """SELECT id, import_msid, export_msid, gsp_group_id, bmu_id, state
             FROM delivered_volume WHERE outbound_file_id = %s""",
        (sent.file_id,),
    ).fetchone()
    assert pair[1] == IMPORT_MSID
    assert pair[2] == EXPORT_MSID
    assert pair[5] == states.SUBMITTED

    periods = conn.execute(
        """SELECT settlement_period, volume_mwh FROM delivered_volume_period
            WHERE delivered_volume_id = %s""",
        (pair[0],),
    ).fetchall()
    assert periods == [(PERIOD, Decimal("0.1000"))]


def test_delivered_without_an_export_meter(submitter, ecvaa, conn):
    """MSI makes Export MSID optional. A site with no export meter is the
    normal case for a battery that does not export."""
    sent = submitter.delivered(ecvaa, Delivered(DATE, (PairVolumes(
        import_msid=IMPORT_MSID,
        gsp_group_id="_A",
        bmu_id=BMU,
        periods=(DeliveredPeriod(PERIOD, Decimal("0.1000")),),
    ),)))

    export = conn.execute(
        "SELECT export_msid FROM delivered_volume WHERE outbound_file_id = %s",
        (sent.file_id,),
    ).fetchone()[0]
    assert export is None


# --- the failure path, which is the point of this file ----------------------

def test_failed_send_still_records_the_rows(failing_submitter, ecvaa, conn):
    """A transport failure must leave a complete record.

    The file is reserved, built and archived before transport is attempted, so
    the sequence number is spent either way. Recording nothing would leave a
    file in the archive that nothing references and a number spent on a
    submission we cannot describe.
    """
    sent = failing_submitter.wman(ecvaa, a_wman())

    assert not sent.delivered
    assert "connection refused" in sent.error

    row = conn.execute(
        "SELECT bmu_id, state FROM wman WHERE outbound_file_id = %s",
        (sent.file_id,),
    ).fetchone()
    assert row == (BMU, states.PENDING)


def test_failed_send_leaves_items_pending_not_submitted(
    failing_submitter, agent, conn
):
    """PENDING, not SUBMITTED. Marking them submitted would claim we had told
    Elexon something we had not, and the reconciliation would then look for a
    response that is never coming."""
    sent = failing_submitter.ecvn(agent, an_ecvn(), ecvnaa_key="KEY1234567")

    notification = conn.execute(
        "SELECT id, state FROM notification WHERE outbound_file_id = %s",
        (sent.file_id,),
    ).fetchone()
    assert notification[1] == states.PENDING

    periods = conn.execute(
        "SELECT state FROM notification_period WHERE notification_id = %s",
        (notification[0],),
    ).fetchall()
    assert {p[0] for p in periods} == {states.PENDING}


def test_failed_send_marks_the_file_send_failed(failing_submitter, ecvaa, conn):
    sent = failing_submitter.wman(ecvaa, a_wman())

    state = conn.execute(
        "SELECT state FROM outbound_file WHERE id = %s", (sent.file_id,),
    ).fetchone()[0]
    assert state == states.SEND_FAILED


def test_failed_send_archives_the_bytes(failing_submitter, ecvaa, conn, tmp_path):
    """Build once, send many (ADR-0002). The bytes must survive the failure so
    the retry sends the same file under the same sequence number -- rebuilding
    would allocate a second number and leave a permanent gap at the first."""
    sent = failing_submitter.wman(ecvaa, a_wman())

    uri = conn.execute(
        "SELECT gcs_uri FROM outbound_file WHERE id = %s", (sent.file_id,),
    ).fetchone()[0]
    assert uri

    archive = LocalArchive(root=tmp_path / "archive")
    assert archive.get(uri) == sent.payload


def test_retry_after_failure_completes_the_submission(
    conn, tmp_path, ecvaa
):
    """The whole point of recording on failure: a retry finds the rows and
    completes them without rebuilding anything."""
    failing = build_submitter(conn, tmp_path, FailingTransport())
    sent = failing.wman(ecvaa, a_wman())
    assert not sent.delivered

    # Transport comes back. Same archive, so retry reads the same bytes.
    working = LocalTransport(outbox=tmp_path / "out", inbox=tmp_path / "in")
    sender = Sender(
        connect=lambda: _NoClose(conn),
        archive=LocalArchive(root=tmp_path / "archive"),
        transport=working,
    )
    retried = sender.retry(sent.file_id)

    assert retried.delivered
    assert retried.payload == sent.payload
    assert retried.sequence_number == sent.sequence_number

    # The items are still PENDING: retry sends the file, it does not know
    # about domain rows. Completing them is the caller's job.
    state = conn.execute(
        "SELECT state FROM wman WHERE outbound_file_id = %s", (sent.file_id,),
    ).fetchone()[0]
    assert state == states.PENDING

    db.submit_items(conn, "wman", sent.file_id)
    state = conn.execute(
        "SELECT state FROM wman WHERE outbound_file_id = %s", (sent.file_id,),
    ).fetchone()[0]
    assert state == states.SUBMITTED


# --- sequence discipline ----------------------------------------------------

def test_each_submission_takes_the_next_sequence_number(submitter, ecvaa):
    first = submitter.wman(ecvaa, Wman(DATE, PERIOD, (ActiveUnit(BMU),)))
    second = submitter.wman(ecvaa, Wman(DATE, PERIOD + 1, (ActiveUnit(BMU),)))

    assert second.sequence_number == first.sequence_number + 1


def test_the_two_identities_have_separate_counters(submitter, ecvaa, agent):
    """WMAN goes out as the VTP, ECVN as the ECVN Agent. Separate channels,
    separate counters -- mixing them corrupts both in a way that cannot be
    corrected retrospectively (ADR-0004)."""
    vtp_first = submitter.wman(ecvaa, a_wman())
    agent_first = submitter.ecvn(agent, an_ecvn(), ecvnaa_key="KEY1234567")

    # Both start at 1 on their own channel.
    assert vtp_first.sequence_number == agent_first.sequence_number == 1

    vtp_second = submitter.wman(
        ecvaa, Wman(DATE, PERIOD + 1, (ActiveUnit(BMU),))
    )
    assert vtp_second.sequence_number == 2


# --- the deviation helper ---------------------------------------------------

def test_deviation_of_a_turn_down_is_positive():
    """CVA convention: positive is Export, negative is Import.

    Expected import 0.12 MWh is -0.1200. Actual import 0.02 is -0.0200. The
    battery discharged 0.10 MWh, which reduces import -- an export-direction
    change, so the deviation is positive.
    """
    assert deviation(Decimal("-0.1200"), Decimal("-0.0200")) == Decimal("0.1000")


def test_deviation_of_a_turn_up_is_negative():
    """Charging increases import, which is a negative deviation. Under current
    rules this leg is not notified, but it still has to compute correctly --
    P510 would make it settleable."""
    assert deviation(Decimal("-0.1200"), Decimal("-0.2200")) == Decimal("-0.1000")


def test_deviation_of_no_action_is_zero():
    assert deviation(Decimal("-0.1200"), Decimal("-0.1200")) == Decimal("0")