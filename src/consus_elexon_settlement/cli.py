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
    EncryptedTransport,
    LocalTransport,
    NullCipher,
    Transport,
)

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
    """Transport, wrapped in encryption if a cipher is configured.

    XSec is supplied by BSC CSA with the communications order and is not yet
    installed, so NullCipher is the current implementation. It is explicit
    rather than an absent wrapper: 'no encryption' should be a visible
    decision in the logs, not an omission.
    """
    inner: Transport = LocalTransport(
        outbox=Path(_require("CONSUS_OUTBOX")),
        inbox=Path(_require("CONSUS_INBOX")),
    )
    cipher = NullCipher()
    log.info("transport=%s cipher=%s", type(inner).__name__, type(cipher).__name__)
    return EncryptedTransport(inner=inner, cipher=cipher)


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

        u = deadlines.urgency(settlement_date, settlement_period, now)
        message = f"file {file_id} ({state}): {u}"
        if u.level in ("CRITICAL", "MISSED"):
            log.error("%s [%s]", message, u.level)
            critical.append(file_id)
        elif u.level == "WARNING":
            log.warning("%s [%s]", message, u.level)
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
    real one. See the Pub/Sub boundary in the design notes.
    """
    raise NotImplementedError(
        "submission is triggered by the EMS over Pub/Sub; that boundary is not "
        "yet built. Use the Python API directly in the meantime."
    )


def reconcile(args: argparse.Namespace) -> int:
    """Placeholder for daily reconciliation.

    Compares what we traded against what was accepted against what was
    settled. Not built: the settlement side needs SAA report parsing, which
    is currently handled generically.
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