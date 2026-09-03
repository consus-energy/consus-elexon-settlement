"""Entry points.

Four commands, all thin. Each parses arguments, builds the gateway and calls
one method. Nothing here decides anything: if a command grows a conditional
about settlement, that logic belongs in a module and the command should call
it.

    collect     pull, parse, handle and acknowledge waiting files
    sweep       find outstanding submissions and alert on the pressing ones
    submit      build and send one file
    reconcile   compare what we traded, sent, and were told

collect and sweep are scheduled and idempotent, which makes them Cloud Run
Jobs rather than endpoints on a service. submit is triggered by the EMS and is
the only one that needs to be reachable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

from . import app, db, deadlines, states
from .archive import Archive, GcsArchive, LocalArchive
from .outbound.transport import (
    Cipher,
    EncryptedTransport,
    LocalTransport,
    NullCipher,
    Transport,
    XSecCipher,
)
from .outbound.gpg import GpgCipher

log = logging.getLogger("consus.settlement")


def bootstrap() -> tuple[app.Gateway, app.Config, str]:
    """Read the environment and assemble the gateway.

    One place, so every command builds the same thing the same way. A command
    that constructs its own archive or transport will eventually construct a
    different one, and the difference will show up as a file in the wrong
    bucket.
    """
    config = app.Config.from_env()
    dsn = _require("CONSUS_SETTLEMENT_DSN")

    return app.build(
        config=config,
        dsn=dsn,
        archive=_archive(),
        transport=_transport(),
        store_key=_key_store(),
    ), config, dsn


def _archive() -> Archive:
    bucket = os.environ.get("CONSUS_ARCHIVE_BUCKET")
    if bucket:
        return GcsArchive(bucket_name=bucket)

    root = os.environ.get("CONSUS_ARCHIVE_PATH")
    if not root:
        raise RuntimeError(
            "set CONSUS_ARCHIVE_BUCKET for GCS or CONSUS_ARCHIVE_PATH for local. "
            "There is no default: an archive silently pointing at /tmp would "
            "lose the audit trail without anyone noticing."
        )
    return LocalArchive(root=Path(root))


def _transport() -> Transport:
    """Transport, wrapped in encryption when XSec is configured.

    XSec is a Windows Service that watches directories rather than a library
    we call, so it is configured by paths rather than credentials. The Linux
    process writes into a folder a Windows node also sees; where that shared
    storage lives is a deployment question, not one for this module.

    When CONSUS_XSEC_ROOT is absent we run with NullCipher. That is correct
    before the communications order completes, but it is logged as a warning:
    'no encryption' should be a visible decision rather than an omission
    nobody noticed.
    """
    inner: Transport = LocalTransport(
        outbox=Path(_require("CONSUS_OUTBOX")),
        inbox=Path(_require("CONSUS_INBOX")),
    )

    cipher = _cipher()
    log.info(
        "transport=%s cipher=%s", type(inner).__name__, type(cipher).__name__
    )
    return EncryptedTransport(inner=inner, cipher=cipher)


def _cipher_xsec() -> Cipher:
    root = os.environ.get("CONSUS_XSEC_ROOT")
    if not root:
        log.warning(
            "CONSUS_XSEC_ROOT is not set: files will be sent UNENCRYPTED. "
            "Correct before the BSC communications order completes; wrong "
            "after it."
        )
        return NullCipher()

    base = Path(root)
    return XSecCipher(
        encrypt_in=base / "ENCRYPT_IN",
        encrypt_out=base / "ENCRYPT_OUT",
        decrypt_in=base / "DECRYPT_IN",
        decrypt_out=base / "DECRYPT_OUT",
        error=base / "ERROR",
        # Encryption sits between building a file and sending it, inside the
        # window before Gate Closure. Thirty seconds is generous for a file of
        # this size and still leaves time to fall back to manual submission.
        timeout_seconds=float(os.environ.get("CONSUS_XSEC_TIMEOUT", "30")),
        # Whether XSec preserves the filename in the output folder is not
        # stated in the user guide. Off by default, which works either way;
        # turn it on once the behaviour has been observed.
        match_by_name=os.environ.get("CONSUS_XSEC_MATCH_BY_NAME") == "1",
    )

def _cipher() -> Cipher:
    """The cipher, chosen by what is configured.

    Elexon's communications team confirmed the requirement is compatibility
    with XSec rather than XSec itself, and supplied the equivalent gpg
    parameters. gpg is preferred: it runs in the same container as everything
    else, it is testable in CI, and it removes a Windows node from the send
    path.

    XSec remains available as a fallback while gpg interoperability is being
    confirmed with Central Services.
    """
    gnupg_home = os.environ.get("CONSUS_GNUPGHOME")
    if gnupg_home:
        return GpgCipher(
            our_key=_require("CONSUS_GPG_KEY"),
            their_key=os.environ.get("CONSUS_GPG_RECIPIENT", "Central-Services-01"),
            home_dir=Path(gnupg_home),
            passphrase=_require("CONSUS_GPG_PASSPHRASE"),
        )

    xsec_root = os.environ.get("CONSUS_XSEC_ROOT")
    if xsec_root:
        base = Path(xsec_root)
        return XSecCipher(
            encrypt_in=base / "ENCRYPT_IN",
            encrypt_out=base / "ENCRYPT_OUT",
            decrypt_in=base / "DECRYPT_IN",
            decrypt_out=base / "DECRYPT_OUT",
            error=base / "ERROR",
            timeout_seconds=float(os.environ.get("CONSUS_XSEC_TIMEOUT", "30")),
            match_by_name=os.environ.get("CONSUS_XSEC_MATCH_BY_NAME") == "1",
        )

    log.warning(
        "No cipher configured: files will be sent UNENCRYPTED. Set "
        "CONSUS_GNUPGHOME for gpg, or CONSUS_XSEC_ROOT for XSec. Correct "
        "before the BSC communications setup completes; wrong after it."
    )
    return NullCipher()


def _key_store():
    """Where an ECVNAA key is written when E0071 arrives.

    Returns None until Secret Manager is wired, which makes app.build install
    the refusing default. That is deliberate: a key discarded silently leaves
    us unable to submit any ECVN, with nothing in the logs to say why.
    """
    return None


# --- commands ---------------------------------------------------------------


def collect(args: argparse.Namespace) -> int:
    """Pull, parse, handle and acknowledge everything waiting."""
    gateway, _, _ = bootstrap()
    results = gateway.collect()

    if not results:
        log.info("nothing waiting")
        return 0

    failed = [r for r in results if not r.ok]
    for result in results:
        log.info(
            "%s parsed=%s acknowledged=%s%s",
            result.filename,
            result.received.parsed if result.received else False,
            result.acknowledged,
            f" error={result.error}" if result.error else "",
        )

    log.info("collected %d, %d with problems", len(results), len(failed))
    # Exit non-zero so the Job shows as failed and the scheduler alerts. The
    # files are archived either way; the exit code is how a human finds out.
    return 1 if failed else 0


def sweep(args: argparse.Namespace) -> int:
    """Find outstanding submissions and report the pressing ones.

    The threshold is not a fixed age. A file sent twenty minutes ago for a
    period closing in ten is urgent; the same file for tomorrow is not. So the
    sweep marks anything silent for longer than the grace period, then judges
    each against its own gate closure.
    """
    _, _, dsn = bootstrap()
    now = dt.datetime.now(dt.timezone.utc)
    grace = dt.timedelta(minutes=args.grace)

    with db.connect(dsn) as conn:
        swept = db.mark_unacknowledged(conn, older_than=now - grace)
        outstanding = _outstanding(conn)

    if swept:
        log.warning("%d file(s) unacknowledged after %s: %s", len(swept), grace, swept)

    critical = []
    for file_id, settlement_date, settlement_period, state in outstanding:
        if settlement_date is None:
            # Registration and authorisation files carry no settlement period,
            # so there is no gate closure to measure them against. Still worth
            # reporting, but not against a deadline.
            log.info("file %s outstanding in %s, no settlement period", file_id, state)
            continue

        urgency = deadlines.urgency(settlement_date, settlement_period, now)
        message = f"file {file_id} ({state}): {urgency}"
        if urgency.level in ("CRITICAL", "MISSED"):
            log.error("%s [%s]", message, urgency.level)
            critical.append(file_id)
        elif urgency.level == "WARNING":
            log.warning("%s [%s]", message, urgency.level)
        else:
            log.info("%s", message)

    if critical:
        log.error(
            "%d submission(s) at or past gate closure. The manual fallback via "
            "the central system web interface is the remedy: see the runbook.",
            len(critical),
        )
        return 1
    return 0


def submit(args: argparse.Namespace) -> int:
    """Placeholder for EMS-triggered submission.

    Left unimplemented rather than guessed at: the interface from the EMS is
    not yet decided, and inventing one here would make it harder to adopt the
    real one.
    """
    raise NotImplementedError(
        "submission is triggered by the EMS over Pub/Sub; that boundary is not "
        "yet built. Use the Python API directly in the meantime."
    )


def reconcile(args: argparse.Namespace) -> int:
    """Placeholder for daily reconciliation.

    Compares what we traded against what was accepted against what was
    settled. Not built: the settlement side needs SAA report parsing, which is
    currently handled generically.
    """
    raise NotImplementedError("reconciliation is not yet built")


def _outstanding(conn) -> list[tuple[int, dt.date | None, int | None, str]]:
    """Outbound files still waiting on a response, with the period they serve.

    A file's settlement period is not on outbound_file: it belongs to the
    items inside. WMAN is the one that matters for gate closure, so it is
    joined directly; the others carry effective dates rather than a single
    period.
    """
    rows = conn.execute(
        """SELECT f.id, w.settlement_date, w.settlement_period, f.state
             FROM outbound_file f
             LEFT JOIN wman w ON w.outbound_file_id = f.id
            WHERE f.state = ANY(%s)
            ORDER BY f.id""",
        (list(states.OUTSTANDING_FILE_STATES),),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="consus-settlement")
    parser.add_argument(
        "--log-level", default=os.environ.get("CONSUS_LOG_LEVEL", "INFO")
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("collect", help="pull, parse and acknowledge waiting files")

    sweep_parser = sub.add_parser("sweep", help="report outstanding submissions")
    sweep_parser.add_argument(
        "--grace",
        type=int,
        default=10,
        help="minutes of silence before a sent file is marked unacknowledged",
    )

    sub.add_parser("submit", help="build and send one file")
    sub.add_parser("reconcile", help="compare traded, accepted and settled")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    commands = {
        "collect": collect,
        "sweep": sweep,
        "submit": submit,
        "reconcile": reconcile,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())