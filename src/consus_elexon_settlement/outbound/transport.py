"""Transport, and the encryption layer that wraps it.

Two protocols, deliberately separate:

    Transport  -- moves files to and from the central systems
    Cipher     -- encrypts and decrypts them

Keeping them apart means the sender never knows whether encryption is on.
EncryptedTransport wraps any Transport, so a test uses NullCipher and
production uses GpgCipher without the sender or the tests changing.

Cipher takes a filename as well as the payload. That is a historical shape:
the first implementation drove XSec, which operates on files in watched
directories rather than on byte streams. GpgCipher does not need it, but the
argument is kept because a protocol that changes shape per implementation is
not a protocol, and a future transport may well need it again.

Outbound is push-only: participant systems push files to the central systems
and use the FTP success code as confirmation of sending (IDD 2.3). Inbound
offers push or pull; under pull, deleting the file from the source directory
is how receipt is confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class TransportError(RuntimeError):
    pass


class CipherError(RuntimeError):
    """Encryption or decryption failed.

    Distinct from TransportError: a cipher failure means the file never
    reached the wire, so nothing was sent and the sequence number is still
    ours. A transport failure means we do not know.
    """


class Cipher(Protocol):
    """Encrypts and decrypts whole files."""

    def encrypt(self, filename: str, payload: bytes) -> bytes: ...

    def decrypt(self, filename: str, payload: bytes) -> bytes: ...


class Transport(Protocol):
    def send(self, filename: str, payload: bytes) -> None:
        """Push one file. Returning normally means sent, not received."""

    def collect(self) -> list[tuple[str, bytes]]:
        """Retrieve waiting files as (filename, payload).

        Under the pull method, a file is deleted from the source directory
        once collected, which is how receipt is confirmed (IDD 2.3). An
        implementation must not delete before the payload is safely held.
        """


class NullCipher:
    """No encryption. For tests, and for local development.

    Explicit rather than an Optional[Cipher]: a None cipher reads as an
    oversight, whereas NullCipher reads as a decision. It also means the
    encrypted path is exercised by every test that touches transport, so the
    wrapper cannot rot while encryption is switched off.
    """

    def encrypt(self, filename: str, payload: bytes) -> bytes:
        return payload

    def decrypt(self, filename: str, payload: bytes) -> bytes:
        return payload


@dataclass
class EncryptedTransport:
    """Any transport, with a cipher applied on the way through.

    The sender does not know this exists, which is the point: turning
    encryption on is a wiring change in app.build, not a code change.
    """

    inner: Transport
    cipher: Cipher

    def send(self, filename: str, payload: bytes) -> None:
        self.inner.send(filename, self.cipher.encrypt(filename, payload))

    def collect(self) -> list[tuple[str, bytes]]:
        return [
            (name, self.cipher.decrypt(name, data))
            for name, data in self.inner.collect()
        ]


@dataclass
class LocalTransport:
    """Files on disk. For development and integration tests.

    Mirrors the directory structure of an FTP endpoint so that swapping in the
    real transport changes the class, not the calling code or the tests.
    """

    outbox: Path
    inbox: Path
    sent: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)

    def send(self, filename: str, payload: bytes) -> None:
        target = self.outbox / filename
        if target.exists():
            # Never overwrite: a filename collision means a sequence or naming
            # bug, and silently replacing the earlier file would hide it.
            raise TransportError(f"{filename} already in outbox")
        target.write_bytes(payload)
        self.sent.append(filename)

    def collect(self) -> list[tuple[str, bytes]]:
        collected: list[tuple[str, bytes]] = []
        for path in sorted(self.inbox.iterdir()):
            if path.is_file():
                collected.append((path.name, path.read_bytes()))
                path.unlink()
        return collected