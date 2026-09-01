# ADR-0006: Period rows move with their parent item

## Status

Accepted, August 2026.

## Context

A notification, an expected volume and a delivered volume each have a parent
row and a set of settlement period rows. Both carry a state.

The original `submit_items` moved only the parent from PENDING to SUBMITTED.
The later cascade -- `accept_notification`, `reject_notification` -- matches on
SUBMITTED, so it found nothing and the period rows stayed PENDING forever.

The parent state looked correct throughout. Only the period rows were wrong,
and nothing read them, so it would have been invisible until someone
investigated a partial rejection.

That matters because E0091 rejects per settlement period. The per-period state
is what records which half of a partially rejected notification is still live.
Silently wrong is worse there than absent.

## Decision

`_ITEM_TABLES` maps each item table to its period child and foreign key.
`submit_items` moves both. `accept_*` and `reject_*` cascade to the child
within the same transaction.

`wman` maps to None: it is flat, one row per BM Unit, with the settlement
period as a column.

## Consequences

The relationship is declared in one place rather than repeated in each
function, so a new item table with periods is one entry rather than four
call sites.

Parent and child cannot diverge, because they move in the same transaction.

`_ITEM_TABLES` is a dict rather than a set, which reads oddly at the
membership checks. Keeping the mapping and the membership test as one
structure is worth the slight awkwardness.

## Alternatives considered

**Derive the child table by naming convention.** `notification` ->
`notification_period` holds today and would break the first time it did not,
silently.

**A database trigger.** Moves the logic somewhere migrations own and tests do
not reach.
