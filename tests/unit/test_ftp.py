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


@pytest.fixture
def ftp_root(tmp_path: Path) -> Path:
    root = tmp_path / "ftp"
    (root / "out").mkdir(parents=True)
    (root / "in").mkdir(parents=True)
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


@pytest.fixture
def transport(server) -> FtpTransport:
    host, port = server
    return FtpTransport(
        host=host,
        port=port,
        username=USER,
        password=PASSWORD,
        outbound_dir="/out",
        inbound_dir="/in",
        tls=False,  # the test server is plain; TLS is tested by configuration
        timeout_seconds=10,
    )


def test_send_puts_the_file_in_the_outbound_directory(
    transport: FtpTransport, ftp_root: Path
):
    transport.send("EN0000000001", b"AAA|E0041001|D|")
    assert (ftp_root / "out" / "EN0000000001").read_bytes() == b"AAA|E0041001|D|"


def test_send_leaves_no_partial_file(transport: FtpTransport, ftp_root: Path):
    """The upload uses a temporary name and renames into place, so a poller on
    the far end never sees a half-written file. Nothing with the suffix should
    survive a successful send."""
    transport.send("EN0000000001", b"data")
    remaining = [p.name for p in (ftp_root / "out").iterdir()]
    assert remaining == ["EN0000000001"]
    assert not any(n.endswith(PARTIAL_SUFFIX) for n in remaining)


def test_send_rejects_an_over_long_filename(transport: FtpTransport):
    """IDD 2.2.5 allows 14 characters. A longer name is rejected by the far
    end, and the sequence number is spent either way -- so catch it here."""
    with pytest.raises(TransportError, match="IDD 2.2.5"):
        transport.send("THIS_NAME_IS_FAR_TOO_LONG", b"data")


def test_collect_returns_and_removes(transport: FtpTransport, ftp_root: Path):
    (ftp_root / "in" / "EC0000000001").write_bytes(b"feedback")

    assert transport.collect() == [("EC0000000001", b"feedback")]
    # IDD 2.3: under the pull method, deletion is how receipt is confirmed.
    assert list((ftp_root / "in").iterdir()) == []


def test_collect_is_ordered(transport: FtpTransport, ftp_root: Path):
    for name in ("EC0000000003", "EC0000000001", "EC0000000002"):
        (ftp_root / "in" / name).write_bytes(name.encode())

    assert [n for n, _ in transport.collect()] == [
        "EC0000000001", "EC0000000002", "EC0000000003",
    ]


def test_collect_on_an_empty_directory(transport: FtpTransport):
    assert transport.collect() == []


def test_collect_skips_partial_files(transport: FtpTransport, ftp_root: Path):
    """A file still being uploaded by the far end carries the suffix. Reading
    it would produce a truncated file we would then reject, wrongly."""
    (ftp_root / "in" / f"EC0000000001{PARTIAL_SUFFIX}").write_bytes(b"half")
    (ftp_root / "in" / "EC0000000002").write_bytes(b"whole")

    assert transport.collect() == [("EC0000000002", b"whole")]
    # The partial is left alone, not deleted.
    assert (ftp_root / "in" / f"EC0000000001{PARTIAL_SUFFIX}").exists()


def test_collect_can_leave_files_in_place(server, ftp_root: Path):
    """Where Elexon push to us rather than us pulling, deleting would remove
    a file they are still tracking."""
    host, port = server
    transport = FtpTransport(
        host=host, port=port, username=USER, password=PASSWORD,
        outbound_dir="/out", inbound_dir="/in", tls=False,
        delete_after_collect=False, timeout_seconds=10,
    )
    (ftp_root / "in" / "EC0000000001").write_bytes(b"feedback")

    assert transport.collect() == [("EC0000000001", b"feedback")]
    assert (ftp_root / "in" / "EC0000000001").exists()


def test_bad_credentials_fail_clearly(server):
    host, port = server
    transport = FtpTransport(
        host=host, port=port, username=USER, password="wrong",
        outbound_dir="/out", inbound_dir="/in", tls=False, timeout_seconds=10,
    )
    with pytest.raises(TransportError, match="connecting to"):
        transport.collect()


def test_unreachable_host_fails_clearly():
    transport = FtpTransport(
        host="127.0.0.1", port=1, username=USER, password=PASSWORD,
        outbound_dir="/out", inbound_dir="/in", tls=False, timeout_seconds=2,
    )
    with pytest.raises(TransportError, match="connecting to"):
        transport.send("EN0000000001", b"data")


def test_binary_content_survives(transport: FtpTransport, ftp_root: Path):
    """Settlement files are ASCII, but the transport must not assume it: a
    transport that mangles bytes would corrupt a checksum silently."""
    payload = bytes(range(256))
    transport.send("EN0000000001", payload)
    assert (ftp_root / "out" / "EN0000000001").read_bytes() == payload