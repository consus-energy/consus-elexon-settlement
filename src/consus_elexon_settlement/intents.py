"""Intent lifecycle: what the EMS asked for, and what became of it.

The layer above submissions, and the third state vocabulary in this system.
Three, deliberately, because they answer different questions:

    file    did the bytes reach central systems?      (states.py)
    item    did central systems agree with them?      (states.py)
    intent  did the EMS get what it asked for?        (here)

Different words for each so nobody conflates them. A file can be
RECEIPT_ACKED while its notification is REJECTED and its intent is PARTIAL,
and all three are true at once.

ACTED MEANS ACCEPTED, NOT SENT.

Sending is immediate; acceptance arrives minutes to hours later. So an intent
sits in ACTING for a while, and that is accurate rather than a problem: until
acceptance arrives a rejection is still possible, and an intent reading ACTED
would be the one place in this system claiming more than it knows.

The sweep depends on it. An open intent approaching Gate Closure is precisely
what a human needs to see, and under the alternative there would be nothing
outstanding to show them.

    RECEIVED -> ACTING -> ACTED
                       \\-> PARTIAL -> ACTING     (retry)
                       \\-> MISSED               (deadline gone)
             \\-> REJECTED                       (never actionable)

MISSED is terminal and distinct from PARTIAL. A partial intent can be retried;
a missed one cannot, because the deadline has passed. Merging them would let a
retry loop keep attempting something that can never succeed, and each attempt
would spend a sequence number on a file central systems will refuse.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from . import deadlines

# --- intent states ----------------------------------------------------------

RECEIVED: Final = "RECEIVED"
"""Recorded, nothing attempted. The message arrived and is durable."""

ACTING: Final = "ACTING"
"""At least one flow attempted, not all accepted yet.

The normal state between submission and acceptance. An intent here is not
stuck; it is waiting for central systems.
"""

ACTED: Final = "ACTED"
"""Every required flow ACCEPTED. The EMS got what it asked for."""

PARTIAL: Final = "PARTIAL"
"""Some flows succeeded, some failed, and the deadline has not passed.

Retryable, and the retry sends only the outstanding flows. Resending one that
already succeeded would spend a sequence number on a duplicate.
"""

MISSED: Final = "MISSED"
"""Gate Closure passed with flows outstanding.

Terminal. Nothing can be submitted for this period now, and the position is
unhedged. This is the state that should have produced an alert while there was
still time.
"""

REJECTED: Final = "REJECTED"
"""Never actionable: no channel, an unknown BM Unit, a malformed message.

Distinct from MISSED. MISSED means we ran out of time; REJECTED means there
was nothing we could have done, and the fault is upstream.
"""

INTENT_STATES: Final = frozenset({
    RECEIVED, ACTING, ACTED, PARTIAL, MISSED, REJECTED,
})

# --- flow states ------------------------------------------------------------

PENDING: Final = "PENDING"
SENT: Final = "SENT"
"""Handed to transport. Says nothing about acceptance."""

ACCEPTED: Final = "ACCEPTED"
FAILED: Final = "FAILED"
"""Build or transport failed. Retryable: nothing reached central systems."""

FLOW_REJECTED: Final = "REJECTED"
"""Central systems refused it. NOT retryable under this intent.

A rejection is a business decision about the content, so resending the same
content would be refused again. Correcting it is a new intent at the next
revision, which keeps the record of what was rejected and why.
"""

FLOW_STATES: Final = frozenset({PENDING, SENT, ACCEPTED, FAILED, FLOW_REJECTED})

# The flows an ordinary trading intent requires, and why each is not optional.
#
#   wman  tells ECVAA we were active. Without it SVAA never learns, so no
#         deviation is measured whatever the ECVN says.
#   ecvn  the contracted volume. Without it the position is cashed out at the
#         imbalance price in full.
#   sev   what the unit would have done absent our action. Without it, or a
#         Default, SVAA sets Settlement Expected Volume to NULL and the
#         deviation is lost (BSCP602 2.13.7).
REQUIRED_FLOWS: Final = ("wman", "ecvn", "sev")

# Delivered volume is its own intent kind: the MSID Pair is unknown at trading
# time, the deadline is a day later, and one delivered volume may cover a
# period traded across several intents.
DELIVERED_FLOW: Final = "delivered"


_INTENT_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    RECEIVED: frozenset({ACTING, REJECTED, MISSED}),
    ACTING:   frozenset({ACTED, PARTIAL, MISSED}),
    PARTIAL:  frozenset({ACTING, MISSED}),
    ACTED:    frozenset(),
    MISSED:   frozenset(),
    REJECTED: frozenset(),
}

_FLOW_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    PENDING:       frozenset({SENT, FAILED}),
    FAILED:        frozenset({SENT, FAILED}),
    SENT:          frozenset({ACCEPTED, FLOW_REJECTED}),
    ACCEPTED:      frozenset(),
    FLOW_REJECTED: frozenset(),
}

TERMINAL_INTENT_STATES: Final = frozenset({ACTED, MISSED, REJECTED})
OPEN_INTENT_STATES: Final = frozenset({RECEIVED, ACTING, PARTIAL})
RETRYABLE_FLOW_STATES: Final = frozenset({PENDING, FAILED})


class IntentError(RuntimeError):
    """An illegal intent state change. Always a bug, never a condition."""


def check_intent_transition(current: str, target: str) -> None:
    _check(current, target, _INTENT_TRANSITIONS, "intent")


def check_flow_transition(current: str, target: str) -> None:
    _check(current, target, _FLOW_TRANSITIONS, "flow")


def _check(current: str, target: str, table: dict[str, frozenset[str]], kind: str) -> None:
    if current not in table:
        raise IntentError(f"unknown {kind} state {current!r}")
    if target not in table:
        raise IntentError(f"unknown {kind} state {target!r}")
    if target not in table[current]:
        allowed = sorted(table[current]) or ["nothing, it is terminal"]
        raise IntentError(
            f"cannot move {kind} from {current} to {target}; allowed: {', '.join(allowed)}"
        )


def is_open(state: str) -> bool:
    """Still waiting on something. What the sweep looks for."""
    return state in OPEN_INTENT_STATES


def is_terminal(state: str) -> bool:
    return state in TERMINAL_INTENT_STATES


def resolve(flow_states: dict[str, str], past_deadline: bool) -> str:
    """What the intent's state should be, given its flows.

    Derived rather than tracked, so the intent cannot disagree with its own
    flows. Recomputed after every flow change.

    The deadline is checked FIRST. An intent whose flows all failed and whose
    Gate Closure has passed is MISSED, not PARTIAL -- PARTIAL invites a retry,
    and a retry after the deadline spends a sequence number on a file central
    systems will refuse.
    """
    if not flow_states:
        return RECEIVED

    outstanding = {
        flow for flow, state in flow_states.items() if state != ACCEPTED
    }

    if not outstanding:
        return ACTED

    if past_deadline:
        return MISSED

    if any(state == FLOW_REJECTED for state in flow_states.values()):
        # A rejection is not retryable under this intent: the content was
        # refused, so resending it would be refused again. But the deadline
        # has not passed, so a new intent at the next revision could still
        # succeed -- which is a decision for the EMS, not for us.
        return PARTIAL

    if all(state == PENDING for state in flow_states.values()):
        return RECEIVED

    if any(state == FAILED for state in flow_states.values()):
        return PARTIAL

    # Some sent, none accepted yet, nothing failed. Waiting.
    return ACTING


def retryable_flows(flow_states: dict[str, str]) -> list[str]:
    """Which flows a retry should attempt.

    Only PENDING and FAILED. Resending an accepted flow would spend a sequence
    number on a duplicate, and resending a rejected one would be refused
    again for the same reason as the first time.
    """
    return sorted(
        flow for flow, state in flow_states.items()
        if state in RETRYABLE_FLOW_STATES
    )


@dataclass(frozen=True)
class Intent:
    """A decision by the EMS, before the gateway has done anything with it.

    Volumes are in CVA convention -- positive is Export, negative is Import --
    because that is what goes on the wire. The gateway does not convert; the
    EMS sends what it means.

    The gateway holds no view on whether these numbers are right. It checks
    the format and the deadline. A wrong expected volume sends cleanly and
    settles wrongly, and that check belongs in the EMS.
    """

    settlement_date: dt.date
    settlement_period: int
    bmu_id: str
    expected_mwh: Decimal
    contracted_mwh: Decimal
    ecvnaa_id: str
    ecvn_ecvnaa_id: str
    revision: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.settlement_period <= 50:
            raise ValueError(
                f"settlement period out of range: {self.settlement_period}"
            )
        if self.revision < 1:
            raise ValueError(f"revision starts at 1, got {self.revision}")
        # Range checks only. The gateway does not judge magnitudes: a volume
        # that is wrong but well formed is the EMS's problem, and refusing it
        # here would mean guessing at a business rule we do not own.
        if -self.expected_mwh.as_tuple().exponent > 4:
            raise ValueError(
                f"expected volume has more than four decimal places: "
                f"{self.expected_mwh}"
            )
        if -self.contracted_mwh.as_tuple().exponent > 3:
            raise ValueError(
                f"contracted volume has more than three decimal places: "
                f"{self.contracted_mwh}"
            )

    @property
    def key(self) -> tuple[dt.date, int, str, int]:
        """The idempotency key.

        A natural key rather than a UUID. Pub/Sub delivers at least once, so
        the same message can arrive twice -- but the EMS can also send the
        same decision twice by mistake, and a UUID would make those two cases
        look different. This makes them the same case.
        """
        return (self.settlement_date, self.settlement_period,
                self.bmu_id, self.revision)

    @property
    def gate_closure(self) -> dt.datetime:
        """The deadline this intent must meet.

        Computed on receipt and stored, so a late arrival is distinguishable
        from one we delayed. That distinction is the first thing anyone wants
        when explaining a missed submission.
        """
        return deadlines.gate_closure(self.settlement_date, self.settlement_period)

    def arrived_too_late(self, now: dt.datetime | None = None) -> bool:
        """True if there is no point attempting this.

        Submitting after Gate Closure is worse than not submitting: it is a
        file central systems reject, a sequence number spent, and a record
        suggesting we tried when we had already run out of time.
        """
        return deadlines.is_closed(self.settlement_date, self.settlement_period, now)


@dataclass(frozen=True)
class DeliveredIntent:
    """A delivered volume, from the EMS, at D+1.

    Its own kind rather than a fourth flow on Intent: the MSID Pair is not
    known at trading time, the deadline is a day later, and one delivered
    volume may cover a period traded across several intents.

    delivered_mwh is our deviation, not the metered volume. SVAA holds the
    metered data already and uses this to allocate how much of it was ours
    (BSCP602 Appendix 3.6).
    """

    settlement_date: dt.date
    settlement_period: int
    bmu_id: str
    gsp_group_id: str
    import_msid: int
    delivered_mwh: Decimal
    export_msid: int | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.settlement_period <= 50:
            raise ValueError(
                f"settlement period out of range: {self.settlement_period}"
            )
        if len(str(self.import_msid)) > 13:
            raise ValueError(f"import MSID exceeds 13 digits: {self.import_msid}")
        if self.export_msid is not None and len(str(self.export_msid)) > 13:
            raise ValueError(f"export MSID exceeds 13 digits: {self.export_msid}")

    @property
    def key(self) -> tuple[dt.date, int, str, int, int]:
        return (self.settlement_date, self.settlement_period,
                self.bmu_id, self.import_msid, self.revision)