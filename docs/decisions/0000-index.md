# Architecture decision records

Short records of decisions that shaped this system, why they were made, and
what was rejected. They exist for two reasons.

The first is practical: several of these decisions look wrong at a glance, and
the obvious alternative is worse in a way that only shows up in production.
Without a record, someone will eventually "simplify" one of them.

The second is regulatory. Under the BSC, material changes to systems within
the scope of Qualification are assessed before implementation. An assessor
asking why something is built a particular way needs a written answer, not a
reconstruction from memory.

New records are numbered sequentially and never edited once accepted. A
decision that is later reversed gets a new record superseding the old one, and
the old one stays.

| ADR | Decision |
|-----|----------|
| [0001](0001-spec-generated-from-idd-spreadsheet.md) | The flow spec is generated from the IDD spreadsheet |
| [0002](0002-build-once-send-many.md) | A file is built once; retries send the archived bytes |
| [0003](0003-postgres-for-sequence-allocation.md) | Postgres, for transactional sequence allocation |
| [0004](0004-two-sender-identities.md) | Two sender identities, two sequence counters |
| [0005](0005-transaction-per-state-transition.md) | One transaction per state transition |
| [0006](0006-period-rows-move-with-parents.md) | Period rows move with their parent item |
| [0007](0007-settlement-periods-from-local-midnight.md) | Settlement periods run from local midnight |
| [0008](0008-receipt-is-not-acceptance.md) | Receipt acknowledgement is not acceptance |
| [0009](0009-reports-handled-generically.md) | Reports are recorded, not modelled |
| [0010](0010-environment-separation-by-channel.md) | Environments are separated by channel, not config |
