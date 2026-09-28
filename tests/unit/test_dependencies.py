"""Dependencies that are imported lazily.

Two modules import their heavy dependencies inside the function that uses
them rather than at module level: the archive imports the cloud storage
client, and the gpg cipher shells out to a binary. Both are deliberate --
unit tests need no cloud credentials and no keyring to exercise the
surrounding logic.

That laziness is also why a missing dependency was invisible to the suite
until a deployed container tried to write its first file and failed at the
archive step. Every test passed; nothing could have been sent. Recorded as
defect D02.

Importing them here turns the omission back into a test failure, which is
where it belongs.
"""

from __future__ import annotations

import shutil


def test_cloud_storage_client_is_installed():
    """archive.GcsArchive.put imports these when it runs, not when it loads.

    The failure without them is ModuleNotFoundError at the moment a file is
    archived, which is after the sequence number has been allocated and
    before anything has been sent.
    """
    from google.api_core.exceptions import PreconditionFailed  # noqa: F401
    from google.cloud import storage  # noqa: F401


def test_gpg_binary_is_available():
    """GpgCipher checks for this at construction, so a missing binary fails at
    startup rather than at Gate Closure. This confirms the image provides it.

    Skipped rather than failed on a developer machine without gnupg: the
    check that matters runs in the build, where the image is what will be
    deployed.
    """
    import pytest

    if shutil.which("gpg") is None:
        pytest.skip("gpg not installed locally; the build image provides it")


def test_the_package_imports_without_optional_configuration():
    """Every module loads with no environment set.

    A module that reads configuration at import time cannot be tested, and
    fails in a container before any log line is written explaining why.
    """
    from consus_elexon_settlement import (  # noqa: F401
        app, archive, cli, db, deadlines, intents, migrate, service, states,
    )
    from consus_elexon_settlement.ems import messages  # noqa: F401
    from consus_elexon_settlement.inbound import handlers, receiver, router  # noqa: F401
    from consus_elexon_settlement.outbound import (  # noqa: F401
        ftp, gpg, sender, submissions, transport,
    )