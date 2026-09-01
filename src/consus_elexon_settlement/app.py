"""Composition root: where the pieces are wired together.

Everything below this module is ignorant of everything else -- file.py does
not know about the database, the router does not know which handlers exist,
the sender does not know whether transport is encrypted. This is the only
place that knows all of it, which is what keeps the rest testable in
isolation.

Nothing here does work. It reads configuration, builds objects and returns
them. If a function in this module grows a branch about settlement, it is in
the wrong place.

The Gateway returned at the end holds the pieces and offers the two verbs a
scheduler needs: collect what has arrived, send what is due. It does not
decide when either happens -- that depends on Gate Closure, which is a
business concern rather than a wiring one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import db
from .archive import Archive
from .idd import spec, spec_cra, spec_saa, spec_svaa
from .inbound import ecvaa
from .inbound.handlers import EcvaaHandlers
from .inbound.reports import ReportHandler
from .inbound.router import Handler, Received, Router
from .outbound.sender import Sender
from .outbound.transport import Transport

# IDD 2.2.1 field 10: the test data flag. 'OPER' or omitted means operational;
# any other value is a test phase. Held here so the comparison is in one place.
OPERATIONAL = "OPER"

# Flows we receive and record but do not act on.
#
# Versions are listed explicitly rather than matched by prefix. Registering a
# handler against a version Elexon does not send means the file arrives, is
# acknowledged, and is silently never processed -- which looks identical to
# working. Confirm the live versions before go-live.
REPORT_FILE_TYPES = (
    "E0131001", "E0131002",                 # Authorisation Report
    "E0141003", "E0141004",                 # Notification Report
    "E0221001",                             # Forward Contract Report
    "P0285001", "P0285002",                 # Delivered Volume Exception
    "P0288001", "P0288002", "P0288003",     # Secondary HH Consumption
    "P0333001", "P0333002",                 # Baselining Expected Volume Report
)


@dataclass(frozen=True)
class Identity:
    """One of our sender identities.

    We hold two. WMAN and the SVAA flows go out as the VTP; ECVNs go out as
    the ECVN Agent, because only an ECVNA may submit one. They are different
    participants with separate sequence counters, and mixing them corrupts
    both sequences in a way that cannot be corrected retrospectively.
    """

    role: str
    participant: str


@dataclass(frozen=True)
class Config:
    vtp: Identity
    ecvna: Identity
    environment: str

    @classmethod
    def from_env(cls) -> "Config":
        """Read configuration, failing if it is incomplete.

        Deliberately no defaults for the participant ids. A gateway that
        starts with the wrong participant id sends files that are rejected,
        and the failure surfaces at Gate Closure rather than at startup.

        The environment defaults to a test value rather than to operational:
        the safe direction for a missing variable is 'this is a test', not
        'send this to live settlement'.
        """
        return cls(
            vtp=Identity("VT", _require("CONSUS_VTP_PARTICIPANT")),
            ecvna=Identity("EN", _require("CONSUS_ECVNA_PARTICIPANT")),
            environment=os.environ.get("CONSUS_ENVIRONMENT", "TST1"),
        )

    @property
    def identities(self) -> dict[str, str]:
        return {
            self.vtp.role: self.vtp.participant,
            self.ecvna.role: self.ecvna.participant,
        }

    @property
    def is_operational(self) -> bool:
        return self.environment == OPERATIONAL

    @property
    def test_flag(self) -> str:
        """The channel's test flag, as stored.

        Empty string for operational, because that is how the header field is
        omitted and how the channel table records it.
        """
        return "" if self.is_operational else self.environment


@dataclass(frozen=True)
class Handlers:
    """The inbound handlers, gathered so build_router takes one argument
    rather than a list that grows with every flow."""

    ecvn_rejection: Handler
    ecvn_acceptance: Handler
    wman_exception: Handler
    report: Handler


@dataclass(frozen=True)
class Gateway:
    """The assembled gateway.

    Holds the pieces and offers the two verbs a scheduler needs. It does not
    decide when to call them: that depends on Gate Closure, which is a
    business concern rather than a wiring one.
    """

    router: Router
    sender: Sender
    transport: Transport

    def collect(self) -> list[Received]:
        """Collect and process every waiting inbound file.

        Each response goes back through the same transport. A file that fails
        to parse still gets one -- that is why the router returns a response
        rather than raising, and why this loop does not skip on error.

        Processing continues past a failure. One malformed file must not stop
        the others being read, because one of those others may be a rejection
        that needs acting on before Gate Closure.
        """
        results: list[Received] = []
        for filename, payload in self.transport.collect():
            result = self.router.receive(payload, filename)
            self.transport.send(response_filename(filename), result.response)
            results.append(result)
        return results


def build(
    config: Config,
    dsn: str,
    archive: Archive,
    transport: Transport,
) -> Gateway:
    """Everything wired. The one call a process makes at startup.

    Archive and transport are passed in rather than constructed here, so a
    test supplies LocalArchive and LocalTransport without this function
    growing a branch on environment. The choice of implementation belongs to
    whoever starts the process.

    `dsn` rather than a connection: handlers and the sender each open one per
    operation. A connection held open across a long-running poller is a
    connection that will be dead when it matters.
    """

    def connect():
        return db.connect(dsn)

    handlers = EcvaaHandlers(connect=connect)

    return Gateway(
        router=build_router(
            config,
            Handlers(
                ecvn_rejection=handlers.ecvn_rejection,
                ecvn_acceptance=handlers.ecvn_acceptance,
                wman_exception=handlers.wman_exception,
                report=ReportHandler(connect=connect),
            ),
        ),
        sender=Sender(connect=connect, archive=archive, transport=transport),
        transport=transport,
    )


def build_router(config: Config, handlers: Handlers) -> Router:
    """The inbound router, with every spec and handler registered.

    All four central systems' specs are loaded regardless of which flows we
    currently handle. An unexpected file is then diagnosed as *unhandled*
    rather than *unknown*: the first means Elexon sent something we were not
    expecting, the second means we could not read it at all. Different
    problems, and only one of them is ours.
    """
    router = Router(identities=config.identities)

    router.add_spec(spec.SPEC)          # ECVAA
    router.add_spec(spec_svaa.SPEC)
    router.add_spec(spec_cra.SPEC)
    router.add_spec(spec_saa.SPEC)

    router.register(ecvaa.REJECTION_FILE_TYPE, handlers.ecvn_rejection)
    router.register(ecvaa.ACCEPTANCE_FILE_TYPE, handlers.ecvn_acceptance)
    router.register(ecvaa.WMAN_EXCEPTION_FILE_TYPE, handlers.wman_exception)

    # Reports are registered so an arriving report is recorded rather than
    # counted as unhandled. The difference matters: unhandled means Elexon
    # sent something we were not expecting, which is worth investigating.
    #
    # An unknown file type here raises at startup rather than being skipped.
    # A typo that silently registers nothing would look identical to working
    # until the report arrived and was ignored.
    for file_type in REPORT_FILE_TYPES:
        if router.flow(file_type) is None:
            raise RuntimeError(
                f"{file_type} is listed as a report but is not in any loaded spec"
            )
        router.register(file_type, handlers.report)

    return router


def channel_for(
    conn, config: Config, identity: Identity, to_role: str, to_participant: str
) -> db.Channel:
    """The channel for one of our identities to a central system.

    A convenience over db.get_channel that supplies the test flag from
    configuration, so a caller cannot accidentally reach the operational
    channel from a test process. The flag is part of the channel's unique key,
    so the wrong flag finds no channel and raises rather than sending.
    """
    return db.get_channel(
        conn,
        from_role_code=identity.role,
        from_participant_id=identity.participant,
        to_role_code=to_role,
        to_participant_id=to_participant,
        test_flag=config.test_flag,
    )


def response_filename(received: str) -> str:
    """The name of our response to a received file.

    IDD 2.2.5: filenames are 14 characters, unique across all central systems
    within a month, and the first two characters are the sender's role code.

    UNCONFIRMED. The IDD states the naming rule for files we originate; it is
    not stated whether a response derives its name from the file it answers or
    takes a fresh name. This returns the received name unchanged, which is the
    reading that lets the recipient correlate without opening the file.
    Confirm against the COMMS document before go-live -- it is on the open
    items list.
    """
    if len(received) != 14:
        raise ValueError(
            f"filename must be 14 characters, got {len(received)}: {received}"
        )
    return received


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value