"""Transport, and the encryption layer that wraps it.

Two protocols, deliberately separate:

    Transport  -- moves bytes to and from the central systems
    Cipher     -- encrypts and decrypts them

BSCP70 Appendix 1 requires XSec encryption software, supplied by BSC CSA as
part of the communications order, with public keys exchanged before testing.
Files are signed and encrypted before transmission and decrypted on receipt.

Keeping the cipher separate means the sender never knows whether encryption
is on. EncryptedTransport wraps any Transport, so a test uses NullCipher and
production uses XSecCipher without either the sender or the tests changing.

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


class Cipher(Protocol):
    def encrypt(self, payload: bytes) -> bytes: ...
    def decrypt(self, payload: bytes) -> bytes: ...


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
    """No encryption. For tests, and for the period before XSec is installed.

    Explicit rather than an Optional[Cipher]: a None cipher reads as an
    oversight, whereas NullCipher reads as a decision.
    """

    def encrypt(self, payload: bytes) -> bytes:
        return payload

    def decrypt(self, payload: bytes) -> bytes:
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
        self.inner.send(filename, self.cipher.encrypt(payload))

    def collect(self) -> list[tuple[str, bytes]]:
        return [(name, self.cipher.decrypt(data)) for name, data in self.inner.collect()]


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