"""Transport and the XSec handshake.

XSec is not a library. It is a Windows Service watching directories: we write
a file, it works, we read the result. That asynchrony is the thing worth
testing, because the failure modes are timing ones and none of them are
visible from reading the code.

The Windows service is simulated by a thread that moves files after a delay.
Crude, but it exercises the same three outcomes the real thing produces: a
result appears, a rejection appears in ERROR, or nothing appears at all.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from consus_elexon_settlement.outbound.transport import (
    CipherError,
    EncryptedTransport,
    LocalTransport,
    NullCipher,
    TransportError,
    XSecCipher,
)


@pytest.fixture
def xsec_root(tmp_path: Path) -> Path:
    """The folder layout XSecManager creates for a configured Participant ID."""
    for name in ("ENCRYPT_IN", "ENCRYPT_OUT", "DECRYPT_IN",
                 "DECRYPT_OUT", "ERROR", "LOGS"):
        (tmp_path / name).mkdir()
    return tmp_path


def cipher(root: Path, **kwargs) -> XSecCipher:
    return XSecCipher(
        encrypt_in=root / "ENCRYPT_IN",
        encrypt_out=root / "ENCRYPT_OUT",
        decrypt_in=root / "DECRYPT_IN",
        decrypt_out=root / "DECRYPT_OUT",
        error=root / "ERROR",
        timeout_seconds=kwargs.pop("timeout_seconds", 2.0),
        poll_interval=kwargs.pop("poll_interval", 0.02),
        **kwargs,
    )


def fake_xsec(source: Path, target: Path, transform, delay: float = 0.05,
              rename: str | None = None) -> threading.Thread:
    """Stand in for the Windows service: wait, transform, move.

    `rename` produces a differently named output, which is the case we do not
    know the answer to -- the user guide does not say whether XSec preserves
    the filename.
    """
    def run():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            files = [p for p in source.iterdir() if p.is_file()]
            if files:
                original = files[0]
                data = original.read_bytes()
                original.unlink()
                name = rename or original.name
                (target / name).write_bytes(transform(data))
                return
            time.sleep(0.01)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# --- the happy path ---------------------------------------------------------

def test_encrypt_reads_the_result_from_the_output_folder(xsec_root):
    fake_xsec(xsec_root / "ENCRYPT_IN", xsec_root / "ENCRYPT_OUT",
              lambda b: b"ENC:" + b)

    result = cipher(xsec_root).encrypt("EN0000000001", b"AAA|E0041001|D|")
    assert result == b"ENC:AAA|E0041001|D|"


def test_decrypt_uses_the_decrypt_folders(xsec_root):
    """Encryption and decryption use different folder pairs. Crossing them
    would produce a file that looks processed and is not."""
    fake_xsec(xsec_root / "DECRYPT_IN", xsec_root / "DECRYPT_OUT",
              lambda b: b.removeprefix(b"ENC:"))

    result = cipher(xsec_root).decrypt("EC0000000001", b"ENC:AAA|E0091001|D|")
    assert result == b"AAA|E0091001|D|"


def test_result_is_removed_after_reading(xsec_root):
    """The output folder is a handover point, not storage. Leaving the file
    there means the next one with the same name is ambiguous, and our copy is
    in the archive anyway."""
    fake_xsec(xsec_root / "ENCRYPT_IN", xsec_root / "ENCRYPT_OUT",
              lambda b: b"ENC:" + b)

    cipher(xsec_root).encrypt("EN0000000001", b"data")
    assert list((xsec_root / "ENCRYPT_OUT").iterdir()) == []


def test_output_name_need_not_match_the_input(xsec_root):
    """The user guide does not state whether XSec preserves filenames, so the
    default matches on 'a new file appeared' rather than on the name."""
    fake_xsec(xsec_root / "ENCRYPT_IN", xsec_root / "ENCRYPT_OUT",
              lambda b: b"ENC:" + b, rename="EN0000000001.xsec")

    assert cipher(xsec_root).encrypt("EN0000000001", b"data") == b"ENC:data"


def test_match_by_name_waits_for_the_exact_name(xsec_root):
    """Stricter, and the right setting once the behaviour is known: it catches
    a result landing for a different file."""
    fake_xsec(xsec_root / "ENCRYPT_IN", xsec_root / "ENCRYPT_OUT",
              lambda b: b"ENC:" + b, rename="SOMETHING_ELSE")

    with pytest.raises(CipherError, match="did not encrypt"):
        cipher(xsec_root, match_by_name=True, timeout_seconds=0.3).encrypt(
            "EN0000000001", b"data"
        )


# --- failure --------------------------------------------------------------

def test_rejection_in_error_folder_raises_immediately(xsec_root):
    """A rejected file looks exactly like a slow one unless ERROR is polled in
    the same loop. Without this we would wait out the full timeout on a file
    that had already failed -- and at Gate Closure that is the difference
    between falling back to manual submission and missing the deadline."""
    fake_xsec(xsec_root / "ENCRYPT_IN", xsec_root / "ERROR", lambda b: b)

    started = time.monotonic()
    with pytest.raises(CipherError, match="rejected"):
        cipher(xsec_root, timeout_seconds=5.0).encrypt("EN0000000001", b"data")

    # Raised on detection, not on timeout.
    assert time.monotonic() - started < 1.0


def test_timeout_rather_than_hanging(xsec_root):
    """Nothing is watching the folder. Encryption sits between building a file
    and sending it, so a hang is a missed deadline; failing fast leaves time
    for the fallback."""
    with pytest.raises(CipherError, match="did not encrypt"):
        cipher(xsec_root, timeout_seconds=0.2).encrypt("EN0000000001", b"data")


def test_timeout_message_says_nothing_was_sent(xsec_root):
    """Whoever reads this at 03:00 needs to know the sequence number is still
    ours and the file can be retried."""
    with pytest.raises(CipherError, match="sequence number is unused"):
        cipher(xsec_root, timeout_seconds=0.2).encrypt("EN0000000001", b"data")


def test_leftover_input_file_is_refused(xsec_root):
    """A file already sitting in ENCRYPT_IN means XSec has not picked it up or
    a previous run died. Overwriting would destroy the evidence of whichever
    it was."""
    (xsec_root / "ENCRYPT_IN" / "EN0000000001").write_bytes(b"stale")

    with pytest.raises(CipherError, match="already in"):
        cipher(xsec_root).encrypt("EN0000000001", b"data")


def test_missing_folder_fails_at_construction(tmp_path):
    """XSec creates its folders when a Participant ID is configured. Missing
    folders mean the participant is not set up, which should surface at
    startup and not at Gate Closure."""
    (tmp_path / "ENCRYPT_IN").mkdir()

    with pytest.raises(CipherError, match="does not exist"):
        cipher(tmp_path)


def test_two_results_are_ambiguous(xsec_root):
    """Something else is writing to the output folder. Picking one arbitrarily
    would send the wrong bytes under our sequence number."""
    out = xsec_root / "ENCRYPT_OUT"

    def two_results():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            files = [p for p in (xsec_root / "ENCRYPT_IN").iterdir() if p.is_file()]
            if files:
                files[0].unlink()
                (out / "one").write_bytes(b"a")
                (out / "two").write_bytes(b"b")
                return
            time.sleep(0.01)

    threading.Thread(target=two_results, daemon=True).start()

    with pytest.raises(CipherError, match="Expected exactly one"):
        cipher(xsec_root).encrypt("EN0000000001", b"data")


# --- wrapping ---------------------------------------------------------------

def test_encrypted_transport_passes_the_filename_through(tmp_path):
    """The cipher needs the filename because XSec operates on files. This
    checks the wrapper forwards it rather than dropping it."""
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


def test_null_cipher_is_a_pass_through(tmp_path):
    """Used before XSec is installed, and in every test that touches
    transport -- which means the encrypted path stays exercised even while
    encryption is off."""
    transport = EncryptedTransport(
        inner=LocalTransport(outbox=tmp_path / "out", inbox=tmp_path / "in"),
        cipher=NullCipher(),
    )
    transport.send("EN0000000001", b"payload")
    assert (tmp_path / "out" / "EN0000000001").read_bytes() == b"payload"


def test_local_transport_refuses_to_overwrite(tmp_path):
    """A filename collision means a sequence or naming bug. Silently replacing
    the earlier file would hide it."""
    transport = LocalTransport(outbox=tmp_path / "out", inbox=tmp_path / "in")
    transport.send("EN0000000001", b"first")

    with pytest.raises(TransportError, match="already in outbox"):
        transport.send("EN0000000001", b"second")


def test_collect_removes_what_it_reads(tmp_path):
    """Under the pull method, deleting from the source directory is how
    receipt is confirmed (IDD 2.3)."""
    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / "EC0000000001").write_bytes(b"inbound")

    transport = LocalTransport(outbox=tmp_path / "out", inbox=inbox)
    assert transport.collect() == [("EC0000000001", b"inbound")]
    assert list(inbox.iterdir()) == []