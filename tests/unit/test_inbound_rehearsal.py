"""Rehearsal: every inbound file type through the router.

Builds a syntactically valid file for each flow Elexon may send us, straight
from the generated spec, and pushes it through the real router. Asserts the
file parses, reaches a handler where one is registered, and comes back with an
ADT carrying response code 0.

This is what can be rehearsed without a connection, credentials or participant
ids. It catches the failures that are expensive to find on the day:

  * a flow version registered that the spec does not define
  * a file type whose handler was never wired, so it is acknowledged and
    silently dropped
  * an identity mismatch, where a file addressed to us as a party rather than
    an agent is refused as somebody else's

It does NOT prove we agree with Elexon about content. The bodies here are
generated from the spec's own field types, so a field we have misunderstood
will still round-trip happily. Only a real file from Elexon settles that.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from consus_elexon_settlement import app
from consus_elexon_settlement.app import (
    REPORT_FILE_TYPES,
    Config,
    Handlers,
    Identity,
    build_router,
)
from consus_elexon_settlement.idd import adt
from consus_elexon_settlement.idd.file import Header, Node, build
from consus_elexon_settlement.idd.model import Cardinality, Flow, Record

VTP = Identity("VT", "CNRGVTP1")
ECVNA = Identity("EN", "CONSUSEN")
CONFIG = Config(vtp=VTP, ecvna=ECVNA, environment="TST1")

RECEIVED = dt.datetime(2026, 10, 16, 9, 30, tzinfo=dt.timezone.utc)

# Who sends what, and to which of our identities. From the IDD Flow Roles tab:
# ECVAA feedback reaches us as 'BP' and 'EN', the WMAN exception as 'VT', the
# registration and settlement reports as 'BP' and 'PA'.
SENDERS = {
    "E": ("EC", "ECVAA"),
    "P": ("G", "UKDC"),
    "R": ("CR", "CRA"),
    "S": ("SA", "SAA"),
}

ADDRESSED_TO = {
    "E0091001": ("EN", ECVNA.participant),
    "E0281001": ("EN", ECVNA.participant),
    "E0071001": ("EN", ECVNA.participant),
    "E0071002": ("EN", ECVNA.participant),
    "E0071003": ("EN", ECVNA.participant),
    "E0521001": ("VT", VTP.participant),
    "E0131001": ("EN", ECVNA.participant),
    "E0131002": ("EN", ECVNA.participant),
    "E0132001": ("BP", VTP.participant),
    "E0132002": ("BP", VTP.participant),
    "E0141003": ("EN", ECVNA.participant),
    "E0141004": ("EN", ECVNA.participant),
    "E0221002": ("BP", VTP.participant),
    "R0143001": ("PA", ECVNA.participant),
}


def addressed_to(file_type: str) -> tuple[str, str]:
    """Our role and id for a file type, defaulting to the party identity.

    IDD 2.2.1 field 7: a file is addressed to the role code for the capacity
    it concerns, and 'in all other cases the To Role Code will be BP'.
    """
    if file_type in ADDRESSED_TO:
        return ADDRESSED_TO[file_type]
    if file_type.startswith(("P0", "R0", "S0")):
        return ("BP", VTP.participant)
    return ("VT", VTP.participant)


# --- building a body from a spec --------------------------------------------

def sample_value(data_type, item_id: str):
    """A value the spec's own rules accept, for the field's declared type."""
    kind = data_type.kind

    if kind in ("text", "char"):
        length = 1 if kind == "char" else (data_type.length or 8)
        # 'A' repeated rather than anything meaningful: this harness proves
        # structure, not content.
        return "A" * min(length, 10)
    if kind == "integer":
        # Settlement period fields are integer(2) and must be 1 to 50. 1 is
        # valid for every integer field in these flows.
        return 1
    if kind == "decimal":
        return Decimal("1.5")
    if kind == "boolean":
        return True
    if kind == "date":
        return dt.date(2026, 10, 16)
    if kind == "datetime":
        return dt.datetime(2026, 10, 16, 9, 30, tzinfo=dt.timezone.utc)
    if kind in ("time", "timestamp"):
        return dt.time(9, 30)
    raise AssertionError(f"no sample value for {data_type} ({item_id})")


def occurrences(cardinality: Cardinality) -> int:
    """How many of a record to emit.

    The minimum, except for the half-hourly groups. IDD 2.2.4 writes those as
    46-50, meaning 46, 48 or 50 and never 47 or 49, so emitting the minimum
    would build a short-day file every time. 48 is the ordinary day and the
    one a test slot will use; the clock-change counts are covered separately
    in test_file.py.
    """
    if cardinality.clock_change:
        return 48
    return cardinality.minimum


def build_node(record: Record) -> Node:
    values = {
        field.item_id: sample_value(field.data_type, field.item_id)
        for field in record.fields
        if field.presence == "M"
    }
    children: list[Node] = []
    for child in record.children:
        # Mandatory children only. Optional groups are left out deliberately:
        # a file Elexon sends may omit them, and a parser that needs them
        # would fail on the day.
        children.extend(
            build_node(child) for _ in range(occurrences(child.cardinality))
        )
    return Node(record_type=record.record_type, values=values, children=children)


def build_body(flow: Flow) -> list[Node]:
    return [
        build_node(record)
        for record in flow.records
        for _ in range(occurrences(record.cardinality))
    ]


def inbound_file(flow: Flow, sequence: int = 1) -> bytes:
    from_role, from_participant = SENDERS[flow.file_id[0]]
    to_role, to_participant = addressed_to(flow.file_type)

    header = Header(
        file_type=flow.file_type,
        message_role="D",
        creation_time=RECEIVED,
        from_role_code=from_role,
        from_participant_id=from_participant,
        to_role_code=to_role,
        to_participant_id=to_participant,
        sequence_number=sequence,
        test_flag=CONFIG.test_flag or None,
    )
    return build(flow, header, build_body(flow))


# --- the rehearsal ----------------------------------------------------------

@pytest.fixture
def recorded():
    """A router with every handler replaced by one that records the call."""
    seen: list[str] = []

    def record(header, body, filename):
        seen.append(header.file_type)

    handlers = Handlers(
        ecvn_rejection=record,
        ecvn_acceptance=record,
        wman_exception=record,
        sev_acceptance=record,
        sev_rejection=record,
        sev_warning=record,
        delivered_confirmation=record,
        delivered_rejection=record,
        ecvnaa_confirmation=record,
        report=record,
    )
    return build_router(CONFIG, handlers), seen


HANDLED_FILE_TYPES = (
    "E0091001",   # ECVN rejection
    "E0281001",   # ECVN acceptance
    "E0521001",   # WMAN exception
    "E0071001",   # ECVNAA feedback, carries the key
    "P0329001",   # SEV rejection
    "P0330001",   # SEV acceptance
    "P0331001",   # SEV warning
    "P0283002",   # delivered volume rejection
    "P0284001",   # delivered volume confirmation
)

ALL_INBOUND = HANDLED_FILE_TYPES + REPORT_FILE_TYPES


@pytest.mark.parametrize("file_type", ALL_INBOUND)
def test_every_inbound_file_type_is_known_to_the_router(recorded, file_type):
    router, _ = recorded
    assert router.flow(file_type) is not None, (
        f"{file_type} is registered but no loaded spec defines it. It would "
        f"be acknowledged with response code 2 and never processed."
    )


@pytest.mark.parametrize("file_type", ALL_INBOUND)
def test_every_inbound_file_type_parses_and_is_acknowledged(recorded, file_type):
    router, _ = recorded
    flow = router.flow(file_type)

    received = router.receive(inbound_file(flow), f"XX{file_type[:12]}", RECEIVED)

    assert received.error is None, f"{file_type} failed to parse: {received.error}"
    assert received.response_code == adt.OK
    assert received.response.startswith(b"AAA|")
    assert b"|R|" in received.response.split(b"\n")[0]


@pytest.mark.parametrize("file_type", HANDLED_FILE_TYPES)
def test_handled_file_types_reach_their_handler(recorded, file_type):
    """A registered flow with no handler is acknowledged and dropped, which
    looks exactly like working."""
    router, seen = recorded
    flow = router.flow(file_type)

    router.receive(inbound_file(flow), f"XX{file_type[:12]}", RECEIVED)

    assert seen == [file_type]


def test_a_file_for_another_participant_is_refused(recorded):
    """Acting on someone else's settlement file is worse than refusing one of
    our own."""
    router, seen = recorded
    flow = router.flow("E0281001")

    header = Header(
        file_type="E0281001",
        message_role="D",
        creation_time=RECEIVED,
        from_role_code="EC",
        from_participant_id="ECVAA",
        to_role_code="EN",
        to_participant_id="SOMEBODY",
        sequence_number=1,
        test_flag="TST1",
    )
    payload = build(flow, header, build_body(flow))

    received = router.receive(payload, "XXNOTOURS0001", RECEIVED)

    assert received.response_code == adt.UNEXPECTED_FILE_TYPE
    assert seen == []


def test_the_party_identity_receives_reports(recorded):
    """Registration and settlement reports arrive as 'BP', not 'VT'. Holding
    only the flow identities would refuse every one of them."""
    router, seen = recorded
    flow = router.flow("R0141001")

    received = router.receive(inbound_file(flow), "XXREGREPORT01", RECEIVED)

    assert received.response_code == adt.OK
    assert seen == ["R0141001"]