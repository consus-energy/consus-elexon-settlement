# ADR-0003: Postgres, for transactional sequence allocation

## Status

Accepted, August 2026.

## Context

Sequence numbers must be contiguous per channel. Allocating one and then
failing to persist the file that uses it leaves a permanent gap.

So allocation and file insertion must be atomic: read the counter, increment
it, insert the row, all or nothing, with concurrent allocators serialised.

The rest of the platform runs on Google Cloud. Firestore is the default choice
there and would otherwise be a reasonable fit.

## Decision

Cloud SQL for Postgres, with `SELECT ... FOR UPDATE` on the channel row held
across the counter update and the file insert.

## Consequences

The guarantee is visible in the code. An assessor reading `allocate_sequence`
can see the lock, the increment and the insert in one transaction without
knowing anything about our infrastructure.

A unique index on `(channel_id, sequence_number)` for live files backs it up,
so a duplicate is rejected by the database even if the application logic is
wrong.

We carry a relational database for a small volume of data -- tens of files a
day. The cost is not the storage, it is the operational surface: backups,
migrations, connection management.

Migrations become part of the deployment. `migrate.py` records a checksum per
applied file, so editing an applied migration fails loudly rather than
diverging silently between environments.

## Alternatives considered

**Firestore transactions.** Can express this, but the guarantee is a property
of the client library rather than something visible in the query. For a
control this important, being able to point at `FOR UPDATE` in a review is
worth more than the convenience.

**A cloud counter service.** Adds a dependency in the path of every file, at
Gate Closure, for something a row lock does adequately.
