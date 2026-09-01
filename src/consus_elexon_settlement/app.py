"""Composition root: where the pieces are wired together.

Everything below this module is ignorant of everything else -- file.py does
not know about the database, the router does not know which handlers exist,
handlers do not know how files arrive. This is the only place that knows all
of it, which is what keeps the rest testable in isolation.

Nothing here does work. It builds objects and returns them. If a function in
this module grows an if statement about settlement, it is in the wrong place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .idd import spec, spec_cra, spec_saa, spec_svaa
from .inbound import ecvaa
from .inbound.router import Handler, Router

# IDD 2.2.2: the test data flag. 'OPER' or omitted means operational; any
# other value is a test phase. Held here so the check is in one place.
OPERATIONAL = "OPER"


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
        """
        return cls(
            vtp=Identity("VT", _require("CONSUS_VTP_PARTICIPANT")),
            ecvna=Identity("EN", _require("CONSUS_ECVNA_PARTICIPANT")),
            environment=os.environ.get("CONSUS_ENVIRONMENT", "TEST1"),
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
    def test_data_flag(self) -> str | None:
        """The AAA header test flag. None when operational, since the field
        may be omitted."""
        return None if self.is_operational else self.environment


@dataclass(frozen=True)
class Handlers:
    """The inbound handlers, gathered so build_router takes one argument
    rather than a list that grows with every flow."""

    ecvn_rejection: Handler
    ecvn_acceptance: Handler
    wman_exception: Handler


def build_router(config: Config, handlers: Handlers) -> Router:
    """The inbound router, with every spec and the known handlers registered.

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

    return router


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


handlers = EcvaaHandlers(connect=lambda: db.connect(dsn))
router = build_router(config, Handlers(
    ecvn_rejection=handlers.ecvn_rejection,
    ecvn_acceptance=handlers.ecvn_acceptance,
    wman_exception=handlers.wman_exception,
))