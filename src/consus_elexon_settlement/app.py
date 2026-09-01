"""Composition root: where the pieces are wired together.

Everything below this module is ignorant of everything else -- file.py does
not know about the database, the router does not know which handlers exist,
the sender does not know whether transport is encrypted. This is the only
place that knows all of it, which is what keeps the rest testable in
isolation.

Nothing here does work. It reads configuration, builds objects and returns
them. If a function in this module grows a branch about settlement, it is in
the wrong place.

The Gateway returned at the end holds the two halves -- receiver and sender --
and does not decide when either runs. That depends on Gate Closure, which is a
business concern rather than a wiring one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import db
from .archive import Archive
from .idd import spec, spec_cra, spec_saa, spec_svaa
from .inbound import ecvaa, svaa
from .inbound.handlers import EcvaaHandlers, EcvnaaHandler, SvaaHandlers
from .inbound.receiver import Collected, Receiver
from .inbound.reports import ReportHandler
from .inbound.router import Handler, Router
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
    "E0131001", "E0131002",                 # Authorisation Report, sub-flow 1
    "E0132001", "E0132002",                 # Authorisation Report, sub-flow 2
    "E0141003", "E0141004",                 # Notification Report
    "E0221002",                             # Forward Contract Report
    "P0285001", "P0285002",                 # Delivered Volume Exception
    "P0288001", "P0288002", "P0288003",     # Secondary HH Consumption
    "P0333001", "P0333002",                 # Baselining Expected Volume Report
)

# E0071 has three versions differing only in optional records. The EAD record
# carrying the ECVNAA Key is present in all three, so all three are registered
# rather than assuming which one ECVAA sends.
ECVNAA_FEEDBACK_FILE_TYPES = ("E0071001", "E0071002", "E0071003")


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
    sev_acceptance: Handler
    sev_rejection: Handler
    sev_warning: Handler
    delivered_confirmation: Handler
    delivered_rejection: Handler
    ecvnaa_confirmation: Handler
    report: Handler


@dataclass(frozen=True)
class Gateway:
    """The assembled gateway.

    Holds the two halves and offers the verbs a scheduler needs. It does not
    decide when to call them: that depends on Gate Closure, which is a
    business concern rather than a wiring one.
    """

    receiver: Receiver
    sender: Sender

    def collect(self) -> list[Collected]:
        """Archive, record, route and acknowledge every waiting file.

        One failure does not stop the rest: a malformed file must not prevent
        the next being read, because that next one may be a rejection needing
        action before Gate Closure.
        """
        return self.receiver.collect()


def build(
    config: Config,
    dsn: str,
    archive: Archive,
    transport: Transport,
    store_key=None,
) -> Gateway:
    """Everything wired. The one call a process makes at startup.

    Archive and transport are passed in rather than constructed here, so a
    test supplies LocalArchive and LocalTransport without this function
    growing a branch on environment. The choice of implementation belongs to
    whoever starts the process.

    `dsn` rather than a connection: handlers and the sender each open one per
    operation. A connection held open across a long-running poller is a
    connection that will be dead when it matters.

    `store_key` writes an ECVNAA key to the secret store and returns a
    reference. It defaults to a function that refuses, so a deployment which
    forgets to supply one fails at the moment the key arrives rather than
    discarding it silently and leaving us unable to submit ECVNs.
    """

    def connect():
        return db.connect(dsn)

    ecvaa_handlers = EcvaaHandlers(connect=connect)
    svaa_handlers = SvaaHandlers(connect=connect)

    handlers = Handlers(
        ecvn_rejection=ecvaa_handlers.ecvn_rejection,
        ecvn_acceptance=ecvaa_handlers.ecvn_acceptance,
        wman_exception=ecvaa_handlers.wman_exception,
        sev_acceptance=svaa_handlers.sev_acceptance,
        sev_rejection=svaa_handlers.sev_rejection,
        sev_warning=svaa_handlers.sev_warning,
        delivered_confirmation=svaa_handlers.delivered_confirmation,
        delivered_rejection=svaa_handlers.delivered_rejection,
        ecvnaa_confirmation=EcvnaaHandler(
            connect=connect, store_key=store_key or _no_key_store
        ),
        report=ReportHandler(connect=connect),
    )

    return Gateway(
        receiver=Receiver(
            connect=connect,
            archive=archive,
            router=build_router(config, handlers),
            transport=transport,
            response_name=response_filename,
        ),
        sender=Sender(connect=connect, archive=archive, transport=transport),
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

    # ECVAA feedback that changes what we believe about a notification.
    router.register(ecvaa.REJECTION_FILE_TYPE, handlers.ecvn_rejection)
    router.register(ecvaa.ACCEPTANCE_FILE_TYPE, handlers.ecvn_acceptance)
    router.register(ecvaa.WMAN_EXCEPTION_FILE_TYPE, handlers.wman_exception)

    # SVAA feedback on expected volumes and delivered volumes.
    router.register(svaa.SEV_ACCEPTANCE_FILE_TYPE, handlers.sev_acceptance)
    router.register(svaa.SEV_REJECTION_FILE_TYPE, handlers.sev_rejection)
    router.register(svaa.SEV_WARNING_FILE_TYPE, handlers.sev_warning)
    router.register(
        svaa.DELIVERED_CONFIRMATION_FILE_TYPE, handlers.delivered_confirmation
    )
    router.register(
        svaa.DELIVERED_REJECTION_FILE_TYPE, handlers.delivered_rejection
    )

    # The authorisation confirmation, which carries the ECVNAA Key. Without it
    # no ECVN can be submitted, so all three versions are registered.
    for file_type in ECVNAA_FEEDBACK_FILE_TYPES:
        router.register(file_type, handlers.ecvnaa_confirmation)

    # Reports are registered so an arriving report is recorded rather than
    # counted as unhandled. An unknown file type raises at startup rather than
    # being skipped: a typo that silently registers nothing looks identical to
    # working until the report arrives and is ignored.
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


def _no_key_store(ecvnaa_id: str, key: str) -> str:
    """The default store_key: refuse rather than discard.

    An ECVNAA Key arrives exactly once, in E0071, and is required on every
    subsequent ECVN. A deployment that forgets to configure a secret store
    would otherwise receive the key, acknowledge the file, throw the key away,
    and then be unable to submit anything -- with nothing in the logs to say
    why. Failing here is far cheaper than diagnosing that later.
    """
    raise RuntimeError(
        f"an ECVNAA key arrived for {ecvnaa_id} but no secret store was configured. "
        f"Pass store_key to app.build. Without it, no ECVN can be submitted."
    )


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value