"""GPG encryption, in the form Elexon's central systems expect.

XSec is Elexon's own encryption software, mandated by BSCP70 Appendix 1 and
Windows-only. Their communications team confirmed the requirement is
compatibility with XSec rather than XSec itself, and supplied the equivalent
gpg invocations. This module implements those.

Encryption is TWO operations, in this order:

    1. sign   with our private key, SHA1 digest, zlib compression, armoured
    2. encrypt with the recipient public key, CAST5, ZIP compression,
       force-mdc, armoured

Decryption reverses it: decrypt with our private key to recover the signed
message, then decrypt that with their public key to recover the content. Both
stages are `--decrypt`; gpg works out which is which from the message.

The algorithm choices are not ours and are not modern. SHA1 and CAST5 are
deprecated, the keys are 1024-bit RSA, and gpg refuses all of it without
--allow-old-cipher-algos. That flag exists because the far end cannot change:
Central Services' key was generated in 2008. Do not "fix" these parameters --
a file encrypted with better algorithms is a file Elexon cannot read.

Pin the gpg version in the image. These flags have been narrowing with each
release and a base image that silently upgrades gpg is a base image that will
eventually stop being able to talk to Elexon.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .transport import CipherError

# Elexon's parameters, named rather than inlined so the reason each exists is
# recorded next to it.
DIGEST_ALGO = "SHA1"
CIPHER_ALGO = "CAST5"
SIGN_COMPRESS_ALGO = "zlib"
ENCRYPT_COMPRESS_ALGO = "ZIP"


@dataclass
class GpgCipher:
    """Signs and encrypts files the way Elexon's central systems expect.

    Unlike XSec this is synchronous: gpg is a command we run, not a service
    watching a directory. That makes it testable in CI, which XSec never could
    be, and removes a Windows node from the send path.

    `home_dir` points at the keyring. In production that is a directory
    populated at startup from Secret Manager, not a developer's ~/.gnupg: a
    private key on a laptop is a private key in every backup of that laptop.

    `passphrase` is read from the secret store and passed on a file descriptor
    rather than the command line, because command lines are visible in the
    process table to anything running on the host.
    """

    our_key: str
    their_key: str
    home_dir: Path
    passphrase: str
    gpg_binary: str = "gpg"
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if shutil.which(self.gpg_binary) is None:
            raise CipherError(
                f"{self.gpg_binary} not found on PATH. The image must install "
                f"gnupg; encryption is not optional for BSC data exchange."
            )
        if not self.home_dir.is_dir():
            raise CipherError(
                f"{self.home_dir} does not exist. The keyring directory must be "
                f"present and populated before any file can be sent."
            )

    def encrypt(self, filename: str, payload: bytes) -> bytes:
        """Sign then encrypt, in that order.

        The filename is unused -- gpg operates on content -- but the Cipher
        protocol carries it because XSec needed it, and a protocol that
        changes shape per implementation is not a protocol.
        """
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source = work / "payload"
            source.write_bytes(payload)

            signed = work / "signed.pgp"
            self._run(
                [
                    "--sign",
                    "--local-user", self.our_key,
                    "--digest-algo", DIGEST_ALGO,
                    "--compress-algo", SIGN_COMPRESS_ALGO,
                    "--armor",
                    "--output", str(signed),
                    str(source),
                ],
                what="sign",
                filename=filename,
            )

            encrypted = work / "encrypted.asc"
            self._run(
                [
                    "--encrypt",
                    # Their key is not signed by anything we trust, and never
                    # will be: this is a bilateral exchange, not a web of
                    # trust. Without this gpg refuses to use it.
                    "--trust-model", "always",
                    # SHA1 and CAST5 are deprecated. Required because Central
                    # Services' key dates from 2008 and cannot be changed.
                    "--allow-old-cipher-algos",
                    "--recipient", self.their_key,
                    "--cipher-algo", CIPHER_ALGO,
                    "--compress-algo", ENCRYPT_COMPRESS_ALGO,
                    "--force-mdc",
                    "--armor",
                    "--output", str(encrypted),
                    str(signed),
                ],
                what="encrypt",
                filename=filename,
            )

            return encrypted.read_bytes()

    def decrypt(self, filename: str, payload: bytes) -> bytes:
        """Decrypt, then verify the signature underneath.

        Two stages, both --decrypt. The first uses our private key to recover
        the signed message; the second checks Central Services' signature and
        yields the content.

        A failure at the second stage means the file decrypted but was not
        signed by them, which is a different and more serious problem than a
        file we could not decrypt at all. The error says which.
        """
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source = work / "received.asc"
            source.write_bytes(payload)

            signed = work / "signed.pgp"
            self._run(
                ["--output", str(signed), "--decrypt", str(source)],
                what="decrypt",
                filename=filename,
            )

            content = work / "content"
            self._run(
                ["--output", str(content), "--decrypt", str(signed)],
                what="verify signature on",
                filename=filename,
            )

            return content.read_bytes()

    def _run(self, args: list[str], what: str, filename: str) -> None:
        command = [
            self.gpg_binary,
            "--homedir", str(self.home_dir),
            "--batch",
            "--yes",
            # Passphrase on a file descriptor, not the command line: command
            # lines are readable in the process table.
            "--pinentry-mode", "loopback",
            "--passphrase-fd", "0",
            *args,
        ]

        try:
            result = subprocess.run(
                command,
                input=self.passphrase.encode(),
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CipherError(
                f"gpg timed out trying to {what} {filename} after "
                f"{self.timeout_seconds}s. Nothing was sent, so the sequence "
                f"number is unused."
            ) from exc

        if result.returncode != 0:
            # gpg puts everything on stderr including successes, so only the
            # tail is useful and only on failure.
            detail = result.stderr.decode(errors="replace").strip()
            raise CipherError(
                f"gpg failed to {what} {filename} (exit {result.returncode}): "
                f"{_last_lines(detail)}"
            )


def _last_lines(text: str, count: int = 4) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-count:])