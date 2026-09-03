"""Entry points.

Seven commands, all thin. Each parses arguments, builds what it needs and
calls one method. Nothing here decides anything: if a command grows a
conditional about settlement, that logic belongs in a module and the command
should call it.

    migrate     apply outstanding schema migrations
    channel     create a channel, or list what exists
    collect     pull, parse, handle and acknowledge waiting files
    sweep       find outstanding submissions and alert on the pressing ones
    submit      build and send one file
    reconcile   compare what we traded, sent, and were told

collect and sweep are scheduled and idempotent, which makes them Cloud Run
Jobs rather than endpoints on a service. submit is triggered by the EMS and is
the only one that needs to be reachable. migrate and channel are run on
demand, from a deployment step or by hand.

migrate and channel deliberately do NOT go through bootstrap(). They need a
database connection and nothing else -- no transport, no archive, no keys. A
schema migration blocked by missing FTP configuration would be an absurd
dependency, and the first time it mattered would be an incident.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

from . import app, db, deadlines, states
from . import migrate as migrations
from .archive import Archive, GcsArchive, LocalArchive
from .outbound.gpg import GpgCipher
from .outbound.transport import (
    Cipher,
    EncryptedTransport,
    LocalTransport,
    NullCipher,
    Transport,
    XSecCipher,
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
    """Transport, wrapped in encryption.

    The process writes into a folder; how that folder reaches Elexon is the
    transport's problem, not this function's.
    """
    inner: Transport = LocalTransport(
        outbox=Path(_require("CONSUS_OUTBOX")),
        inbox=Path(_require("CONSUS_INBOX")),
    )

    cipher = _cipher()
    log.info("transport=%s cipher=%s", type(inner).__name__, type(cipher).__name__)
    return EncryptedTransport(inner=inner, cipher=cipher)


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
            passphrase=_read_secret_file("CONSUS_GPG_PASSPHRASE_FILE"),
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


def _read_secret_file(name: str) -> str:
    """Read a secret mounted as a file.

    Cloud Run mounts secrets as files rather than environment variables, and
    that is the right way round: a value in the environment is readable
    through /proc by anything in the container.
    """
    path = os.environ.get(name)
    if not path:
        raise RuntimeError(f"{name} is not set")
    return Path(path).read_text().strip()


def _key_store():
    """Where an ECVNAA key is written when E0071 arrives.

    Returns None until Secret Manager is wired, which makes app.build install
    the refusing default. That is deliberate: a key discarded silently leaves
    us unable to submit any ECVN, with nothing in the logs to say why.
    """
    return None


# --- commands ---------------------------------------------------------------


def migrate(args: argparse.Namespace) -> int:
    """Apply outstanding schema migrations.

    Takes only a DSN. Not routed through bootstrap() because a migration
    should not be blocked by transport configuration it does not use.

    Safe to run repeatedly: applied migrations are recorded with a checksum,
    so a second run applies nothing and an edited migration fails loudly
    rather than diverging between environments.
    """
    dsn = _require("CONSUS_SETTLEMENT_DSN")

    with db.connect(dsn) as conn:
        before = migrations.current_version(conn)

        if args.check:
            # Report and exit non-zero, applying nothing. For a deployment
            # gate: a deploy that assumes the schema is current, when it is
            # not, fails at the first query rather than at startup.
            already = migrations.applied(conn)
            outstanding = [
                m for m in migrations.discover() if m.version not in already
            ]
            if outstanding:
                for m in outstanding:
                    log.warning("outstanding: %s", m.name)
                return 1
            log.info("schema is up to date at version %s", before)
            return 0

        applied = migrations.migrate(conn)

    if not applied:
        log.info("no migrations to apply, schema at version %s", before)
        return 0

    for m in applied:
        log.info("applied %s", m.name)
    log.info(
        "%d migration(s) applied, now at version %s",
        len(applied),
        applied[-1].version,
    )
    return 0


def channel(args: argparse.Namespace) -> int:
    """Create a channel, or list what exists.

    A channel is the sequence counter for one sender identity talking to one
    central system. Nothing can be built without one, because the sequence
    number comes from it.

    The test flag is NOT an argument. It comes from CONSUS_ENVIRONMENT, and
    it is part of the channel's unique key, so a test process cannot create or
    reach an operational channel. That is the structural control described in
    ADR-0010: not a check that can be overridden, but an absence that stops
    the header being buildable at all.

    Role codes are required rather than defaulted. They are defined in IDD
    Part 1 section 2.2.1 and getting one wrong means every file on that
    channel is rejected -- so a wrong value should come from a human who was
    asked, not from a default nobody questioned.
    """
    dsn = _require("CONSUS_SETTLEMENT_DSN")
    config = app.Config.from_env()

    with db.connect(dsn) as conn:
        if args.list:
            rows = db.list_channels(conn)
            if not rows:
                log.info("no channels. Nothing can be sent until one exists.")
                return 0
            for row in rows:
                (channel_id, from_role, from_party, to_role, to_party,
                 flag, next_seq, gaps) = row
                log.info(
                    "%3d  %s/%s -> %s/%s  flag=%-4s next=%d%s",
                    channel_id, from_role, from_party, to_role, to_party,
                    flag or "(oper)", next_seq,
                    "  gaps allowed" if gaps else "",
                )
            return 0

        if not (args.from_role and args.from_participant
                and args.to_role and args.to_participant):
            log.error(
                "creating a channel needs --from-role, --from-participant, "
                "--to-role and --to-participant"
            )
            return 2

        created = db.ensure_channel(
            conn,
            from_role_code=args.from_role,
            from_participant_id=args.from_participant,
            to_role_code=args.to_role,
            to_participant_id=args.to_participant,
            test_flag=config.test_flag,
            allows_sequence_gaps=args.allow_gaps,
        )

    log.info(
        "channel %d: %s/%s -> %s/%s flag=%s next_sequence=%d",
        created.id,
        created.from_role_code, created.from_participant_id,
        created.to_role_code, created.to_participant_id,
        created.test_flag or "(operational)",
        created.next_sequence,
    )
    if created.next_sequence > 1:
        log.info(
            "channel already existed and has sent %d file(s). Nothing was "
            "changed: resetting a sequence would produce duplicates that "
            "central systems reject.",
            created.next_sequence - 1,
        )
    return 0


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

    migrate_parser = sub.add_parser("migrate", help="apply schema migrations")
    migrate_parser.add_argument(
        "--check",
        action="store_true",
        help="report outstanding migrations and exit non-zero, applying nothing",
    )

    channel_parser = sub.add_parser(
        "channel", help="create a channel, or list what exists"
    )
    channel_parser.add_argument(
        "--list", action="store_true", help="list channels and their sequence position"
    )
    channel_parser.add_argument(
        "--from-role",
        help="our role code in the file header, IDD 2.2.1 field 5",
    )
    channel_parser.add_argument(
        "--from-participant", help="our participant id for that role"
    )
    channel_parser.add_argument(
        "--to-role", help="the central system's role code, IDD 2.2.1 field 7"
    )
    channel_parser.add_argument(
        "--to-participant", help="the central system's participant id"
    )
    channel_parser.add_argument(
        "--allow-gaps",
        action="store_true",
        help=(
            "SVAA tolerates gaps in our sequence numbering; ECVAA does not. "
            "Set this for SVAA channels only."
        ),
    )

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
        "migrate": migrate,
        "channel": channel,
        "collect": collect,
        "sweep": sweep,
        "submit": submit,
        "reconcile": reconcile,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())