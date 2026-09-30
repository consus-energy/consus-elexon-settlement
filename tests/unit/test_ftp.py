"""FTP transport, against a real in-process server.

pyftpdlib runs an actual FTP server on a loopback port, so these tests
exercise the protocol rather than a mock of it. That matters here: the bugs
worth catching are protocol-level -- a partial file visible to a reader, a
deletion that happens before the read -- and a mock would happily pretend
either was fine.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from consus_elexon_settlement.outbound.ftp import PARTIAL_SUFFIX, FtpTransport
from consus_elexon_settlement.outbound.transport import TransportError

pyftpdlib = pytest.importorskip("pyftpdlib")

from pyftpdlib.authorizers import DummyAuthorizer  # noqa: E402
from pyftpdlib.handlers import FTPHandler  # noqa: E402
from pyftpdlib.servers import FTPServer  # noqa: E402

USER = "consusen"
PASSWORD = "test"


# PCIG 5.1: login lands in a directory named for the Participant ID, holding
# temp, inbox and outbox. The tests use that layout rather than an invented
# one, because the store-then-rename across directories is the behaviour under
# test.
ACCOUNT = "CONSUSEN"


@pytest.fixture
def ftp_root(tmp_path: Path) -> Path:
    root = tmp_path / "ftp"
    for name in ("temp", "inbox", "outbox"):
        (root / ACCOUNT / name).mkdir(parents=True)
    return root


@pytest.fixture
def server(ftp_root: Path):
    authorizer = DummyAuthorizer()
    authorizer.add_user(USER, PASSWORD, str(ftp_root), perm="elradfmw")

    handler = FTPHandler
    handler.authorizer = authorizer

    # Port 0 lets the OS choose, so tests do not collide.
    srv = FTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    yield srv.address

    srv.close_all()
    thread.join(timeout=2)


@pytest.fixture
def transport(server) -> FtpTransport:
    host, port = server
    return FtpTransport(
        host=host,
        port=port,
        username=USER,
        password=PASSWORD,
        staging_dir=f"/{ACCOUNT}/temp",
        outbound_dir=f"/{ACCOUNT}/inbox",
        inbound_dir=f"/{ACCOUNT}/outbox",
        timeout_seconds=10,
    )


def test_send_puts_the_file_in_the_inbox(transport: FtpTransport, ftp_root: Path):
    transport.send("EN0000000001", b"AAA|E0041001|D|")
    landed = ftp_root / ACCOUNT / "inbox" / "EN0000000001"
    assert landed.read_bytes() == b"AAA|E0041001|D|"


def test_send_stages_in_temp_first(transport: FtpTransport, ftp_root: Path):
    """PCIG 5.1.2 requires STOR into temp and a rename into inbox.

    The file must never appear in inbox under any name but its own: that
    directory is the one Elexon collect from, so a partial there can be taken
    mid-transfer, rejected as malformed, and spend the sequence number for a
    file we sent correctly.
    """
    transport.send("EN0000000001", b"data")

    assert [p.name for p in (ftp_root / ACCOUNT / "inbox").iterdir()] == [
        "EN0000000001"
    ]
    # temp is left clean on success.
    assert list((ftp_root / ACCOUNT / "temp").iterdir()) == []


def test_a_failed_rename_leaves_the_file_in_temp(server, ftp_root: Path):
    """Not in inbox, where it would be collected half-formed.

    The destination directory is made to not exist, which is the simplest way
    to make RNTO fail. What matters is where the bytes end up afterwards.
    """
    host, port = server
    transport = FtpTransport(
        host=host, port=port, username=USER, password=PASSWORD,
        staging_dir=f"/{ACCOUNT}/temp",
        outbound_dir=f"/{ACCOUNT}/nonexistent",
        inbound_dir=f"/{ACCOUNT}/outbox",
        timeout_seconds=10,
    )

    with pytest.raises(TransportError, match="renaming"):
        transport.send("EN0000000002", b"data")

    assert (ftp_root / ACCOUNT / "temp" / "EN0000000002").read_bytes() == b"data"


def test_send_rejects_an_over_long_filename(transport: FtpTransport):
    """IDD 2.2.5 allows 14 characters. A longer name is rejected by the far
    end, and the sequence number is spent either way -- so catch it here."""
    with pytest.raises(TransportError, match="IDD 2.2.5"):
        transport.send("THIS_NAME_IS_FAR_TOO_LONG", b"data")


def test_collect_returns_and_removes(transport: FtpTransport, ftp_root: Path):
    (ftp_root / ACCOUNT / "outbox" / "EC0000000001").write_bytes(b"feedback")

    assert transport.collect() == [("EC0000000001", b"feedback")]
    # PCIG 5.1.1: retrieve, then delete. The delete is our confirmation.
    assert list((ftp_root / ACCOUNT / "outbox").iterdir()) == []


def test_plain_ftp_is_the_default():
    """PCIG 5.1 and the firewall tables: standard FTP on port 21, passive.

    Defaulting to TLS would mean a connection that cannot be made. The payload
    is gpg-encrypted before it reaches this layer (ADR-0011).
    """
    assert FtpTransport.__dataclass_fields__["tls"].default is False
    assert FtpTransport.__dataclass_fields__["passive"].default is True
    assert FtpTransport.__dataclass_fields__["port"].default == 21


def test_collect_is_ordered(transport: FtpTransport, ftp_root: Path):
    for name in ("EC0000000003", "EC0000000001", "EC0000000002"):
        (ftp_root / ACCOUNT / "outbox" / name).write_bytes(name.encode())

    assert [n for n, _ in transport.collect()] == [
        "EC0000000001", "EC0000000002", "EC0000000003",
    ]


def test_collect_on_an_empty_directory(transport: FtpTransport):
    assert transport.collect() == []


def test_collect_skips_partial_files(transport: FtpTransport, ftp_root: Path):
    """A file still being uploaded by the far end carries the suffix. Reading
    it would produce a truncated file we would then reject, wrongly."""
    outbox = ftp_root / ACCOUNT / "outbox"
    (outbox / f"EC0000000001{PARTIAL_SUFFIX}").write_bytes(b"half")
    (outbox / "EC0000000002").write_bytes(b"whole")

    assert transport.collect() == [("EC0000000002", b"whole")]
    # The partial is left alone, not deleted.
    assert (outbox / f"EC0000000001{PARTIAL_SUFFIX}").exists()


def test_collect_can_leave_files_in_place(server, ftp_root: Path):
    """Where Elexon push to us rather than us pulling, deleting would remove
    a file they are still tracking."""
    host, port = server
    transport = FtpTransport(
        host=host, port=port, username=USER, password=PASSWORD,
        staging_dir=f"/{ACCOUNT}/temp",
        outbound_dir=f"/{ACCOUNT}/inbox",
        inbound_dir=f"/{ACCOUNT}/outbox",
        delete_after_collect=False, timeout_seconds=10,
    )
    (ftp_root / ACCOUNT / "outbox" / "EC0000000001").write_bytes(b"feedback")

    assert transport.collect() == [("EC0000000001", b"feedback")]
    assert (ftp_root / ACCOUNT / "outbox" / "EC0000000001").exists()


def test_bad_credentials_fail_clearly(server):
    host, port = server
    transport = FtpTransport(
        host=host, port=port, username=USER, password="wrong",
        staging_dir=f"/{ACCOUNT}/temp",
        outbound_dir=f"/{ACCOUNT}/inbox",
        inbound_dir=f"/{ACCOUNT}/outbox",
        timeout_seconds=10,
    )
    with pytest.raises(TransportError, match="connecting to"):
        transport.collect()


def test_unreachable_host_fails_clearly():
    transport = FtpTransport(
        host="127.0.0.1", port=1, username=USER, password=PASSWORD,
        staging_dir=f"/{ACCOUNT}/temp",
        outbound_dir=f"/{ACCOUNT}/inbox",
        inbound_dir=f"/{ACCOUNT}/outbox",
        timeout_seconds=2,
    )
    with pytest.raises(TransportError, match="connecting to"):
        transport.send("EN0000000001", b"data")


def test_binary_content_survives(transport: FtpTransport, ftp_root: Path):
    """Settlement files are ASCII, but the transport must not assume it: a
    transport that mangles bytes would corrupt a checksum silently."""
    payload = bytes(range(256))
    transport.send("EN0000000001", payload)
    landed = ftp_root / ACCOUNT / "inbox" / "EN0000000001"
    assert landed.read_bytes() == payload