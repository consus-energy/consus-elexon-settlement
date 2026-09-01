# ADR-0005: One transaction per state transition

## Status

Accepted, August 2026.

## Context

State transitions are guarded twice: `states.py` checks the change is legal,
and every UPDATE carries a WHERE clause on the current state so a concurrent
writer loses rather than double-applying.

Both guards raise `TransitionError` on failure. The original implementation
raised inside whatever transaction the caller held.

Postgres aborts an entire transaction on error. Every subsequent statement on
that connection fails with `InFailedSqlTransaction` until a rollback.

An integration test caught this: a handler catching `TransitionError` and
continuing would fail on everything after it. In production that means one bad
notification takes down a whole poller batch, including feedback that needed
acting on before Gate Closure.

## Decision

Each transition opens its own transaction. `_move_item_locked` is the
exception and is named to say so: it requires the caller to hold one, because
the state change and the cascade to period rows must commit together.

## Consequences

A rejected transition affects only itself. The connection stays usable and the
batch continues.

More transactions per batch. At tens of files a day this is not measurable.

Callers cannot wrap several transitions in one atomic unit. Nothing currently
needs to, and if something does it will need a deliberate design rather than
inheriting the property by accident.

## Alternatives considered

**Catch and roll back in the caller.** Pushes the requirement onto every call
site, where it will eventually be forgotten, and the failure is silent until a
batch dies.

**Check legality before opening a transaction.** Closes the common case but
not the concurrent one, which is the case that matters.
