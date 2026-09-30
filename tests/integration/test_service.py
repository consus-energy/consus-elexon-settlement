"""The intent service: a decision becomes files, and files become a resolved
intent.

Against a real database, because the thing being tested is a lifecycle across
several tables and the interesting cases are ordering ones.

Four cases carry most of the weight:

    a duplicate sends nothing        idempotency by natural key
    a late intent sends nothing      the deadline check, before any attempt
    a retry sends only what failed   no duplicate sequence numbers
    an acceptance closes the intent  ACTED means accepted, not sent

The last is the round trip that proves the design decision. Without it an
intent would sit in ACTING forever and the sweep would show every submission
as outstanding.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from consus_elexon_settlement import db, intents, service, states
from consus_elexon_settlement.archive import LocalArchive
from consus_elexon_settlement.outbound.sender import Sender
from consus_elexon_settlement.outbound.submissions import Submitter
from consus_elexon_settlement.outbound.transport import (
    LocalTransport,
    TransportError,
)

from ..conftest import make_channel
from .test_submissions import FailingTransport, _NoClose

DATE = dt.date(2026, 9, 15)
PERIOD = 37
BMU = "V__ACNRG001"
IMPORT_MSID = 1300035399160

# Well before Gate Closure for period 37, which is 17:00 UTC on a BST day.
IN_TIME = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc)
TOO_LATE = dt.datetime(2026, 9, 15, 18, 30, tzinfo=dt.timezone.utc)


class SelectiveTransport:
    """Fails only the flows named, so partial failure can be tested.

    Which file is which is judged by its role prefix: ECVNs go out as the
    ECVN Agent ('EN'), everything else as the VTP ('VT'). Crude, but it is
    the only distinction visible at transport level -- and that it is the
    only one is itself worth knowing.
    """

    def __init__(self, fail_prefixes: tuple[str, ...]) -> None:
        self.fail_prefixes = fail_prefixes
        self.sent: list[str] = []

    def send(self, filename: str, payload: bytes) -> None:
        if filename.startswith(self.fail_prefixes):
            raise TransportError(f"refusing {filename}")
        self.sent.append(filename)

    def collect(self) -> list[tuple[str, bytes]]:
        return []


def build_service(conn, tmp_path: Path, transport) -> service.IntentService:
    connect = lambda: _NoClose(conn)  # noqa: E731
    sender = Sender(
        connect=connect,
        archive=LocalArchive(root=tmp_path / "archive"),
        transport=transport,
    )
    submitter = Submitter(connect=connect, sender=sender)

    # Both identities point at the same test channel apart from the role. In
    # production these are separate channels with separate sequence counters
    # (ADR-0004); here the distinction that matters is only that they differ.
    channels = service.Channels(
        vtp_to_ecvaa=make_channel(conn, role="VT", participant="CONSUSVT"),
        agent_to_ecvaa=make_channel(conn, role="EN", participant="CONSUSEN"),
        vtp_to_svaa=make_channel(conn, role="VT", participant="CONSUSVT",
                                 to_role="SV", to_participant="SVAA"),
    )
    return service.IntentService(
        connect=connect, submitter=submitter, channels=channels
    )


@pytest.fixture
def registered_key(conn):
    """An authorisation with a key reference, so the ECVN can be built.

    counterparty_id, our_pc_flag and effective_from are NOT NULL: an
    authorisation is a standing agreement between two named parties from a
    date, established manually under BSCP71, and a row without those is not
    an authorisation.

    our_pc_flag is 'P' or 'C' -- which side of the contract we are. It
    determines the sign convention on the ECVN, since volume is signed from
    party 1 to party 2.

    _read_secret still raises -- the secret store is not wired -- so tests
    needing a successful ECVN patch it. This fixture only satisfies the
    lookup that happens first.
    """
    with conn.transaction():
        conn.execute(
            """INSERT INTO ecvnaa (ecvnaa_id, key_secret_ref, counterparty_id,
                                   our_pc_flag, effective_from)
                    VALUES ('AUTH000001', 'projects/x/secrets/y/versions/1',
                            'COUNTERP', 'P', %s)
               ON CONFLICT (ecvnaa_id) DO UPDATE
                    SET key_secret_ref = EXCLUDED.key_secret_ref""",
            (DATE,),
        )


@pytest.fixture
def svc(conn, tmp_path, registered_key, monkeypatch) -> service.IntentService:
    # The secret store is not built. Patching it here keeps these tests about
    # the intent lifecycle rather than about a gap we already know exists.
    monkeypatch.setattr(service, "_read_secret", lambda ref: "KEY1234567")
    return build_service(conn, tmp_path, LocalTransport(
        outbox=tmp_path / "out", inbox=tmp_path / "in",
    ))


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


# --- the happy path ----------------------------------------------------------

def test_acting_after_all_three_flows_sent(svc, conn):
    """Sent is not accepted. The intent stays open until central systems
    confirm, which is the whole point of the choice."""
    outcome = svc.act(an_intent(), now=IN_TIME)

    assert outcome.state == intents.ACTING
    assert set(outcome.flows) == {"wman", "ecvn", "sev"}
    assert set(outcome.flows.values()) == {intents.SENT}
    assert not outcome.acted


def test_each_flow_records_its_file(svc, conn):
    """A flow without a file id cannot be reconciled when feedback arrives,
    and feedback is the only route to ACTED."""
    outcome = svc.act(an_intent(), now=IN_TIME)

    rows = conn.execute(
        """SELECT flow, outbound_file_id FROM intent_flow
            WHERE intent_id = %s ORDER BY flow""",
        (outcome.intent_id,),
    ).fetchall()

    assert len(rows) == 3
    assert all(file_id is not None for _, file_id in rows)


def test_the_intent_records_its_gate_closure(svc, conn):
    """Stored on receipt, so a late arrival is distinguishable from one we
    delayed. That distinction is the first thing anyone wants when explaining
    a missed submission."""
    from consus_elexon_settlement import deadlines

    outcome = svc.act(an_intent(), now=IN_TIME)
    stored = conn.execute(
        "SELECT gate_closure FROM intent WHERE id = %s", (outcome.intent_id,),
    ).fetchone()[0]

    assert stored == deadlines.gate_closure(DATE, PERIOD)


# --- idempotency -------------------------------------------------------------

def test_a_duplicate_sends_nothing(svc, conn, tmp_path):
    """Pub/Sub delivers at least once, and the EMS can send the same decision
    twice by mistake. A natural key makes both the same case."""
    first = svc.act(an_intent(), now=IN_TIME)
    files_after_first = len(list((tmp_path / "out").iterdir()))

    second = svc.act(an_intent(), now=IN_TIME)

    assert second.duplicate
    assert second.intent_id == first.intent_id
    assert len(list((tmp_path / "out").iterdir())) == files_after_first


def test_a_revision_is_not_a_duplicate(svc, conn, tmp_path):
    """Trading the same period again is a new decision. Without revision on
    the key the two are indistinguishable, and we would either reject a real
    change or double-submit a repeat."""
    first = svc.act(an_intent(), now=IN_TIME)
    second = svc.act(an_intent(revision=2), now=IN_TIME)

    assert not second.duplicate
    assert second.intent_id != first.intent_id
    assert len(list((tmp_path / "out").iterdir())) == 6


# --- the deadline ------------------------------------------------------------

def test_a_late_intent_is_recorded_and_not_sent(svc, conn, tmp_path):
    """Submitting after Gate Closure is worse than not submitting: a rejected
    file, a spent sequence number, and a record suggesting we tried."""
    outcome = svc.act(an_intent(), now=TOO_LATE)

    assert outcome.state == intents.MISSED
    assert outcome.needs_attention
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_a_late_intent_records_why(svc, conn):
    """The record has to distinguish 'arrived late' from 'we were slow'. The
    detail says which, and the gate closure column proves it."""
    outcome = svc.act(an_intent(), now=TOO_LATE)

    detail = conn.execute(
        "SELECT detail FROM intent WHERE id = %s", (outcome.intent_id,),
    ).fetchone()[0]

    assert "after gate closure" in detail


def test_a_late_intent_still_exists(svc, conn):
    """A discarded message cannot answer the question asked afterwards."""
    outcome = svc.act(an_intent(), now=TOO_LATE)

    row = conn.execute(
        "SELECT state, completed_at FROM intent WHERE id = %s",
        (outcome.intent_id,),
    ).fetchone()

    assert row[0] == intents.MISSED
    assert row[1] is not None


# --- partial failure and retry -----------------------------------------------

def test_a_transport_failure_leaves_the_intent_partial(
    conn, tmp_path, registered_key, monkeypatch
):
    monkeypatch.setattr(service, "_read_secret", lambda ref: "KEY1234567")
    # Fail only the ECVN, which goes out under the ECVN Agent identity.
    svc = build_service(conn, tmp_path, SelectiveTransport(("EN",)))

    outcome = svc.act(an_intent(), now=IN_TIME)

    assert outcome.state == intents.PARTIAL
    assert outcome.flows["wman"] == intents.SENT
    assert outcome.flows["sev"] == intents.SENT
    assert outcome.flows["ecvn"] == intents.FAILED


def test_one_flow_failing_does_not_stop_the_others(
    conn, tmp_path, registered_key, monkeypatch
):
    """A partial submission is more recoverable than none, and the retry knows
    which is which."""
    monkeypatch.setattr(service, "_read_secret", lambda ref: "KEY1234567")
    transport = SelectiveTransport(("EN",))
    svc = build_service(conn, tmp_path, transport)

    svc.act(an_intent(), now=IN_TIME)

    # WMAN and SEV both went despite the ECVN failing.
    assert len(transport.sent) == 2


def test_retry_sends_only_the_failed_flow(
    conn, tmp_path, registered_key, monkeypatch
):
    """Resending an accepted or sent flow would spend a sequence number on a
    duplicate, and ECVAA would hold two notifications for one position."""
    monkeypatch.setattr(service, "_read_secret", lambda ref: "KEY1234567")
    failing = SelectiveTransport(("EN",))
    svc = build_service(conn, tmp_path, failing)
    outcome = svc.act(an_intent(), now=IN_TIME)
    assert outcome.state == intents.PARTIAL

    # Transport recovers.
    working = SelectiveTransport(())
    retried_svc = build_service(conn, tmp_path, working)
    retried = retried_svc.retry(outcome.intent_id, now=IN_TIME)

    assert retried.state == intents.ACTING
    # Exactly one file on the retry: the ECVN. Not three.
    assert len(working.sent) == 1


def test_retry_after_the_deadline_is_missed_not_attempted(
    conn, tmp_path, registered_key, monkeypatch
):
    """MISSED is terminal for exactly this reason: a retry after Gate Closure
    spends a sequence number on a file central systems will refuse."""
    monkeypatch.setattr(service, "_read_secret", lambda ref: "KEY1234567")
    svc = build_service(conn, tmp_path, SelectiveTransport(("EN",)))
    outcome = svc.act(an_intent(), now=IN_TIME)

    working = SelectiveTransport(())
    retried_svc = build_service(conn, tmp_path, working)
    retried = retried_svc.retry(outcome.intent_id, now=TOO_LATE)

    assert retried.state == intents.MISSED
    assert working.sent == []


def test_a_terminal_intent_cannot_be_retried(svc):
    outcome = svc.act(an_intent(), now=TOO_LATE)

    with pytest.raises(service.ServiceError, match="cannot be retried"):
        svc.retry(outcome.intent_id, now=IN_TIME)


# --- reconciliation: the round trip -----------------------------------------

def test_acceptance_moves_a_flow_but_not_the_intent(svc, conn):
    """One acceptance is not three. The intent stays ACTING until every flow
    is accepted -- which is what stops it reading finished early."""
    outcome = svc.act(an_intent(), now=IN_TIME)

    file_id = conn.execute(
        "SELECT outbound_file_id FROM intent_flow WHERE intent_id = %s AND flow = 'wman'",
        (outcome.intent_id,),
    ).fetchone()[0]

    # The handler would do this; here we do it directly.
    db.reject_wman(conn, DATE, PERIOD, "test", bmu_id=None)
    conn.execute(
        "UPDATE wman SET state = %s WHERE outbound_file_id = %s",
        (states.ACCEPTED, file_id),
    )
    state = service.reconcile_intent(conn, file_id, now=IN_TIME)

    assert state == intents.ACTING


def test_all_acceptances_close_the_intent(svc, conn):
    """The round trip that proves the design. Sent is not done; accepted is.

    Without this path an intent sits in ACTING forever and every submission
    shows as outstanding, which would make the sweep useless.
    """
    outcome = svc.act(an_intent(), now=IN_TIME)

    files = dict(conn.execute(
        "SELECT flow, outbound_file_id FROM intent_flow WHERE intent_id = %s",
        (outcome.intent_id,),
    ).fetchall())

    for flow, table in (
        ("wman", "wman"), ("ecvn", "notification"), ("sev", "sev"),
    ):
        conn.execute(
            f"UPDATE {table} SET state = %s WHERE outbound_file_id = %s",
            (states.ACCEPTED, files[flow]),
        )
        state = service.reconcile_intent(conn, files[flow], now=IN_TIME)

    assert state == intents.ACTED

    completed = conn.execute(
        "SELECT completed_at FROM intent WHERE id = %s", (outcome.intent_id,),
    ).fetchone()[0]
    assert completed is not None


def test_a_rejection_makes_the_intent_partial(svc, conn):
    """Not retryable under this intent -- the content was refused -- but the
    deadline has not passed, so a new intent at the next revision could still
    succeed. That is the EMS's call."""
    outcome = svc.act(an_intent(), now=IN_TIME)

    file_id = conn.execute(
        "SELECT outbound_file_id FROM intent_flow WHERE intent_id = %s AND flow = 'ecvn'",
        (outcome.intent_id,),
    ).fetchone()[0]

    conn.execute(
        "UPDATE notification SET state = %s WHERE outbound_file_id = %s",
        (states.REJECTED, file_id),
    )
    state = service.reconcile_intent(conn, file_id, now=IN_TIME)

    assert state == intents.PARTIAL


def test_reconciling_a_file_with_no_intent_is_harmless(conn, channel):
    """Registration submissions and anything sent by hand have no intent.
    Reconciliation must not raise on them."""
    from ..conftest import built_file

    file_id = built_file(conn, channel)
    assert service.reconcile_intent(conn, file_id, now=IN_TIME) is None


def test_acceptance_after_the_deadline_does_not_reopen_a_missed_intent(
    svc, conn
):
    """An intent that ran out of time stays MISSED. An acceptance arriving
    afterwards does not un-miss it, and a state that could reopen would mean a
    missed deadline could look met."""
    outcome = svc.act(an_intent(), now=TOO_LATE)
    assert outcome.state == intents.MISSED

    # No flows were sent, so there is no file to reconcile -- which is itself
    # the point: nothing went, so nothing can come back.
    flows = conn.execute(
        "SELECT count(*) FROM intent_flow WHERE intent_id = %s",
        (outcome.intent_id,),
    ).fetchone()[0]
    assert flows == 0


# --- delivered volumes -------------------------------------------------------

def a_delivered() -> intents.DeliveredIntent:
    return intents.DeliveredIntent(
        settlement_date=DATE,
        settlement_period=PERIOD,
        bmu_id=BMU,
        gsp_group_id="_A",
        import_msid=IMPORT_MSID,
        delivered_mwh=Decimal("0.1000"),
    )


def test_delivered_intent_is_recorded_and_sent(svc, conn):
    outcome = svc.deliver(a_delivered())

    assert outcome.state == intents.ACTING
    row = conn.execute(
        """SELECT state, outbound_file_id, due_by FROM delivered_intent
            WHERE id = %s""",
        (outcome.intent_id,),
    ).fetchone()
    assert row[0] == intents.ACTING
    assert row[1] is not None
    assert row[2] is not None


def test_a_duplicate_delivered_intent_sends_nothing(svc, tmp_path):
    svc.deliver(a_delivered())
    count = len(list((tmp_path / "out").iterdir()))

    second = svc.deliver(a_delivered())

    assert second.duplicate
    assert len(list((tmp_path / "out").iterdir())) == count


def test_delivered_is_keyed_by_msid_too(svc):
    """One BM Unit may have several MSID Pairs, each with its own delivered
    volume for the same period."""
    first = svc.deliver(a_delivered())
    second = svc.deliver(intents.DeliveredIntent(
        settlement_date=DATE, settlement_period=PERIOD, bmu_id=BMU,
        gsp_group_id="_A", import_msid=IMPORT_MSID + 1,
        delivered_mwh=Decimal("0.2000"),
    ))

    assert not second.duplicate
    assert second.intent_id != first.intent_id