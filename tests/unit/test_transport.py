"""Transport and the encryption wrapper.

XSec-specific tests were removed when gpg replaced it -- see ADR-0011. What
remains tests the wrapper and the local transport, both of which are
implementation-independent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from consus_elexon_settlement.outbound.transport import (
    EncryptedTransport,
    LocalTransport,
    NullCipher,
    TransportError,
)


def test_encrypted_transport_passes_the_filename_through(tmp_path: Path):
    """The cipher receives the filename as well as the payload. GpgCipher does
    not use it, but the protocol carries it and the wrapper must not drop it."""
    seen: list[str] = []

    class Recording(NullCipher):
        def encrypt(self, filename: str, payload: bytes) -> bytes:
            seen.append(filename)
            return payload

    transport = EncryptedTransport(
        inner=LocalTransport(outbox=tmp_path / "out", inbox=tmp_path / "in"),
        cipher=Recording(),
    )
    transport.send("EN0000000001", b"data")
    assert seen == ["EN0000000001"]


def test_cipher_is_applied_on_the_way_out(tmp_path: Path):
    class Prefixing(NullCipher):
        def encrypt(self, filename: str, payload: bytes) -> bytes:
            return b"ENC:" + payload

    transport = EncryptedTransport(
        inner=LocalTransport(outbox=tmp_path / "out", inbox=tmp_path / "in"),
        cipher=Prefixing(),
    )
    transport.send("EN0000000001", b"payload")
    assert (tmp_path / "out" / "EN0000000001").read_bytes() == b"ENC:payload"


def test_cipher_is_applied_on_the_way_in(tmp_path: Path):
    class Stripping(NullCipher):
        def decrypt(self, filename: str, payload: bytes) -> bytes:
            return payload.removeprefix(b"ENC:")

    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / "EC0000000001").write_bytes(b"ENC:inbound")

    transport = EncryptedTransport(
        inner=LocalTransport(outbox=tmp_path / "out", inbox=inbox),
        cipher=Stripping(),
    )
    assert transport.collect() == [("EC0000000001", b"inbound")]


def test_null_cipher_is_a_pass_through(tmp_path: Path):
    """Used in development and in every test that touches transport, which
    keeps the encrypted path exercised even while encryption is off."""
    transport = EncryptedTransport(
        inner=LocalTransport(outbox=tmp_path / "out", inbox=tmp_path / "in"),
        cipher=NullCipher(),
    )
    transport.send("EN0000000001", b"payload")
    assert (tmp_path / "out" / "EN0000000001").read_bytes() == b"payload"


def test_local_transport_refuses_to_overwrite(tmp_path: Path):
    """A filename collision means a sequence or naming bug. Silently replacing
    the earlier file would hide it."""
    transport = LocalTransport(outbox=tmp_path / "out", inbox=tmp_path / "in")
    transport.send("EN0000000001", b"first")

    with pytest.raises(TransportError, match="already in outbox"):
        transport.send("EN0000000001", b"second")


def test_collect_removes_what_it_reads(tmp_path: Path):
    """Under the pull method, deleting from the source directory is how
    receipt is confirmed (IDD 2.3)."""
    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / "EC0000000001").write_bytes(b"inbound")

    transport = LocalTransport(outbox=tmp_path / "out", inbox=inbox)
    assert transport.collect() == [("EC0000000001", b"inbound")]
    assert list(inbox.iterdir()) == []


def test_collect_returns_files_in_order(tmp_path: Path):
    """Sorted, so a batch of feedback is processed in filename order. Not a
    correctness requirement -- the router correlates by content -- but it
    makes logs readable."""
    inbox = tmp_path / "in"
    inbox.mkdir()
    for name in ("EC0000000003", "EC0000000001", "EC0000000002"):
        (inbox / name).write_bytes(name.encode())

    transport = LocalTransport(outbox=tmp_path / "out", inbox=inbox)
    assert [n for n, _ in transport.collect()] == [
        "EC0000000001", "EC0000000002", "EC0000000003",
    ]