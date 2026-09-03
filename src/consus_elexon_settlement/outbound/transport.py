"""Transport, and the encryption layer that wraps it.

Two protocols, deliberately separate:

    Transport  -- moves files to and from the central systems
    Cipher     -- encrypts and decrypts them

BSCP70 Appendix 1 requires XSec encryption software, supplied by BSC CSA as
part of the communications order, with public keys exchanged before testing.

XSec is NOT a library. It is a Windows Service that watches directories:

    ENCRYPT_IN    drop a plaintext file here
    ENCRYPT_OUT   XSec deposits the encrypted file here
    DECRYPT_IN    drop an encrypted file here
    DECRYPT_OUT   XSec deposits the plaintext here
    ERROR         rejected files land here
    LOGS          audit and error logs

So encryption is asynchronous file movement, not a function call: write, wait,
read, tidy up. That is why Cipher takes a filename as well as the payload --
the unit XSec operates on is a file, and a byte-stream interface cannot
express it.

The user guide states that XSec is supported only on Microsoft Windows and
runs within .NET Framework 4.0. It cannot run in a Linux container, so
XSecCipher reaches shared storage that a Windows node also watches. Where that
storage lives, and how the Linux and Windows sides both see it, is a
deployment question this module does not answer.

Keeping the cipher separate means the sender never knows any of this.
EncryptedTransport wraps any Transport, so a test uses NullCipher and
production uses XSecCipher without the sender or the tests changing.

Outbound is push-only: participant systems push files to the central systems
and use the FTP success code as confirmation of sending (IDD 2.3). Inbound
offers push or pull; under pull, deleting the file from the source directory
is how receipt is confirmed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class TransportError(RuntimeError):
    pass


class CipherError(RuntimeError):
    """Encryption or decryption failed, or did not finish in time.

    Distinct from TransportError: a cipher failure means the file never
    reached the wire, so nothing was sent and the sequence number is still
    ours. A transport failure means we do not know.
    """


class Cipher(Protocol):
    """Encrypts and decrypts whole files.

    Takes a filename because XSec operates on files rather than streams: the
    name is how a result is matched back to its input, and how a rejection in
    the ERROR folder is identified.
    """

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
    """No encryption. For tests, and for the period before XSec is installed.

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
class XSecCipher:
    """XSec, driven through the directories its Windows Service watches.

    The flow for one file:

        1. write the payload to ENCRYPT_IN (or DECRYPT_IN)
        2. poll ENCRYPT_OUT (or DECRYPT_OUT) until the result appears
        3. poll ERROR in the same loop -- a rejected file looks exactly like
           a slow one otherwise, and we would wait out the timeout on a file
           that already failed
        4. read the result and remove it, so the next file with the same name
           is not confused with this one

    UNCONFIRMED: whether XSec preserves the filename in the output folder.
    The user guide does not say, and the audit log columns suggest input and
    output names are recorded separately. `match_by_name` therefore defaults
    to False and the poll takes whatever new file appears, which works either
    way. Set it True once the behaviour is known -- matching by name is
    stricter and catches a result landing for the wrong file.

    The timeout matters. Encryption sits between building a file and sending
    it, inside the window before Gate Closure. A cipher that hangs is
    indistinguishable from one that is slow, and both mean a missed deadline,
    so failing fast leaves time for the manual fallback.
    """

    encrypt_in: Path
    encrypt_out: Path
    decrypt_in: Path
    decrypt_out: Path
    error: Path

    timeout_seconds: float = 30.0
    poll_interval: float = 0.25
    match_by_name: bool = False

    def __post_init__(self) -> None:
        for folder in (self.encrypt_in, self.encrypt_out,
                       self.decrypt_in, self.decrypt_out, self.error):
            if not folder.is_dir():
                raise CipherError(
                    f"{folder} does not exist. XSec creates its folders when a "
                    f"Participant ID is configured in XSecManager; if they are "
                    f"missing, the participant is not set up."
                )

    def encrypt(self, filename: str, payload: bytes) -> bytes:
        return self._process(filename, payload, self.encrypt_in,
                             self.encrypt_out, "encrypt")

    def decrypt(self, filename: str, payload: bytes) -> bytes:
        return self._process(filename, payload, self.decrypt_in,
                             self.decrypt_out, "decrypt")

    def _process(
        self, filename: str, payload: bytes,
        inbox: Path, outbox: Path, operation: str,
    ) -> bytes:
        before = _names(outbox)
        errors_before = _names(self.error)

        source = inbox / filename
        if source.exists():
            raise CipherError(
                f"{filename} is already in {inbox}. Either XSec has not picked "
                f"it up or a previous run left it behind; both need looking at "
                f"before overwriting it."
            )
        source.write_bytes(payload)

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            rejected = _names(self.error) - errors_before
            if rejected:
                raise CipherError(
                    f"XSec rejected {filename} during {operation}: {sorted(rejected)} "
                    f"appeared in {self.error}. The reason is in the XSec audit log."
                )

            produced = self._result(outbox, before, filename)
            if produced is not None:
                data = produced.read_bytes()
                # Remove it so a later file of the same name is not mistaken
                # for this one. The archive holds our copy; this folder is a
                # handover point, not storage.
                produced.unlink()
                return data

            time.sleep(self.poll_interval)

        raise CipherError(
            f"XSec did not {operation} {filename} within {self.timeout_seconds}s. "
            f"Check the XSec service is running and the participant is "
            f"configured. Nothing was sent, so the sequence number is unused."
        )

    def _result(self, outbox: Path, before: set[str], filename: str) -> Path | None:
        if self.match_by_name:
            candidate = outbox / filename
            return candidate if candidate.exists() else None

        appeared = _names(outbox) - before
        if not appeared:
            return None
        if len(appeared) > 1:
            # Two results for one input means something else is writing here,
            # and picking one arbitrarily would send the wrong bytes.
            raise CipherError(
                f"{len(appeared)} new files appeared in {outbox} while processing "
                f"{filename}: {sorted(appeared)}. Expected exactly one."
            )
        return outbox / appeared.pop()


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


def _names(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir() if p.is_file()}