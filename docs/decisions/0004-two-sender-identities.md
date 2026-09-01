# ADR-0004: Two sender identities, two sequence counters

## Status

Accepted, August 2026.

## Context

Consus is registering both as a Virtual Trading Party and as its own ECVN
Agent. Only an ECVNA may submit an ECVN (BSCP71), so we hold that role rather
than appointing a third party.

The two roles have different participant identifiers and different role codes
in the file header:

    E0041 ECVN          'EN' plus our ECVNA Id
    E0511 WMAN          'VT' plus our Party Id
    P0328, P0282        'VT' plus our Party Id

Sequence numbers are contiguous per from-role, from-participant, to-role,
to-participant combination. Two identities therefore means two independent
counters.

## Decision

The `channel` table is keyed on the full tuple plus the environment flag, and
holds its own `next_sequence`. The caller selects the identity by passing the
right channel; nothing infers it from the file type.

The inbound router holds both identities and replies as whichever one a file
was addressed to.

## Consequences

Mixing the identities corrupts both sequences, and neither can be corrected
retrospectively. Making the channel an explicit argument means the choice is
visible at the call site rather than buried in a lookup.

A file addressed to a role and participant we do not hold is rejected rather
than handled. Acting on someone else's file would be worse than refusing one
of our own.

There is no single "our participant id", which occasionally reads awkwardly.
That awkwardness is the point: code that assumes one identity would be wrong
half the time.

## Alternatives considered

**Infer the identity from the file type.** A lookup table from flow to role.
Rejected because an inference that is wrong corrupts a sequence silently, and
the table would need updating for every new flow.

**Appoint a third-party ECVN Agent.** Would leave one identity, but makes
contract notification -- core to the business -- dependent on someone else.
