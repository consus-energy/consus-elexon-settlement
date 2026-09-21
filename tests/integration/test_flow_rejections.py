"""A rejected WMAN reaches the EMS. The most urgent message this channel carries.

WHY IT IS THE URGENT ONE. A rejected WMAN means SVAA never learns we were
active, so no Deviation Volume is calculated for the period at all (BSC Section
T 4.3.AA.1) -- whatever the ECVN says. A battery that keeps delivering into it
moves for nothing and the trade behind it settles unhedged. The EMS gate closes
on this, and before S-13b it had no way to know.

AGAINST A REAL DATABASE, because the thing under test is which BM Units the
UPDATE actually changed. A period-level exception names no units, so the ids
come out of `db.reject_wman`'s RETURNING clause and nowhere else -- a fake
connection would let the assertions agree with a mental model of that query.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from consus_elexon_settlement import db, states
from consus_elexon_settlement.idd.file import Header, Node
from consus_elexon_settlement.inbound import ecvaa
from consus_elexon_settlement.inbound.handlers import EcvaaHandlers, HandlerError

from ..conftest import built_file
from .test_submissions import RecordingPublisher, _NoClose

DATE = dt.date(2026, 9, 1)
PERIOD = 37
FILENAME = "ECVAAEXCEPT01"

#: ECVAA's own clock. The rejection's `occurred_at` is theirs, not ours --
#: they made the decision.
ISSUED = dt.datetime(2026, 9, 1, 10, 15, tzinfo=dt.timezone.utc)


def _header() -> Header:
    return Header(
        file_type=ecvaa.WMAN_EXCEPTION_FILE_TYPE,
        message_role="D",
        creation_time=ISSUED,
        from_role_code="EC",
        from_participant_id="UKDC",
        to_role_code="VT",
        to_participant_id="CONSUS",
        sequence_number=1,
    )


def _body(reason: str | None, units: tuple[tuple[str, str], ...] = ()) -> list[Node]:
    values: dict = {
        ecvaa.SETTLEMENT_DATE: DATE,
        ecvaa.SETTLEMENT_PERIOD: PERIOD,
    }
    if reason is not None:
        values[ecvaa.REASON] = reason
    children = [
        Node(record_type=ecvaa.WMJ, values={ecvaa.BMU_ID: bmu, ecvaa.REASON: why}, children=[])
        for bmu, why in units
    ]
    return [Node(record_type=ecvaa.WMR, values=values, children=children)]


@pytest.fixture
def submitted_wman(conn, channel):
    """Two BM Units notified for one period, both SUBMITTED."""
    file_id = built_file(conn, channel, file_type="E0511001")
    for bmu in ("V__ACNRG001", "V__ACNRG002"):
        with conn.transaction():
            conn.execute(
                """INSERT INTO wman (outbound_file_id, settlement_date,
                                     settlement_period, bmu_id, active, state)
                        VALUES (%s, %s, %s, %s, true, 'PENDING')""",
                (file_id, DATE, PERIOD, bmu),
            )
    db.record_sent(conn, file_id)
    db.submit_items(conn, "wman", file_id)
    return file_id


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def handlers(conn, publisher) -> EcvaaHandlers:
    return EcvaaHandlers(connect=lambda: _NoClose(conn), flow_publisher=publisher)


def test_a_period_level_rejection_names_every_unit_it_took_out(
    handlers, publisher, submitted_wman
):
    """THE CASE THAT NEEDED `RETURNING`. E0521 with a reason and no WMJ records
    rejects the WHOLE period and names nobody — so the only way to tell the EMS
    which BM Units just lost it is to read back what the UPDATE changed."""
    handlers.wman_exception(_header(), _body("period rejected"), FILENAME)

    assert [m["bmu_id"] for m in publisher.messages] == ["V__ACNRG001", "V__ACNRG002"]
    for message in publisher.messages:
        assert message["event"] == "rejected"
        assert message["settlement_period"] == PERIOD


def test_a_unit_level_rejection_tells_the_ems_about_that_unit_only(
    handlers, publisher, submitted_wman
):
    """Named units reject only those. Treating it as total would close the gate
    on a BM Unit the market never mentioned."""
    handlers.wman_exception(
        _header(), _body(None, (("V__ACNRG001", "not baselined"),)), FILENAME
    )

    assert [m["bmu_id"] for m in publisher.messages] == ["V__ACNRG001"]
    assert publisher.messages[0]["rejection_detail"] == "not baselined"


def test_the_timestamp_is_ECVAAs_not_ours(handlers, publisher, submitted_wman):
    """A rejection is their decision and its time is theirs. There is no `sent`
    reading of one, which is why `rejected_event` takes no basis at all."""
    handlers.wman_exception(_header(), _body("period rejected"), FILENAME)

    message = publisher.messages[0]
    assert message["occurred_at"] == "2026-09-01T10:15:00Z"
    assert message["occurred_at_basis"] == "received"


def test_the_code_is_the_file_type_because_E0521_has_no_finer_one(
    handlers, publisher, submitted_wman
):
    """Unlike E0091, a WMAN exception carries only 80 characters of free text
    (N0187). Inventing a code out of that text would be a vocabulary of ours
    that nobody else uses and that drifts the first time ECVAA rewords."""
    handlers.wman_exception(_header(), _body("period rejected"), FILENAME)

    assert publisher.messages[0]["rejection_code"] == ecvaa.WMAN_EXCEPTION_FILE_TYPE
    assert publisher.messages[0]["rejection_detail"] == "period rejected"


def test_a_rejection_we_have_no_record_of_still_raises(handlers, publisher, conn):
    """The handler's existing contract is unchanged: feedback for something we
    have no record of sending is a correlation failure worth alerting on, and
    it must not be softened into a published event about nothing."""
    with pytest.raises(HandlerError):
        handlers.wman_exception(_header(), _body("period rejected"), FILENAME)

    assert publisher.messages == []


def test_the_rejection_is_recorded_even_when_publishing_fails(
    conn, channel, submitted_wman
):
    """BEST EFFORT, AND THE ORDER OF IMPORTANCE IS STATED. A lost rejection is
    a settlement problem; a missed flow event is a reporting one, and it fails
    SAFE at the far end because no evidence means no dispatch."""

    class Exploding(RecordingPublisher):
        def publish_all(self, messages):
            raise RuntimeError("pubsub is down")

    handlers = EcvaaHandlers(connect=lambda: _NoClose(conn), flow_publisher=Exploding())
    handlers.wman_exception(_header(), _body("period rejected"), FILENAME)

    states_now = conn.execute(
        "SELECT DISTINCT state FROM wman WHERE settlement_date = %s", (DATE,)
    ).fetchall()
    assert states_now == [(states.REJECTED,)]
