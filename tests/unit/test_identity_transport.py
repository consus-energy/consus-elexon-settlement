"""One FTP account per identity.

PCIG 5.1 issues an account per registered Participant ID, and the Participant
ID is the username. We hold two: the Party Id we send WMAN and the SVAA flows
under, and the ECVNA Id we send ECVNs under. That makes two accounts with two
directory trees, and the consequences run both ways:

  * an outbound file must leave through its own identity's account, or it
    lands in the wrong party's inbox
  * both outboxes must be drained, or a rejection sits unread in one of them
    and looks exactly like a quiet day

These tests cover the routing, not the protocol -- test_ftp.py does that
against a real server.
"""

from __future__ import annotations

import pytest

from consus_elexon_settlement import db
from consus_elexon_settlement.inbound.receiver import Receiver
from consus_elexon_settlement.outbound.sender import Sender


class RecordingTransport:
    """Remembers what it was asked to send, and hands out what it holds."""

    def __init__(self, name: str, waiting: list[tuple[str, bytes]] | None = None):
        self.name = name
        self.sent: list[str] = []
        self.waiting = list(waiting or [])

    def send(self, filename: str, payload: bytes) -> None:
        self.sent.append(filename)

    def collect(self) -> list[tuple[str, bytes]]:
        waiting, self.waiting = self.waiting, []
        return waiting


class BrokenTransport(RecordingTransport):
    def collect(self):
        raise OSError("account unreachable")


def channel(participant: str, role: str) -> db.Channel:
    return db.Channel(
        id=1,
        from_role_code=role,
        from_participant_id=participant,
        to_role_code="EC",
        to_participant_id="ECVAA",
        test_flag="TST1",
    )


# --- outbound ---------------------------------------------------------------

def test_sender_picks_the_account_for_the_sending_identity():
    accounts = {
        "CONSUSVT": RecordingTransport("vtp"),
        "CONSUSEN": RecordingTransport("ecvna"),
    }
    sender = Sender(connect=None, archive=None, transport=accounts.__getitem__)

    assert sender._transport_for(channel("CONSUSEN", "EN")).name == "ecvna"
    assert sender._transport_for(channel("CONSUSVT", "VT")).name == "vtp"


def test_sender_keys_on_participant_not_role():
    """One Participant Id may send under more than one role code, and all of
    it goes through the one account. Keying on the role would ask for an
    account that does not exist."""
    calls: list[str] = []

    def factory(participant_id: str) -> RecordingTransport:
        calls.append(participant_id)
        return RecordingTransport(participant_id)

    sender = Sender(connect=None, archive=None, transport=factory)
    sender._transport_for(channel("CONSUSVT", "VT"))
    sender._transport_for(channel("CONSUSVT", "BP"))

    assert calls == ["CONSUSVT", "CONSUSVT"]


def test_a_single_transport_still_serves_every_channel():
    """Local and in-memory transports stay one object, as tests and
    development use them."""
    only = RecordingTransport("local")
    sender = Sender(connect=None, archive=None, transport=only)

    assert sender._transport_for(channel("CONSUSEN", "EN")) is only
    assert sender._transport_for(channel("CONSUSVT", "VT")) is only


# --- inbound ----------------------------------------------------------------

@pytest.fixture
def receiver_factory():
    def make(transports):
        return Receiver(
            connect=None,
            archive=None,
            router=None,
            transport=transports,
            response_name=lambda name: f"R{name}"[:14],
        )
    return make


def test_receiver_drains_every_account(receiver_factory, monkeypatch):
    vtp = RecordingTransport("vtp", [("EC0000000001", b"a")])
    ecvna = RecordingTransport("ecvna", [("EC0000000002", b"b")])
    receiver = receiver_factory([vtp, ecvna])

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        receiver, "receive_one",
        lambda filename, payload, transport=None: seen.append(
            (filename, transport.name)
        ),
    )
    receiver.collect()

    # Both accounts read, and each file carries the account it came from so
    # the acknowledgement can go back the same way.
    assert seen == [("EC0000000001", "vtp"), ("EC0000000002", "ecvna")]


def test_one_unreachable_account_does_not_block_the_other(
    receiver_factory, monkeypatch
):
    """A rejection in the reachable account still needs reading before Gate
    Closure. Failing the whole run would lose it."""
    broken = BrokenTransport("vtp")
    working = RecordingTransport("ecvna", [("EC0000000002", b"b")])
    receiver = receiver_factory([broken, working])

    seen: list[str] = []
    monkeypatch.setattr(
        receiver, "receive_one",
        lambda filename, payload, transport=None: seen.append(filename),
    )
    receiver.collect()

    assert seen == ["EC0000000002"]


def test_a_single_transport_is_accepted(receiver_factory, monkeypatch):
    only = RecordingTransport("local", [("EC0000000001", b"a")])
    receiver = receiver_factory(only)

    seen: list[str] = []
    monkeypatch.setattr(
        receiver, "receive_one",
        lambda filename, payload, transport=None: seen.append(filename),
    )
    receiver.collect()

    assert seen == ["EC0000000001"]