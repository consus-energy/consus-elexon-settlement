# ADR-0010: Environments are separated by channel, not config

## Status

Accepted, August 2026.

## Context

The file header carries a test data flag (IDD 2.2.1, field 10). 'OPER' or
omitted means operational; any other value marks a test phase.

Sending an operational file from a test environment is among the worst things
this system could do. It would enter live settlement, and there is no
mechanism to withdraw it after Gate Closure.

The obvious control is a configuration flag checked before sending. That is a
control someone can forget, override for a local test, or leave set wrongly
after a deployment.

## Decision

The test flag is part of the channel's unique key, alongside the four role and
participant fields. A channel is operational or it is not, and the flag comes
from the channel row when the header is built. It is not a parameter.

`app.channel_for` supplies the flag from configuration, so a caller cannot ask
for a channel with a different one.

`Config.from_env` defaults the environment to a test value, not to
operational. A missing variable should mean "this is a test", never "send this
to live settlement".

Test and operational run in separate GCP projects with separate credentials,
so a test deployment has no route to the operational endpoint.

## Consequences

A test process looking for the operational channel finds no row and raises. It
cannot construct the header, so it cannot build the file, so there is nothing
to send. The separation is structural rather than a check.

Every environment needs its channel rows seeded before it can send anything.
That is a deployment step, and its absence fails loudly.

Testing operational behaviour locally requires an operational channel row,
which is deliberately awkward.

## Alternatives considered

**A configuration flag checked at send time.** One `if` between a test system
and live settlement, and one that reads as unremarkable in review.

**Environment inferred from the DSN or project id.** An inference, and the
failure mode of a wrong inference is the one we are trying to prevent.
