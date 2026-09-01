# ADR-0008: Receipt acknowledgement is not acceptance

## Status

Accepted, August 2026.

## Context

Two separate things come back from a submitted file, and they arrive at
different times.

An ADT response is a receipt (IDD 2.2.7). It says the bytes arrived and
parsed. The IDD is explicit that it does not imply acceptance of the contents.

Business validation is separate and later: E0281 accepts, E0091 rejects, E0521
rejects a wholesale market activity notification. A file can be receipt-acked
at 09:00 and wholly rejected at 09:20.

Code that treats the ADT as acceptance believes a position is hedged when it
is not, and discovers otherwise at cash-out.

## Decision

Two state vocabularies in `states.py`.

A FILE moves through transmission and terminates at RECEIPT_ACKED. That is as
far as a file gets.

An ITEM -- notification, expected volume, wholesale market activity -- moves
through business validation, from SUBMITTED to ACCEPTED or REJECTED.

They are separate tables of legal transitions. There is no path from a file
state to an item state.

## Consequences

`test_receipt_ack_is_not_acceptance` asserts that after an ADT with response
code 0, the file is RECEIPT_ACKED and the notification inside it is still
SUBMITTED. That test exists to fail if someone collapses the two.

An outstanding item and an outstanding file are different questions with
different answers, so `OUTSTANDING_FILE_STATES` and `OUTSTANDING_ITEM_STATES`
are separate sets.

Two vocabularies is more to hold in mind than one. The alternative is a single
state machine where RECEIPT_ACKED and ACCEPTED sit in the same enum, which is
an invitation to conflate them.

## Alternatives considered

**One state machine.** Simpler to write and wrong in a way that costs money.

**Treat receipt as provisional acceptance.** Would let downstream code proceed
on an ADT. That is precisely the belief this decision exists to prevent.
