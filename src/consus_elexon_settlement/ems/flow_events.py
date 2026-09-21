"""Telling the EMS what happened to a flow. The return half of a one-way bridge.

The bridge into this service is one way: the EMS publishes an intent, Pub/Sub
discards our response body, and nothing goes back. So the EMS could never tell
a WMAN that reached ECVAA from one thrown out -- and its dispatch gate had to
INFER that a settlement period was covered from its own published intent, which
is a record of what it asked for rather than of what the market did.

This module builds the messages that close that. Pure functions: no HTTP, no
database, no Pub/Sub client, for the same reason `ems/messages.py` is pure --
the contract is the part most likely to drift, and it should be testable
without either side of the wire.

THERE IS NO `accepted` EVENT AND THERE NEVER WILL BE. ECVAA Service Description
v26.0 section 9A gives a Virtual Trading Party no acceptance signal: 9A.2 and
9A.3 promise nothing, the only outbound flows are the rejections at 9A.4 and
9A.5, and the Acceptance Feedback Report that ECVNs and MVRNs receive within
fifteen minutes does not go to VTPs. So the strongest fact this channel can
ever carry is "submitted, and no rejection since", and a value named `accepted`
would assert something the market never said. The EMS parser refuses it; this
builder cannot produce it.

WHOSE CLOCK, AND WHY IT IS AN EXPLICIT FIELD.

The EMS contract first asked for "when ECVAA received the submission". What
this service holds for a submission is `outbound_file.sent_at` -- the moment we
handed the file to FTP. ECVAA's own receipt time arrives later, in the ADT
record of the acknowledgement, and that inbound path is not wired to the router
today.

Sending one under the other's name would be the OPTIMISTIC direction on a
deadline check: a WMAN handed over a minute before Gate Closure and received a
minute after it would read as punctual. So `occurred_at_basis` says which fact
the timestamp is:

    sent      our handover to transport. A LOWER BOUND on their receipt.
    received  ECVAA's own clock, from the ADT. Exact.

The asymmetry is what makes one field enough. A lower bound at or after Gate
Closure PROVES lateness; one before it proves nothing. So `sent` can convict
and cannot acquit, and the EMS treats it accordingly -- it alarms on a
submission whose only evidence is a lower bound close to the deadline, rather
than refusing it outright, which would make this channel inert until the ADT
path lands.

A REJECTION IS ALWAYS `received`. We do not reject a flow; we relay one ECVAA
issued, and the issue time is on the message we relay. `rejected_event` does
not take a basis for that reason -- there is no reading of a rejection where a
transport timestamp is the right answer.
"""

from __future__ import annotations

import datetime as dt

KIND = "flow_event"

# Which flow. `delivered` is deliberately absent: it is a D+1 volume report,
# not a notification that gates a dispatch, and the EMS has no gate to open on
# it. Adding it here would invite a reader to think otherwise.
WMAN = "wman"
ECVN = "ecvn"
SEV = "sev"
FLOWS = (WMAN, ECVN, SEV)

SUBMITTED = "submitted"
REJECTED = "rejected"

BASIS_SENT = "sent"
BASIS_RECEIVED = "received"


class FlowEventError(ValueError):
    """We were asked to build a message that cannot be true.

    Raised at BUILD time, in this process, rather than being sent and refused
    at the far end -- where the failure would be a log line in somebody else's
    project and the flow event would simply be missing.
    """


def _instant(value: dt.datetime, field: str) -> str:
    """A UTC instant in the shape the EMS parser accepts.

    NAIVE IS REFUSED, NOT ASSUMED. The EMS compares this against Gate Closure,
    a UTC instant derived from a UK local calendar, so an hour in the wrong
    direction turns a late submission into a punctual one on every BST day of
    the year. Its parser refuses a naive timestamp; refusing here too means the
    failure surfaces where somebody can fix it.
    """
    if value.tzinfo is None:
        raise FlowEventError(
            f"{field} has no timezone. It is compared against Gate Closure, so "
            "assuming UTC would make every BST event look an hour earlier."
        )
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _common(flow: str, bmu_id: str, settlement_date: dt.date, settlement_period: int) -> dict:
    if flow not in FLOWS:
        raise FlowEventError(f"unknown flow {flow!r}; expected one of {list(FLOWS)}")
    if not bmu_id:
        raise FlowEventError("bmu_id is required: the EMS gate is per BM Unit")
    # 1..50, because a settlement day is 46, 48 or 50 periods long. The EMS
    # checks the number against its own date; this is the outer bound, so an
    # obviously wrong value does not travel.
    if not 1 <= settlement_period <= 50:
        raise FlowEventError(
            f"settlement_period {settlement_period} is outside 1..50"
        )
    return {
        "kind": KIND,
        "flow": flow,
        "bmu_id": bmu_id,
        "settlement_date": settlement_date.isoformat(),
        "settlement_period": settlement_period,
    }


def submitted_event(
    *,
    flow: str,
    bmu_id: str,
    settlement_date: dt.date,
    settlement_period: int,
    occurred_at: dt.datetime,
    basis: str,
) -> dict:
    """We put a flow on the wire for this BM Unit and period.

    `basis` IS REQUIRED AND HAS NO DEFAULT. Defaulting it to `sent` would make
    the day the ADT path lands a silent behaviour change, and defaulting it to
    `received` would be a claim we cannot back. The caller knows which
    timestamp it is holding; nothing here can work it out.
    """
    if basis not in (BASIS_SENT, BASIS_RECEIVED):
        raise FlowEventError(
            f"unknown basis {basis!r}; expected {BASIS_SENT!r} or {BASIS_RECEIVED!r}"
        )
    return {
        **_common(flow, bmu_id, settlement_date, settlement_period),
        "event": SUBMITTED,
        "occurred_at": _instant(occurred_at, "occurred_at"),
        "occurred_at_basis": basis,
    }


def rejected_event(
    *,
    flow: str,
    bmu_id: str,
    settlement_date: dt.date,
    settlement_period: int,
    occurred_at: dt.datetime,
    rejection_code: str,
    rejection_detail: str | None = None,
) -> dict:
    """ECVAA threw a flow back.

    NO BASIS PARAMETER. A rejection is ECVAA's decision and its time is theirs,
    so the basis is always `received`. A parameter would be a way to get it
    wrong for no gain.

    `rejection_code` IS REQUIRED. A rejection with no reason is a dead end for
    whoever reads it at settlement, and this is the only place the code exists
    in a form the EMS can see -- the file it came in stays here.
    """
    if not rejection_code:
        raise FlowEventError(
            "a rejection needs a code. Without one the EMS records that a "
            "period failed and nothing about why, which is the state somebody "
            "has to act on at settlement."
        )
    message = {
        **_common(flow, bmu_id, settlement_date, settlement_period),
        "event": REJECTED,
        "occurred_at": _instant(occurred_at, "occurred_at"),
        "occurred_at_basis": BASIS_RECEIVED,
        "rejection_code": rejection_code,
    }
    if rejection_detail:
        message["rejection_detail"] = rejection_detail
    return message
