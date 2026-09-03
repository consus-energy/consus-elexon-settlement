"""GPG encryption against Elexon's parameters.

These tests generate a throwaway key pair in a temporary keyring, so they run
anywhere gpg is installed and leave nothing behind. That is the whole reason
gpg is preferable to XSec: XSec could not be tested in CI at all.

The parameters are not ours. SHA1, CAST5 and 1024-bit RSA are deprecated, and
gpg refuses them without --allow-old-cipher-algos. They are required because
Central Services' key was generated in 2008 and cannot be changed. A test that
"fixed" them would be testing something Elexon cannot read.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from consus_elexon_settlement.outbound.gpg import GpgCipher
from consus_elexon_settlement.outbound.transport import CipherError

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None, reason="gpg not installed"
)

PASSPHRASE = "test-passphrase"


def generate(home: Path, name: str) -> None:
    """A throwaway 1024-bit RSA key, matching what Elexon issue."""
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    subprocess.run(
        [
            "gpg", "--homedir", str(home), "--batch", "--yes",
            "--pinentry-mode", "loopback", "--passphrase", PASSPHRASE,
            "--expert", "--quick-generate-key",
            name, "rsa1024", "cert,sign,encr,auth", "never",
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def keyring(tmp_path: Path) -> Path:
    """One keyring holding both identities.

    In production these are separate: our private key and their public key.
    Sharing a keyring here means one fixture can round-trip without an export
    and import step that tests gpg rather than our code.
    """
    home = tmp_path / "gnupg"
    generate(home, "CONSUSEN")
    generate(home, "Central-Services-01")
    return home


@pytest.fixture
def cipher(keyring: Path) -> GpgCipher:
    return GpgCipher(
        our_key="CONSUSEN",
        their_key="Central-Services-01",
        home_dir=keyring,
        passphrase=PASSPHRASE,
    )


def test_round_trip(cipher: GpgCipher):
    """Sign, encrypt, decrypt, verify. The whole path."""
    payload = b"AAA|E0041001|D|20260901100000|EN|CONSUSEN|EC|ECVAA|1|TST1|\n"
    encrypted = cipher.encrypt("EN0000000001", payload)
    assert cipher.decrypt("EN0000000001", encrypted) == payload


def test_output_is_armoured(cipher: GpgCipher):
    """Elexon's parameters specify --armor at both stages, so the file on the
    wire is ASCII rather than binary."""
    encrypted = cipher.encrypt("EN0000000001", b"data")
    assert encrypted.startswith(b"-----BEGIN PGP MESSAGE-----")


def test_encryption_is_not_deterministic(cipher: GpgCipher):
    """Two encryptions of the same content differ: the session key is random.

    Worth pinning because it rules out a tempting optimisation -- caching an
    encrypted file and reusing it -- which would leak information about
    repeated content.
    """
    first = cipher.encrypt("EN0000000001", b"identical")
    second = cipher.encrypt("EN0000000002", b"identical")
    assert first != second
    assert cipher.decrypt("x", first) == cipher.decrypt("x", second)


def test_binary_content_survives(cipher: GpgCipher):
    """Settlement files are ASCII, but nothing in the cipher should assume it:
    a cipher that mangles bytes would corrupt a checksum silently."""
    payload = bytes(range(256))
    assert cipher.decrypt("x", cipher.encrypt("x", payload)) == payload


def test_empty_payload(cipher: GpgCipher):
    assert cipher.decrypt("x", cipher.encrypt("x", b"")) == b""


def test_large_payload(cipher: GpgCipher):
    """A full settlement day of periods across a portfolio. Not large by
    modern standards, but larger than anything else in the test suite."""
    payload = b"CD9|37|0.900|\n" * 50_000
    assert cipher.decrypt("x", cipher.encrypt("x", payload)) == payload


def test_missing_key_fails_clearly(keyring: Path):
    """Naming a key that is not in the keyring should say so, not produce an
    unreadable file."""
    cipher = GpgCipher(
        our_key="NOT-A-KEY",
        their_key="Central-Services-01",
        home_dir=keyring,
        passphrase=PASSPHRASE,
    )
    with pytest.raises(CipherError, match="failed to sign"):
        cipher.encrypt("EN0000000001", b"data")


def test_wrong_passphrase_fails_clearly(keyring: Path):
    cipher = GpgCipher(
        our_key="CONSUSEN",
        their_key="Central-Services-01",
        home_dir=keyring,
        passphrase="wrong",
    )
    with pytest.raises(CipherError, match="failed to sign"):
        cipher.encrypt("EN0000000001", b"data")


def test_corrupt_input_fails_to_decrypt(cipher: GpgCipher):
    with pytest.raises(CipherError, match="failed to decrypt"):
        cipher.decrypt("EC0000000001", b"not a pgp message")


def test_missing_keyring_fails_at_construction(tmp_path: Path):
    """A missing keyring should surface at startup, not at Gate Closure."""
    with pytest.raises(CipherError, match="does not exist"):
        GpgCipher(
            our_key="CONSUSEN",
            their_key="Central-Services-01",
            home_dir=tmp_path / "nope",
            passphrase=PASSPHRASE,
        )


def test_missing_binary_fails_at_construction(keyring: Path):
    with pytest.raises(CipherError, match="not found on PATH"):
        GpgCipher(
            our_key="CONSUSEN",
            their_key="Central-Services-01",
            home_dir=keyring,
            passphrase=PASSPHRASE,
            gpg_binary="gpg-that-does-not-exist",
        )