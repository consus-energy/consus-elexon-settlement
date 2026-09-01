"""Lifecycle states for outbound files and the notifications inside them.

Two vocabularies, because two different things are being tracked.

A FILE moves through transmission. Its terminal state is RECEIPT_ACKED: the
recipient confirms the bytes arrived and parsed. That is as far as a file
gets. Whether the contents are agreed is a separate question with a separate
answer, arriving later in a separate flow.

An ITEM -- a notification, an expected volume, a wholesale market activity --
moves through business validation. It is SUBMITTED when its file is sent, and
ACCEPTED or REJECTED when feedback arrives.

    file:  RESERVED -> BUILT -> SENT -> RECEIPT_ACKED
                                    \\-> SEND_FAILED -> SENT
           any -> SUPERSEDED  (NACK codes 1-3, sequence number reused)

    item:  PENDING -> SUBMITTED -> ACCEPTED
                                \\-> REJECTED

Conflating the two is the mistake this module exists to prevent. IDD 2.2.7 is
explicit that receipt acknowledgement does not imply acceptance of contents,
and a file can be receipt-acked at 09:00 and wholly rejected at 09:20. Code
that treats an ADT as acceptance will believe a position is hedged when it is
not, and will discover otherwise at cash-out.

UNACKNOWLEDGED is not a transition anyone makes deliberately. It is what a
file becomes when nothing has come back and the deadline is approaching, and
it exists so that silence is visible rather than indistinguishable from
success.
"""

from __future__ import annotations

from typing import Final

# --- file states ------------------------------------------------------------

RESERVED: Final = "RESERVED"
"""Sequence number allocated, bytes not yet written."""

BUILT: Final = "BUILT"
"""Bytes written to the archive. Immutable from here: a retry sends these
bytes, it does not regenerate them. Regenerating would burn a sequence
number and leave a permanent gap."""

SENT: Final = "SENT"
"""Handed to transport successfully. Says nothing about receipt."""

SEND_FAILED: Final = "SEND_FAILED"
"""Transport failed. Retryable, against the same bytes and the same
sequence number."""

RECEIPT_ACKED: Final = "RECEIPT_ACKED"
"""ADT received with response code 0. The file arrived and parsed.
NOT acceptance."""

UNACKNOWLEDGED: Final = "UNACKNOWLEDGED"
"""Sent, but nothing came back within the expected window. A position may be
unhedged. Requires a human."""

SUPERSEDED: Final = "SUPERSEDED"
"""Replaced by a corrected file reusing the same sequence number, following a
NACK with code 1, 2 or 3 (IDD 2.2.8)."""

FILE_STATES: Final = frozenset({
    RESERVED, BUILT, SENT, SEND_FAILED, RECEIPT_ACKED, UNACKNOWLEDGED, SUPERSEDED,
})

# --- item states ------------------------------------------------------------

PENDING: Final = "PENDING"
"""Recorded, its file not yet sent."""

SUBMITTED: Final = "SUBMITTED"
"""Its file has been sent. Awaiting business validation."""

ACCEPTED: Final = "ACCEPTED"
"""Passed validation and is in effect."""

REJECTED: Final = "REJECTED"
"""Failed validation and is not in effect. Carries a reason."""

ITEM_STATES: Final = frozenset({PENDING, SUBMITTED, ACCEPTED, REJECTED})


# --- transitions ------------------------------------------------------------

# SUPERSEDED is reachable from any non-terminal file state: a NACK can arrive
# at any point after sending, and the correction supersedes whatever was
# there. Encoded per-state rather than as a special case so the table stays
# readable.
_FILE_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    RESERVED:       frozenset({BUILT, SUPERSEDED}),
    BUILT:          frozenset({SENT, SEND_FAILED, SUPERSEDED}),
    SEND_FAILED:    frozenset({SENT, SEND_FAILED, SUPERSEDED}),
    SENT:           frozenset({RECEIPT_ACKED, UNACKNOWLEDGED, SUPERSEDED}),
    UNACKNOWLEDGED: frozenset({RECEIPT_ACKED, SUPERSEDED}),
    RECEIPT_ACKED:  frozenset({SUPERSEDED}),
    SUPERSEDED:     frozenset(),
}

_ITEM_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    PENDING:   frozenset({SUBMITTED}),
    # A rejected item is corrected by a new submission, which is a new row.
    # Moving this one back to SUBMITTED would lose the record of what was
    # rejected and why.
    SUBMITTED: frozenset({ACCEPTED, REJECTED}),
    ACCEPTED:  frozenset(),
    REJECTED:  frozenset(),
}

TERMINAL_FILE_STATES: Final = frozenset({RECEIPT_ACKED, SUPERSEDED})
TERMINAL_ITEM_STATES: Final = frozenset({ACCEPTED, REJECTED})

# States where we are still waiting on the other side. Everything here is a
# candidate for the deadline sweep.
OUTSTANDING_FILE_STATES: Final = frozenset({BUILT, SENT, SEND_FAILED, UNACKNOWLEDGED})
OUTSTANDING_ITEM_STATES: Final = frozenset({PENDING, SUBMITTED})


class TransitionError(RuntimeError):
    """An illegal state change was attempted.

    Always a bug, never an operational condition. Feedback arriving twice for
    the same notification is handled by checking is_terminal first, not by
    catching this.
    """


def check_file_transition(current: str, target: str) -> None:
    _check(current, target, _FILE_TRANSITIONS, "file")


def check_item_transition(current: str, target: str) -> None:
    _check(current, target, _ITEM_TRANSITIONS, "item")


def _check(current: str, target: str, table: dict[str, frozenset[str]], kind: str) -> None:
    if current not in table:
        raise TransitionError(f"unknown {kind} state {current!r}")
    if target not in table:
        raise TransitionError(f"unknown {kind} state {target!r}")
    if target not in table[current]:
        allowed = sorted(table[current]) or ["nothing, it is terminal"]
        raise TransitionError(
            f"cannot move {kind} from {current} to {target}; allowed: {', '.join(allowed)}"
        )


def is_terminal_file(state: str) -> bool:
    return state in TERMINAL_FILE_STATES


def is_terminal_item(state: str) -> bool:
    return state in TERMINAL_ITEM_STATES


def file_outstanding(state: str) -> bool:
    """True while we are still waiting on the other side."""
    return state in OUTSTANDING_FILE_STATES


def item_outstanding(state: str) -> bool:
    return state in OUTSTANDING_ITEM_STATES