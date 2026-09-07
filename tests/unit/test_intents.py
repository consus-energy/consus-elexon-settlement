"""Intent lifecycle.

Pure logic, no database. resolve() is the function everything above it
depends on: it decides when an intent is done, when it can be retried, and
when it has run out of time. Getting it wrong means either a retry loop
attempting something that can never succeed, or an intent that reads finished
while a rejection is still possible.

The distinction being tested throughout is ACTED means ACCEPTED, not sent.
Sending is immediate; acceptance arrives minutes to hours later. An intent
sits in ACTING for that gap, and that is the whole point -- it is the window
in which a human can still act.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from consus_elexon_settlement import intents

DATE = dt.date(2026, 9, 15)
PERIOD = 37
BMU = "V__ACNRG001"

A = intents.ACCEPTED
P = intents.PENDING
S = intents.SENT
F = intents.FAILED
R = intents.FLOW_REJECTED


def an_intent(**overrides) -> intents.Intent:
    defaults = dict(
        settlement_date=DATE,
        settlement_period=PERIOD,
        bmu_id=BMU,
        expected_mwh=Decimal("-0.1200"),
        contracted_mwh=Decimal("0.400"),
        ecvnaa_id="AUTH000001",
        ecvn_ecvnaa_id="AUTH000001",
    )
    return intents.Intent(**{**defaults, **overrides})


# --- resolve: the core --------------------------------------------------------

def test_all_accepted_is_acted():
    assert intents.resolve({"wman": A, "ecvn": A, "sev": A}, False) == intents.ACTED


def test_sent_but_not_accepted_is_acting():
    """The gap between sending and acceptance. Not a problem, and not done.

    Under the alternative -- ACTED on send -- this would read finished twenty
    minutes before we know whether ECVAA agreed, and the sweep would have
    nothing outstanding to show a human.
    """
    assert intents.resolve({"wman": A, "ecvn": S, "sev": A}, False) == intents.ACTING


def test_nothing_attempted_is_received():
    assert intents.resolve({"wman": P, "ecvn": P, "sev": P}, False) == intents.RECEIVED


def test_no_flows_yet_is_received():
    """An intent recorded before its flows have been created."""
    assert intents.resolve({}, False) == intents.RECEIVED


def test_a_failure_is_partial_while_time_remains():
    assert intents.resolve({"wman": A, "ecvn": F, "sev": A}, False) == intents.PARTIAL


def test_a_failure_past_the_deadline_is_missed():
    """The deadline is checked BEFORE the failure.

    PARTIAL invites a retry, and a retry after Gate Closure spends a sequence
    number on a file central systems will refuse. MISSED is terminal for
    exactly that reason.
    """
    assert intents.resolve({"wman": A, "ecvn": F, "sev": A}, True) == intents.MISSED


def test_fully_accepted_stays_acted_even_past_the_deadline():
    """An intent that succeeded before Gate Closure does not become MISSED
    because someone looked at it afterwards."""
    assert intents.resolve({"wman": A, "ecvn": A, "sev": A}, True) == intents.ACTED


def test_a_rejection_is_partial_while_time_remains():
    """A rejection is not retryable under this intent -- the content was
    refused, so resending it would be refused again. But a new intent at the
    next revision could still succeed, and that is the EMS's call, not ours."""
    assert intents.resolve({"wman": A, "ecvn": R, "sev": A}, False) == intents.PARTIAL


def test_a_rejection_past_the_deadline_is_missed():
    assert intents.resolve({"wman": A, "ecvn": R, "sev": A}, True) == intents.MISSED


def test_a_rejection_outranks_a_pending_flow():
    """Mixed states resolve to the most serious. A rejection with work still
    pending is still PARTIAL: the pending work may succeed, the rejection
    will not."""
    assert intents.resolve({"wman": R, "ecvn": P, "sev": P}, False) == intents.PARTIAL


# --- retryable flows ---------------------------------------------------------

def test_retry_skips_accepted_flows():
    """Resending an accepted flow would spend a sequence number on a
    duplicate, and ECVAA would hold two notifications for one position."""
    assert intents.retryable_flows({"wman": A, "ecvn": F, "sev": P}) == ["ecvn", "sev"]


def test_retry_skips_rejected_flows():
    """A rejection is a decision about content. The same content would be
    refused again for the same reason."""
    assert intents.retryable_flows({"wman": A, "ecvn": R, "sev": F}) == ["sev"]


def test_retry_skips_sent_but_unacknowledged_flows():
    """SENT means it reached transport and we are waiting. Resending would
    duplicate a file that may be about to be accepted."""
    assert intents.retryable_flows({"wman": S, "ecvn": F, "sev": A}) == ["ecvn"]


def test_nothing_to_retry_when_all_accepted():
    assert intents.retryable_flows({"wman": A, "ecvn": A, "sev": A}) == []


# --- transitions -------------------------------------------------------------

def test_legal_intent_transitions():
    intents.check_intent_transition(intents.RECEIVED, intents.ACTING)
    intents.check_intent_transition(intents.ACTING, intents.ACTED)
    intents.check_intent_transition(intents.ACTING, intents.PARTIAL)
    # A retry reopens a partial intent.
    intents.check_intent_transition(intents.PARTIAL, intents.ACTING)


def test_acted_is_terminal():
    """Nothing follows success. An intent that could reopen would mean a
    settled position was not settled after all."""
    with pytest.raises(intents.IntentError, match="terminal"):
        intents.check_intent_transition(intents.ACTED, intents.ACTING)


def test_missed_is_terminal():
    """The deadline cannot be un-passed. Allowing a retry from MISSED is the
    loop this state exists to prevent."""
    with pytest.raises(intents.IntentError, match="terminal"):
        intents.check_intent_transition(intents.MISSED, intents.ACTING)


def test_an_intent_cannot_skip_straight_to_acted():
    """Every intent passes through ACTING. Going directly would mean claiming
    acceptance for flows never sent."""
    with pytest.raises(intents.IntentError):
        intents.check_intent_transition(intents.RECEIVED, intents.ACTED)


def test_flow_cannot_be_accepted_without_being_sent():
    with pytest.raises(intents.IntentError):
        intents.check_flow_transition(intents.PENDING, intents.ACCEPTED)


def test_failed_flow_can_be_retried():
    """Nothing reached central systems, so the same bytes can go again."""
    intents.check_flow_transition(intents.FAILED, intents.SENT)


def test_rejected_flow_cannot_be_retried():
    with pytest.raises(intents.IntentError, match="terminal"):
        intents.check_flow_transition(intents.FLOW_REJECTED, intents.SENT)


# --- open and terminal -------------------------------------------------------

def test_open_states_are_what_the_sweep_looks_for():
    """An open intent approaching Gate Closure is what a human needs to see."""
    assert intents.is_open(intents.RECEIVED)
    assert intents.is_open(intents.ACTING)
    assert intents.is_open(intents.PARTIAL)

    assert not intents.is_open(intents.ACTED)
    assert not intents.is_open(intents.MISSED)
    assert not intents.is_open(intents.REJECTED)


def test_open_and_terminal_partition_the_states():
    """Every state is one or the other. A state that is neither would be
    invisible to both the sweep and the completion check."""
    assert (intents.OPEN_INTENT_STATES | intents.TERMINAL_INTENT_STATES) == \
        intents.INTENT_STATES
    assert not (intents.OPEN_INTENT_STATES & intents.TERMINAL_INTENT_STATES)


# --- the intent itself -------------------------------------------------------

def test_the_key_is_natural_not_random():
    """Pub/Sub delivers at least once, and the EMS can also send the same
    decision twice by mistake. A UUID would make those look different; this
    makes them the same case."""
    first = an_intent()
    duplicate = an_intent()
    assert first.key == duplicate.key


def test_revision_changes_the_key():
    """Trading the same period again is a new decision, not a duplicate.
    Without revision the two are indistinguishable and we would either reject
    a real change or double-submit a repeat."""
    assert an_intent().key != an_intent(revision=2).key


def test_gate_closure_is_an_hour_before_the_period():
    from consus_elexon_settlement import deadlines

    intent = an_intent()
    assert intent.gate_closure == deadlines.gate_closure(DATE, PERIOD)


def test_an_intent_knows_when_it_arrived_too_late():
    """Submitting after Gate Closure is worse than not submitting: a rejected
    file, a spent sequence number, and a record suggesting we tried."""
    intent = an_intent()
    before = intent.gate_closure - dt.timedelta(minutes=5)
    after = intent.gate_closure + dt.timedelta(minutes=5)

    assert not intent.arrived_too_late(before)
    assert intent.arrived_too_late(after)


def test_period_out_of_range_is_rejected():
    with pytest.raises(ValueError, match="out of range"):
        an_intent(settlement_period=51)


def test_period_fifty_is_accepted():
    """Clock-change days have 50 periods. Never assume 48."""
    assert an_intent(settlement_period=50).settlement_period == 50


def test_expected_volume_precision_is_four_places():
    """decimal(14,4) on the wire. More precision than the format carries
    would be silently truncated by the field encoder."""
    with pytest.raises(ValueError, match="four decimal places"):
        an_intent(expected_mwh=Decimal("-0.12345"))


def test_contracted_volume_precision_is_three_places():
    """decimal(10,3) for ECVN. Different from the SEV field width, which is
    the kind of asymmetry that is easy to miss."""
    with pytest.raises(ValueError, match="three decimal places"):
        an_intent(contracted_mwh=Decimal("0.4001"))


def test_the_gateway_does_not_judge_magnitudes():
    """A volume that is wrong but well formed is the EMS's problem.

    Refusing it here would mean inventing a business rule we do not own, and
    the gateway holds no view on what a plausible volume is.
    """
    huge = an_intent(contracted_mwh=Decimal("9999999.999"))
    assert huge.contracted_mwh == Decimal("9999999.999")


# --- delivered intents -------------------------------------------------------

def a_delivered(**overrides) -> intents.DeliveredIntent:
    defaults = dict(
        settlement_date=DATE,
        settlement_period=PERIOD,
        bmu_id=BMU,
        gsp_group_id="_A",
        import_msid=1300035399160,
        delivered_mwh=Decimal("0.1000"),
    )
    return intents.DeliveredIntent(**{**defaults, **overrides})


def test_delivered_key_includes_the_msid():
    """One BM Unit may have several MSID Pairs, each with its own delivered
    volume for the same period."""
    first = a_delivered(import_msid=1300035399160)
    second = a_delivered(import_msid=1300035399161)
    assert first.key != second.key


def test_delivered_export_msid_is_optional():
    """BSCP602 1.1.1: an MSID Pair must contain an Import Metering System but
    need not contain an Export one."""
    assert a_delivered().export_msid is None


def test_msid_must_fit_thirteen_digits():
    with pytest.raises(ValueError, match="13 digits"):
        a_delivered(import_msid=12345678901234)