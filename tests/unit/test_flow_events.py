"""The return channel's message contract, from this side.

THE EXECUTABLE HALF OF A CROSS-REPO AGREEMENT. The consumer is
`services/vtp/src/vtp/return_channel.py` in consus-prod, and its own tests
state what it will accept. These state what we will produce. Where the two
disagree the bridge is broken in a way no single repo's test suite would
catch, so each field asserted here is a field asserted there.

PURE, WITH NO CLIENT AND NO DATABASE. Same reason `ems/messages.py` is pure:
the contract is the part most likely to drift, and it should be testable
without either side of the wire.
"""

from __future__ import annotations

import datetime as dt

import pytest

from consus_elexon_settlement.ems import flow_events as fe

DATE = dt.date(2026, 9, 15)
PERIOD = 37
BMU = "V__FCNRG001"
WHEN = dt.datetime(2026, 9, 15, 16, 30, tzinfo=dt.timezone.utc)


def _submitted(**kw):
    args = {
        "flow": fe.WMAN,
        "bmu_id": BMU,
        "settlement_date": DATE,
        "settlement_period": PERIOD,
        "occurred_at": WHEN,
        "basis": fe.BASIS_SENT,
    }
    args.update(kw)
    return fe.submitted_event(**args)


def _rejected(**kw):
    args = {
        "flow": fe.WMAN,
        "bmu_id": BMU,
        "settlement_date": DATE,
        "settlement_period": PERIOD,
        "occurred_at": WHEN,
        "rejection_code": "E0521001",
    }
    args.update(kw)
    return fe.rejected_event(**args)


# --------------------------------------------------------------- the shape


def test_a_submission_carries_every_field_the_ems_requires():
    """Every one of these is `_require`d by the consumer's parser. A field
    missing here is a message acked and discarded over there, with nothing
    coming back to tell us — the bridge is one way in both directions."""
    assert _submitted() == {
        "kind": "flow_event",
        "flow": "wman",
        "event": "submitted",
        "bmu_id": BMU,
        "settlement_date": "2026-09-15",
        "settlement_period": 37,
        "occurred_at": "2026-09-15T16:30:00Z",
        "occurred_at_basis": "sent",
    }


def test_a_rejection_carries_its_code_and_detail():
    message = _rejected(rejection_detail="BM Unit not baselined")

    assert message["event"] == "rejected"
    assert message["rejection_code"] == "E0521001"
    assert message["rejection_detail"] == "BM Unit not baselined"


def test_a_rejection_omits_an_absent_detail_rather_than_sending_null():
    """E0521's reason is 80 characters of optional free text. Absent is absent;
    a null would read at the far end as a detail somebody chose to blank."""
    assert "rejection_detail" not in _rejected()


def test_the_settlement_period_is_a_json_number_not_a_string():
    """The consumer refuses a string here, and a `bool` is not period 1 either.
    Volumes are strings and periods are numbers, in both directions."""
    assert isinstance(_submitted()["settlement_period"], int)


# ---------------------------------------------------------- whose clock


def test_a_rejection_is_always_ECVAAs_clock():
    """No basis parameter exists, and the value is fixed.

    We do not reject a flow — we relay one ECVAA issued, and the issue time is
    theirs. The consumer refuses a rejection carrying any other basis.
    """
    assert _rejected()["occurred_at_basis"] == "received"
    with pytest.raises(TypeError):
        fe.rejected_event(
            flow=fe.WMAN, bmu_id=BMU, settlement_date=DATE, settlement_period=PERIOD,
            occurred_at=WHEN, rejection_code="E0521001", basis=fe.BASIS_SENT,
        )


def test_a_submission_basis_is_required_and_checked():
    """NOT DEFAULTED. `sent` would make the day the ADT path lands a silent
    behaviour change; `received` would be a claim we cannot back."""
    with pytest.raises(TypeError):
        fe.submitted_event(
            flow=fe.WMAN, bmu_id=BMU, settlement_date=DATE,
            settlement_period=PERIOD, occurred_at=WHEN,
        )
    with pytest.raises(fe.FlowEventError, match="basis"):
        _submitted(basis="guessed")


# ------------------------------------------------------------- refusals


def test_there_is_no_accepted_event_to_build():
    """ECVAA Service Description v26.0 section 9A gives a Virtual Trading Party
    no acceptance signal, so the strongest fact this channel can carry is
    "submitted, and no rejection since". There is no function that produces
    `accepted` — the capability is absent rather than declined."""
    assert not hasattr(fe, "accepted_event")
    assert "accepted" not in (fe.SUBMITTED, fe.REJECTED)


def test_a_naive_timestamp_is_refused_rather_than_assumed_utc():
    """The EMS compares this against Gate Closure, a UTC instant derived from a
    UK LOCAL calendar. An hour in the wrong direction turns a late submission
    into a punctual one on every BST day of the year — and its parser would
    refuse the message, in its logs, in another project."""
    with pytest.raises(fe.FlowEventError, match="timezone"):
        _submitted(occurred_at=dt.datetime(2026, 9, 15, 16, 30))


def test_a_non_utc_timestamp_is_converted_not_refused():
    """Aware is enough; the wire format is UTC and the conversion is exact."""
    london = dt.timezone(dt.timedelta(hours=1))
    message = _submitted(occurred_at=dt.datetime(2026, 9, 15, 17, 30, tzinfo=london))

    assert message["occurred_at"] == "2026-09-15T16:30:00Z"


def test_a_rejection_with_no_code_is_refused():
    """A rejection with no reason is a dead end at settlement, and this is the
    only place the code exists in a form the EMS can see — the file it arrived
    in stays here."""
    with pytest.raises(fe.FlowEventError, match="code"):
        _rejected(rejection_code="")


def test_an_unknown_flow_is_refused():
    with pytest.raises(fe.FlowEventError, match="delivered"):
        _submitted(flow="delivered")


def test_a_period_outside_the_settlement_day_is_refused():
    """1..50, because a settlement day is 46, 48 or 50 periods long. The EMS
    checks the number against its own date; this is the outer bound, so an
    obviously wrong value does not travel."""
    with pytest.raises(fe.FlowEventError, match="1..50"):
        _submitted(settlement_period=51)
    with pytest.raises(fe.FlowEventError, match="1..50"):
        _submitted(settlement_period=0)


def test_an_empty_bmu_is_refused():
    """The EMS gate is per BM Unit; a message without one cannot open or close
    anything and would sit in its table unattributable."""
    with pytest.raises(fe.FlowEventError, match="bmu_id"):
        _submitted(bmu_id="")
